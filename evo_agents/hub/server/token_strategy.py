"""The Token strategy at a run's claim (plan token-and-quota-economy, criterion a3): the hub rates the run's difficulty
and gives it the model of the strategy before its credentials are leased (``evo_agents.hub.model_router`` has the
rules), and keeps what it did on the run (``runs.routing``).

``decide`` runs in the claim's transaction, once the run is leased, for a run of a routed kind and runtime that names no
model and whose agent profile does not pin one, while the project's policy has routing on:

- it rates the run: a step by its plan (``rate_step``), any other run by its request or chat message and the role of its
  agent (``rate_prompt``), a chat run that works on plan steps at least as hard as the hardest of them (``with_steps``:
  the coordinator of a plan handed to a system, its open steps; an ask that names a step, that step); a free prompt
  the rules cannot rate is left to the worker's scorer, unless ``routing.scorer`` is ``off``;
- a run whose run before it failed on easy or medium goes one level up: the run it retries or reruns, else the last run
  of the same step and kind (the next dispatch of the step);
- it reads the quota of the run's subscription: the env accounts of a model the run may be leased (its agent's account
  pool, else its dispatcher's own), the most of a weekly window left among those not paused nor cooling down, a weekly
  budget counting as one (``accounts.service.budgets``); none live: used up;
- it takes the strategy's model for the difficulty (``model_router.choose``) and places it: a gateway model goes through
  the gateway when the gateway serves its prefix, the worker's daemon runs gateway runs and the run may use an account
  of the prefix (``gateway_accounts``), and sets ``runs.gateway_provider``, so the run's lease is a gateway key of the
  run (``gateway_leases``); a runtime's own model is Claude Code's, so a run on another runtime keeps its own model.
  A model the run cannot take falls back to the ladder's, and a model the profile's allowlist leaves out is not taken.

A run the hub leaves to the scorer gets the strategy's model of each difficulty (``choices``), the medium one leased
first; when the worker's report names another, ``reported`` moves the run's gateway prefix to that model's, and the
worker takes its leases again before its agent starts. ``reported`` also keeps what the hub rated in every later report
of the run, which carries the worker's own record. ``recommend`` gives a step's difficulty and model to its handoff.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import gateway_runs, model_router, profiles, tables
from evo_agents.hub.account_providers import MODEL_ENV_VARS
from evo_agents.hub.accounts import FIVE_HOUR, SEVEN_DAY, used_now, window_of
from evo_agents.hub.model_router import NATIVE_RUNTIME, Choice, Quota, Rating
from evo_agents.hub.plans import CLOSED_STEP_STATUSES, PlanProblem, step_index, step_key
from evo_agents.hub.server import gateway_accounts
from evo_agents.hub.server.accounts.service.budgets import budget_windows, with_budgets
from evo_agents.hub.server.runs.service.views import current_plan
from evo_agents.hub.server.systems.service import traces as system_traces

MAX_RATED_CHARS = 20_000  # of a request or a chat message the rules read
USED_UP = Quota(weekly_left=0.0, five_hour_used=1.0, known=True)  # every account of the subscription paused or cooling


def _run_query(run_id: int):
    r, ap, v = tables.runs, tables.agent_profiles, tables.agent_profile_versions
    body = select(v.c.body).where(v.c.profile_id == r.c.profile_id, v.c.version == r.c.profile_version)
    return (
        select(
            r.c.id,
            r.c.kind,
            r.c.model,
            r.c.runtime,
            r.c.request,
            r.c.plan_id,
            r.c.step_key,
            r.c.project_id,
            r.c.dispatched_by,
            r.c.principal_id,
            r.c.profile_id,
            r.c.worker_id,
            r.c.parent_run_id,
            r.c.resume_of_run_id,
            r.c.chat_message_id,
            ap.c.project_id.label("profile_project_id"),
            func.coalesce(ap.c.is_default, False).label("profile_default"),
            ap.c.account_pool_id,
            body.scalar_subquery().label("profile"),
        )
        .select_from(r.outerjoin(ap, ap.c.id == r.c.profile_id))
        .where(r.c.id == run_id)
    )


def _permissions(row) -> dict | None:
    body = row.profile if isinstance(row.profile, Mapping) else {}
    found = body.get("permissions")
    return found if isinstance(found, dict) else None


def _asker(row) -> gateway_accounts.Asker:
    agent_owned = row.profile_id is not None and not row.profile_default
    credential = (_permissions(row) or {}).get("credential")
    return gateway_accounts.Asker(
        row.id,
        row.dispatched_by,
        row.dispatched_by if row.principal_id is None else row.principal_id,
        row.project_id,
        row.profile_id,
        row.profile_project_id,
        agent_owned=agent_owned,
        permissions={"credential": credential} if credential else None,
    )


# Rating


def _step_of(plan: Mapping | None, step_key: str | None) -> dict | None:
    if not isinstance(plan, Mapping) or step_key is None:
        return None
    try:
        step = plan["steps"][step_index(dict(plan), step_key)]
    except (PlanProblem, KeyError, TypeError, IndexError):
        return None
    return step if isinstance(step, dict) else None


async def _text_of(conn: AsyncConnection, row) -> str:
    """What the rules read of a run that is not a plan's step: its chat message, else its request."""
    if row.kind == "chat" and row.chat_message_id is not None:
        m = tables.chat_messages
        found = (await conn.execute(select(m.c.body).where(m.c.id == row.chat_message_id))).scalar_one_or_none()
        return (found or "")[:MAX_RATED_CHARS]
    return (row.request or "")[:MAX_RATED_CHARS]


def _role(row) -> str | None:
    if row.profile_default or not isinstance(row.profile, Mapping):
        return None
    return " ".join(str(row.profile.get(key) or "") for key in ("handle", "name")).strip() or None


async def _steps_worked(conn: AsyncConnection, row) -> list[tuple[str, Rating]]:
    """(key, rating) of each plan step chat run ``row`` works on (``system_traces.plan_work``): the steps its ask
    names, or every open step of the plan handed to the system whose coordinator it is; a plan or step the hub lacks
    counts for none."""
    found, bodies = [], {}
    for project_id, plan_id, key in await system_traces.plan_work(conn, row.id):
        if (project_id, plan_id) not in bodies:
            held = await current_plan(conn, project_id, plan_id)
            bodies[project_id, plan_id] = held.body if held is not None and isinstance(held.body, dict) else None
        body = bodies[project_id, plan_id]
        if body is None:
            continue
        if key is not None:
            step = _step_of(body, key)
            found += [] if step is None else [(key, model_router.rate_step(step, body))]
            continue
        for index, step in enumerate(body.get("steps") or ()):
            if isinstance(step, dict) and step.get("status") not in CLOSED_STEP_STATUSES:
                found.append((step_key(step, index), model_router.rate_step(step, body)))
    return found


async def _rating(conn: AsyncConnection, row, plan: Mapping | None) -> Rating:
    step = _step_of(plan, row.step_key) if row.kind == "step" else None
    if step is not None:
        return model_router.rate_step(step, plan)
    rating = model_router.rate_prompt(row.kind, await _text_of(conn, row), _role(row))
    if row.kind != "chat":
        return rating
    return model_router.with_steps(rating, await _steps_worked(conn, row))


async def _failed_before(conn: AsyncConnection, row) -> str | None:
    """The difficulty of the run before run ``row`` when it failed: the run it retries, reruns or goes on from, else
    the last run of the same step and kind; None when there is none, it did not fail, or it was not rated."""
    r = tables.runs
    earlier = [run_id for run_id in (row.parent_run_id, row.resume_of_run_id) if run_id is not None]
    if earlier:
        where = r.c.id.in_(earlier)
    elif row.plan_id is not None and row.step_key is not None:
        where = (
            (r.c.project_id == row.project_id)
            & (r.c.plan_id == row.plan_id)
            & (r.c.step_key == row.step_key)
            & (r.c.kind == row.kind)
            & (r.c.id < row.id)
        )
    else:
        return None
    query = select(r.c.state, r.c.routing).where(where).order_by(r.c.id.desc()).limit(1)
    prior = (await conn.execute(query)).first()
    if prior is None or prior.state != "failed" or not isinstance(prior.routing, Mapping):
        return None
    level = prior.routing.get("difficulty")
    return level if level in model_router.LEVELS else None


# The subscription's quota


async def native_quota(conn: AsyncConnection, owner_id: int, pooled: bool, pool_id: int | None) -> Quota:
    """What the subscription of a run has left (see the module): the env accounts of a model of account pool
    ``pool_id`` for a run under an agent (``pooled``), else those of member ``owner_id``."""
    s, a, m = tables.secrets, tables.accounts, tables.account_pool_members
    if pooled and pool_id is None:
        return Quota()
    query = (
        select(
            s.c.id,
            a.c.last_usage,
            (a.c.paused_at.is_not(None) | func.coalesce(a.c.cooldown_until > func.now(), False)).label("resting"),
        )
        .select_from(s.outerjoin(a, a.c.secret_id == s.c.id))
        .where(s.c.kind == "env", s.c.env_var.in_(MODEL_ENV_VARS), s.c.deleted_at.is_(None))
    )
    if pooled:
        query = query.where(s.c.id.in_(select(m.c.secret_id).where(m.c.pool_id == pool_id)))
    else:
        query = query.where(s.c.owner_id == owner_id)
    rows = (await conn.execute(query)).all()
    live = [row for row in rows if not row.resting]
    if not live:
        return USED_UP if rows else Quota()
    usages = with_budgets({row.id: row.last_usage for row in live}, await budget_windows(conn, [r.id for r in live]))
    now = datetime.now(UTC)
    weekly = [window_of(usages[row.id], SEVEN_DAY) for row in live]
    hourly = [window_of(usages[row.id], FIVE_HOUR) for row in live]
    known = any(window is not None for window in (*weekly, *hourly))
    left = max(1.0 - used_now(window, now) for window in weekly)
    used = min(used_now(window, now) for window in hourly)
    return Quota(weekly_left=left, five_hour_used=used, known=known)


async def _quota_of(conn: AsyncConnection, row) -> Quota:
    return await native_quota(conn, row.dispatched_by, row.profile_id is not None, row.account_pool_id)


# Placing the model


async def _gateway_refusal(conn: AsyncConnection, row, model: str, agent_version: str | None) -> tuple[str | None, str]:
    """(the gateway prefix ``model`` goes through, or None, and why not)."""
    asker = _asker(row)
    prefix = await gateway_accounts.prefix_for(conn, asker, row.runtime, model)
    if prefix is None:
        return None, f"the model gateway serves no prefix of {model}"
    if row.runtime not in gateway_runs.GATEWAY_RUNTIMES or not gateway_runs.takes_gateway_runs(agent_version):
        return None, f"the worker does not run {row.runtime} through the model gateway"
    usable = await gateway_accounts.usable_accounts(conn, asker, row.worker_id, prefix)
    if not usable.accounts:
        return None, usable.why(prefix)
    return prefix, ""


async def _placed(conn: AsyncConnection, row, model: str | None, agent_version: str | None):
    """(model, gateway prefix, why the model was not taken) of ``model`` for run ``row``."""
    if model is None:
        return None, None, ""
    if model_router.is_gateway_model(model):
        prefix, why = await _gateway_refusal(conn, row, model, agent_version)
        if prefix is not None or (row.runtime == "opencode" and gateway_runs.split(model) is None):
            return model, prefix, ""  # opencode's own <provider>/<model> runs on its own sign-in
        return None, None, why
    if row.runtime != NATIVE_RUNTIME:
        return None, None, f"{model} is Claude Code's model, so {row.runtime} keeps its own"
    return model, None, ""


async def _taken(conn: AsyncConnection, row, routing: Mapping, difficulty: str, choice: Choice, agent_version):
    """(model, prefix, branch, reason) the run takes of ``choice``: the ladder's model when the run cannot take it, and
    no model when it can take neither or its profile does not allow it."""
    model, prefix, why = await _placed(conn, row, choice.model, agent_version)
    branch, reason = choice.branch, choice.reason
    if model is None and why and choice.branch == model_router.OVERFLOW:
        ladder = routing["ladder"][difficulty]
        model, prefix, also = await _placed(conn, row, ladder, agent_version)
        branch, reason = model_router.LADDER, f"{reason}; {why}, so the ladder's {ladder}"
        why = also
    if model is None and why:
        reason = f"{reason}; {why}"
    allowed = profiles.allowed(_permissions(row), "model")
    if model is not None and allowed is not None and model not in allowed:
        model, prefix, reason = None, None, f"{reason}; the run's agent profile does not allow {model}"
    return model, prefix, branch, reason


# The decision


async def _agent_version(conn: AsyncConnection, worker_id: int | None) -> str | None:
    if worker_id is None:
        return None
    w = tables.workers
    return (await conn.execute(select(w.c.agent_version).where(w.c.id == worker_id))).scalar_one_or_none()


async def _routing_of(conn: AsyncConnection, row) -> dict | None:
    if row.model or row.kind not in model_router.ROUTED_KINDS or row.runtime not in model_router.ROUTED_RUNTIMES:
        return None
    if model_router.pinned_by_profile(profiles.allowed(_permissions(row), "model")):
        return None
    from evo_agents.hub.server.autonomy.service.policies import (
        effective_policy,  # it imports the Loop, which reads runs
    )

    return model_router.routing_section(await effective_policy(conn, row.project_id))


async def _pending(conn: AsyncConnection, row, routing: Mapping, prior: str | None, quota: Quota, version) -> dict:
    """The record of a run the worker's scorer rates: the model of each difficulty, the medium one leased first."""
    floor = model_router.escalate(prior) if prior is not None else None
    choices, prefixes = {}, {}
    for level in model_router.LEVELS:
        rated = model_router.at_least(level, floor)
        model, prefix, _, _ = await _taken(
            conn, row, routing, rated, model_router.choose(routing, rated, quota), version
        )
        if model is not None:
            choices[level], prefixes[level] = model, prefix
    first = model_router.at_least(model_router.FALLBACK, floor)
    return model_router.record(
        routing["scorer"],
        None,
        choices.get(first),
        source="scorer",
        reason="the rules could not rate the prompt, so the worker's scorer rates it",
        strategy=routing.get("strategy"),
        escalated_from=prior if prior in (model_router.EASY, model_router.MEDIUM) else None,
        gateway=prefixes.get(first),
        quota=quota.view(),
        pending=True,
        choices=choices or None,
    )


async def decide(conn: AsyncConnection, run_id: int, plan: Mapping | None) -> dict | None:
    """Rate leased run ``run_id`` of ``plan`` (the plan's body it was dispatched at) and give it the strategy's model,
    as the module says: the routing its claim carries, None when it is not routed or takes no model of the strategy."""
    row = (await conn.execute(_run_query(run_id))).one()
    routing = await _routing_of(conn, row)
    if routing is None:
        return None
    rating = await _rating(conn, row, plan)
    prior = await _failed_before(conn, row)
    floor = model_router.escalate(prior) if prior is not None else None
    quota, version = await _quota_of(conn, row), await _agent_version(conn, row.worker_id)
    if rating.difficulty is None and routing["scorer"] != model_router.SCORER_OFF:
        found = await _pending(conn, row, routing, prior, quota, version)
    else:
        if rating.difficulty is None:
            rating = Rating(None, "fallback", "the rules could not rate the prompt and the scorer is off")
        difficulty = model_router.at_least(rating.difficulty or model_router.FALLBACK, floor)
        choice = model_router.choose(routing, difficulty, quota)
        model, prefix, branch, reason = await _taken(conn, row, routing, difficulty, choice, version)
        found = model_router.record(
            routing["scorer"],
            difficulty,
            model,
            source=rating.source,
            reason=f"{rating.reason}; {reason}",
            strategy=routing.get("strategy"),
            branch=branch,
            escalated_from=prior if difficulty != (rating.difficulty or model_router.FALLBACK) else None,
            gateway=prefix,
            quota=quota.view(),
        )
    r = tables.runs
    await conn.execute(update(r).values(routing=found, gateway_provider=found.get("gateway")).where(r.c.id == run_id))
    return claim_of(found)


def claim_of(found: Mapping) -> dict | None:
    """The routing a claim carries of the hub's record ``found``: every rung the chosen model, or the choices while the
    scorer rates the run; None when the run takes no model of the strategy."""
    hub = {key: found.get(key) for key in ("source", "reason", "strategy", "branch", "escalated_from")}
    if found.get("pending"):
        choices = found.get("choices") or {}
        if any(level not in choices for level in model_router.LEVELS):
            return None
        return {"scorer": found["scorer"], "ladder": dict(choices), "pending": True, **hub}
    model = found.get("model")
    if not isinstance(model, str):
        return None
    ladder = dict.fromkeys(model_router.LEVELS, model)
    return {"scorer": found["scorer"], "ladder": ladder, "difficulty": found["difficulty"], **hub}


# The worker's reports


async def reported(conn: AsyncConnection, run_id: int, routing: dict) -> dict:
    """``routing``, as the worker of run ``run_id`` reported it, with what the hub rated kept; when the hub left the
    rating to the scorer and the worker took another of its choices, the run's gateway prefix becomes that model's."""
    r = tables.runs
    row = (await conn.execute(select(r.c.routing).where(r.c.id == run_id))).first()
    stored = row.routing if row is not None and isinstance(row.routing, Mapping) else None
    if stored is None or "strategy" not in stored:  # the worker's own rating, as before the hub rated runs
        return routing
    merged = {**{key: value for key, value in stored.items() if key in model_router.HUB_KEYS}, **routing}
    for key in model_router.HUB_KEYS:  # a key the worker left null keeps the hub's
        if routing.get(key) is None and stored.get(key) is not None:
            merged[key] = stored[key]
    held = stored.get("choices")
    choices = held if isinstance(held, Mapping) else {}
    model = routing.get("model")
    if stored.get("pending"):
        merged["pending"] = False
    if stored.get("pending") and model in choices.values():
        split = gateway_runs.split(model)  # a choice of a prefix the gateway serves went through it at the claim
        merged["gateway"] = split[0] if split is not None else None
        if merged["gateway"] != stored.get("gateway"):
            await conn.execute(update(r).values(gateway_provider=merged["gateway"]).where(r.c.id == run_id))
    return {key: value for key, value in merged.items() if key not in model_router.HUB_KEYS or value is not None}


# The handoff


async def recommend(conn: AsyncConnection, project_id: int, owner_id: int, plan: Mapping, step_key: str) -> dict:
    """The difficulty of step ``step_key`` of ``plan`` and the model the project's Token strategy gives it now, for
    its handoff (``plans.handoff``): the ladder's while routing is off; the quota is member ``owner_id``'s."""
    step = _step_of(plan, step_key)
    if step is None:
        return {"difficulty": None, "recommended_model": None, "routing_reason": None}
    rating = model_router.rate_step(step, plan)
    level = rating.difficulty or model_router.FALLBACK  # the rules rate every step
    from evo_agents.hub.server.autonomy.service.policies import effective_policy

    policy = await effective_policy(conn, project_id)
    routing = model_router.routing_section({"routing": {**policy.get("routing", {}), "enabled": True}})
    if routing is None:
        return {"difficulty": level, "recommended_model": None, "routing_reason": rating.reason}
    choice = model_router.choose(routing, level, await native_quota(conn, owner_id, False, None))
    return {
        "difficulty": level,
        "recommended_model": choice.model,
        "routing_reason": f"{rating.reason}; {choice.reason}",
    }
