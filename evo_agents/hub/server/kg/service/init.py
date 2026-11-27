"""A project's kg_init runs (``evo_agents.hub.server.kg_init`` says what they do): the phases ``advance`` moves a run
through, queueing the review run, recording the Curator's decision, and starting and listing the runs. Each phase and
each request opens its own transactions with UnitOfWork; the queries are ``kg.repository.init``'s and
``evo_agents.hub.kg_init_db``'s. What reads the graph (the starter measure, the questions run's phase) is handed in by
the route module."""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace

import psycopg
from fastapi import HTTPException
from sqlalchemy import func, null
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, kg_init, kg_init_db, kg_settings, runs
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build import QueueBusy, queue_build
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg import built_graphs
from evo_agents.hub.server.kg.models.init import (
    KG_INIT,
    MAX_LISTED,
    QUEUE_WAIT,
    WITHHELD,
    InitAll,
    InitFailed,
    InitRun,
    InitRuns,
    Measure,
    Phase,
)
from evo_agents.hub.server.kg.models.questions import Count
from evo_agents.hub.server.kg.repository import init as repo
from evo_agents.hub.server.kg.repository.init import asker_of
from evo_agents.hub.server.kg.service.questions import OwnerReads
from evo_agents.hub.server.kg.service.questions import shown as questions_shown
from evo_agents.hub.server.kg_audience import agent_audience
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.tables_kg import INIT_ACTIVE
from evo_agents.kg import entities, init_run
from evo_agents.kg import settings as tuning
from evo_agents.kg import strategy as kg_strategy

log = logging.getLogger("evo_agents.hub.server.kg_init")  # the name its log lines had before the layers


# Measuring


async def starter_view(engine: AsyncEngine, row, graphs_tried: int, owner_reads: OwnerReads, latest_set) -> tuple:
    """(the latest ``graphs_tried`` graphs of ``row``'s project, its owner's access, the agents' sink, the project's
    latest eval set, the owner's audience, None), or (graphs, None, None, None, None, why the graph cannot be read so),
    in one transaction; ``owner_reads`` is kg_health's, ``latest_set`` kg_eval_sets'."""
    async with UnitOfWork(engine) as conn:
        graphs = await built_graphs(conn, row.project_id, graphs_tried)
        access, sink, why = await owner_reads(conn, row, graphs, agent=True)
        if why is not None:
            return graphs, None, None, None, None, why
        found_set = await latest_set(conn, row.project_id)
        audience = await agent_audience(conn, row.owner_id, None, None)
    return graphs, access, sink, found_set, audience, None


# The review run


async def _last_optimize(conn: AsyncConnection, project_id: int) -> dict | None:
    """What the newest ended KG optimize run measured before and after, for the Curator to decide keep or revert."""
    row = await repo.last_optimize(conn, project_id)
    if row is None or not isinstance(row.body, dict):
        return None
    body = row.body
    handed = body.get("handover") or {}
    changes = [f"run #{row.id} ended {row.state}"]
    for key in ("semantic", "asked_weight", "regressions", "decisions", "pulls"):
        if handed.get(key):
            changes.append(f"{key}: {handed[key]}")
    return {"before": handed.get("before"), "after": handed.get("after"), "changes": changes}


def _coverage_text(coverage) -> dict:
    """The build's coverage for a prompt: counts; the text of any error or warning, which can name an item, is withheld."""
    found = coverage if isinstance(coverage, dict) else {}
    return {
        "errors": [WITHHELD] * int(len(found.get("errors") or [])),
        "warnings": [WITHHELD] * int(len(found.get("warnings") or [])),
        "dangling_edges": found.get("dangling_edges", 0),
    }


async def queue_repoless(
    conn: AsyncConnection, project_id: int, project: str, who: int, title: str, notes: list, *, repos: bool = True
):
    """Queue a review run of ``project`` titled ``title``, dispatched by user ``who``, on its KG pool
    (``night_duty.kg_duty``) under the charter's review cost cap, with no repo but those the node holds (none with
    ``repos`` False): (its id, its caps), or None with why in ``notes``."""
    from evo_agents.hub.server.collect import review_place
    from evo_agents.hub.server.night_duty import kg_duty, pool_empty
    from evo_agents.hub.server.runs.service.dispatch import insert_run

    charter = await repo.charter_body(conn, project_id) or {}
    due = SimpleNamespace(project_id=project_id, project=project)
    duty, why = await kg_duty(conn, due)
    if duty is None:
        notes.append(f"no review run: {why}")
        return None
    runtime = (charter.get("reviewer") or {}).get("runtime") or curator.DEFAULT_RUNTIME
    found, problems = await review_place(conn, due, duty, runtime, repoless=True)
    if problems and duty.shared:  # no node of the shared KG pool fits it: the agent's pool is still empty
        await pool_empty(conn, project_id)
    if problems:
        notes.append("no review run: " + "; ".join(problems))
        return None
    caps = kg_init.budget(charter)
    if caps["max_usd"] < curator.MIN_RUN_USD:
        notes.append("no review run: the review cost cap leaves no money")
        return None
    values = {
        "kind": "review",
        "project_id": project_id,
        "plan_id": None,
        "title": title,
        "plan_revision": None,
        "dispatched_by": who,
        "dispatched_via": "machine",
        "pinned_worker_id": None,
        "selector": duty.columns()["selector"],
        "tolerations": duty.columns()["tolerations"],
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": (charter.get("reviewer") or {}).get("model"),
        "mode": "headless",
        "approval": "auto",
        "timeout_s": caps["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "repos": (found if repos else None) or null(),  # SQL NULL: no repo (schema 0202)
        "budget": caps,
    }
    try:
        async with conn.begin_nested():
            run_id = await insert_run(conn, values)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):
            notes.append("no review run: another review run of the project is active")
            return None
        raise
    return run_id, caps


async def queue_agent(
    conn: AsyncConnection, ctx, body: dict, measures: dict | None, lens: str = "kg_init"
) -> int | None:
    """Queue the review run of ``ctx`` through ``lens`` (kg_init, or kg_questions for the run that writes its starter
    questions first); its id, or None with why in ``body["notes"]``."""
    from evo_agents.hub.server.run_state import notify_queued, write_event

    who = await asker_of(conn, ctx)
    if who is None:
        body.setdefault("notes", []).append("no review run: the project has no admin to dispatch it as")
        return None
    title = f"KG init of {ctx.project}" if lens == "kg_init" else f"KG starter questions of {ctx.project}"
    queued = await queue_repoless(conn, ctx.project_id, ctx.project, who[0], title, body.setdefault("notes", []))
    if queued is None:
        return None
    run_id, caps = queued
    text = (
        f"Queued for KG init run #{ctx.id} of project {ctx.project}, {kg_init.CALLED.format(who=who[1])}: lens "
        f"{lens}; cost cap {curator.money(caps['max_usd'])}, {caps['max_turns']} turns, "
        f"{caps['max_seconds'] // 60} minutes of agent time."
    )
    await write_event(conn, run_id, {"text": text, "init_run_id": ctx.id, "lenses": [lens], **caps})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=who[0],
        token_id=None,
        action=KG_INIT,
        target=f"{ctx.project} run:{run_id} init={ctx.id}" + ("" if lens == "kg_init" else f" lens={lens}"),
        project_id=ctx.project_id,
    )
    return run_id


# The job


async def _measures(engine: AsyncEngine, ctx, body: dict, now: datetime, job: SimpleNamespace) -> tuple[bool, dict]:
    """(whether the run still waits for the run that writes its starter questions, which the project may need first
    (``kg_questions.phase``), its body), the body with ``measures`` filled once it does not wait."""
    async with UnitOfWork(engine) as conn:
        asker = await asker_of(conn, ctx)
    row = SimpleNamespace(
        project_id=ctx.project_id,
        project=ctx.project,
        owner_id=asker[0] if asker else None,
        owner=asker[1] if asker else None,
    )
    waits, body = await job.phase(
        engine,
        ctx,
        body,
        row if asker else None,
        now,
        blobs=job.blobs,
        data_dir=job.data_dir,
        count=job.count,
        queue=queue_agent,
    )
    if waits:
        return True, body
    found = (
        await job.measure(engine, row, job.blobs, job.data_dir) if asker else {"why": "no admin to read the graph as"}
    )
    if found.get("why"):
        body["measures"] = {}
        body.setdefault("notes", []).append(f"no starter figures: {found['why']}")
    else:
        body["measures"] = {"words": found.get("words"), "meaning": found.get("meaning")}
    return False, body


async def _queued(engine: AsyncEngine, ctx, now: datetime, job: SimpleNamespace) -> str:
    """A run not yet given to an agent: wait for the run that writes its starter questions when the project needs one
    (``kg_questions.phase``), measure, write the prompt, queue the review run."""
    async with UnitOfWork(engine) as conn:
        latest = await repo.latest_build(conn, ctx.project_id)
    if latest is None:
        raise InitFailed("the project has no successful build to profile")
    profile = ctx.profile or (latest.coverage or {}).get("profile")
    if not isinstance(profile, dict) or not profile:
        raise InitFailed("the latest build holds no data profile: build the project again")
    body = dict(ctx.body or {})
    if body.get("measures") is None:
        waits, body = await _measures(engine, ctx, body, now, job)
        if waits:
            return "questions"
    async with UnitOfWork(engine) as conn:
        settings = await kg_settings.read(conn, ctx.project_id)
        optimize = await _last_optimize(conn, ctx.project_id)
        body["prompt"] = kg_init.prompt(
            project=ctx.project,
            run_id=ctx.id,
            why=list(ctx.why or []),
            profile=profile,
            coverage=_coverage_text(latest.coverage),
            strategies=[s.to_json() for s in kg_strategy.builtins().values()],
            measures=body["measures"] or None,
            settings=settings.to_json(),
            optimize=optimize,
        )
        body["notes"] = [n for n in body.get("notes") or [] if not n.startswith("no review run")]
        agent_run_id = await queue_agent(conn, ctx, body, body["measures"])
        if agent_run_id is None:
            if ctx.created_at + QUEUE_WAIT < now:
                raise InitFailed("no review run could be queued in " + str(QUEUE_WAIT))
            await repo.set_run(conn, ctx.id, body=body, build_id=latest.id, profile=profile)
            return "waiting"
        await repo.set_run(
            conn, ctx.id, state="running", body=body, build_id=latest.id, profile=profile, agent_run_id=agent_run_id
        )
    return "queued"


async def _running(engine: AsyncEngine, ctx) -> str:
    async with UnitOfWork(engine) as conn:
        state = await repo.run_state(conn, ctx.agent_run_id) if ctx.agent_run_id else None
        if state in runs.ACTIVE_STATES:
            return "running"
        body = dict(ctx.body or {})
        decided = bool(body.get("decision"))
        error = None if decided else f"the review run ended ({state or 'gone'}) without a decision"
        await _end(conn, ctx, body, "done" if decided else "failed", error)
    return "done" if decided else "failed"


async def _end(conn: AsyncConnection, ctx, body: dict, state: str, error: str | None) -> None:
    text = kg_init.report(ctx.project, ctx.id, list(ctx.why or []), state, body, error)
    await repo.set_run(
        conn,
        ctx.id,
        state=state,
        body=body,
        report=text,
        error=error and error[: kg_init.ERROR_CHARS],
        ended_at=func.now(),
    )


async def advance(
    engine: AsyncEngine,
    *,
    measure: Measure,
    phase: Phase,
    blobs=None,
    data_dir=None,
    now: datetime | None = None,
    count: Count | None = None,
) -> dict:
    """Move each kg_init run in flight one phase on, each on its own; how many did what. ``measure`` is the starter
    measure, ``phase`` the questions run's (``kg_questions.phase``), ``count`` what it reads of the graph."""
    job = SimpleNamespace(measure=measure, phase=phase, blobs=blobs, data_dir=data_dir, count=count)
    async with UnitOfWork(engine) as conn:
        moment = now or await repo.now(conn)
        rows = await repo.active_contexts(conn)
    outcomes: dict[str, int] = {}
    for ctx in rows:
        try:
            if ctx.state == "queued":
                word = await _queued(engine, ctx, moment, job)
            else:
                word = await _running(engine, ctx)
        except InitFailed as exc:
            await _fail(engine, ctx, str(exc))
            word = "failed"
        except Exception as exc:  # one run failing leaves the others their turn
            log.exception("a kg init run failed", extra={"project": ctx.project, "init_run_id": ctx.id})
            await _fail(engine, ctx, f"{type(exc).__name__}: {exc}")
            word = "failed"
        outcomes[word] = outcomes.get(word, 0) + 1
    return dict(sorted(outcomes.items()))


async def _fail(engine: AsyncEngine, ctx, error: str) -> None:
    async with UnitOfWork(engine) as conn:
        body = dict(ctx.body or {})
        body.setdefault("notes", []).append(f"failed: {error}")
        await _end(conn, ctx, body, "failed", error)


# The decision


async def _strategy_values(chosen: dict) -> tuple[dict, dict | None, str]:
    """(settings to set, what stays pending, a name for the reason) of the strategy part of a decision."""
    if "use" in chosen:
        builtin = kg_strategy.builtins().get(chosen["use"])
        if builtin is None:
            raise HTTPException(
                422, f"no built-in strategy named {chosen['use']!r}; known: {', '.join(sorted(kg_strategy.builtins()))}"
            )
        return {**builtin.settings, "strategy": builtin.name}, None, builtin.name
    made = kg_strategy.parse(chosen["create"], origin="created by the Curator")
    pending = {
        "name": made.name,
        "identifiers": list(made.identifiers),
        "ontology": made.ontology,
        "steps": list(made.steps),
        "file": chosen["create"],
    }
    return dict(made.settings), pending, made.name


def _reasons(decision: dict, *about: str) -> tuple[str, dict]:
    found = [r for r in decision.get("reasons") or [] if r["about"] in about]
    why = "; ".join(r["why"] for r in found)
    figures: dict = {}
    for r in found:
        figures.update({f"{r['about']}.{k}": v for k, v in r["figures"].items()})
    return why, figures


async def apply_decision(conn: AsyncConnection, access, user: Principal, row, decision: dict) -> dict:
    """Apply a validated ``decision`` to the project on the hub: the strategy's and the settings' values in one change of
    the settings' history, a rebuild queued when one is needed; what each part did, by part."""
    applied: dict = {}
    values: dict = {}
    chosen = decision.get("strategy")
    if chosen:
        values, pending, name = await _strategy_values(chosen)
        applied["strategy"] = {"name": name, "settings": {k: v for k, v in values.items()}}
        if pending:
            applied["strategy"]["pending"] = pending
    if decision.get("settings"):
        values = {**values, **decision["settings"]}
        applied["settings"] = dict(decision["settings"])
    if values:
        why, figures = _reasons(decision, "strategy", "settings")
        try:
            before, after, made = await kg_settings.change(
                conn,
                access.project_id,
                values,
                user_id=user.user_id,
                run_id=user.acts_for_run,
                reason=f"kg init #{row.id}: {why}"[:500],
                figures=figures or None,
            )
        except tuning.SettingsError as exc:
            raise HTTPException(422, f"settings: {exc}") from None
        needs = tuning.rebuild_keys(before, after)
        applied["change"] = {"id": made, "needs_rebuild": needs}
        if needs:
            try:
                build_id, _ = await queue_build(JobQueue.in_transaction(), conn, access.project_id, access.name, None)
            except QueueBusy:
                build_id = None
            applied["change"]["build_id"] = build_id
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.KG_SETTINGS,
            target=f"{access.name} change={made} kind=set init={row.id}",
            project_id=access.project_id,
        )
    for part, how in (
        ("sources", "file each source with kg_propose (kind kg_source): the Judge checks it, the build reads it"),
        ("code_changes", "file each with kg_propose or `evo-agents hub curator propose --lens kg_init`"),
    ):
        if decision.get(part):
            applied[part] = {"through": "the proposal flow", "how": how, "items": decision[part]}
    return applied


async def record_decision(engine: AsyncEngine, user: Principal, project: str, run_id: int, data) -> dict:
    """Validate and apply ``data``, the decision of run ``run_id``, in one transaction; the run's row as a dict of what
    was applied."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if not holds(access.role, "writer"):
            raise HTTPException(403, f"deciding a kg_init run of project {access.name} needs the writer role on it")
        row = await repo.run_for_update(conn, access.project_id, run_id)
        if row is None:
            raise HTTPException(404, f"project {access.name} has no kg_init run {run_id}")
        if row.state not in INIT_ACTIVE:
            raise HTTPException(409, f"kg_init run {run_id} is {row.state}: it takes no decision")
        acting = user.acts_for_run
        if acting is not None and acting != row.agent_run_id:
            raise HTTPException(403, f"run {acting} is not the review run of kg_init run {run_id}")
        body = dict(row.body or {})
        if body.get("decision"):
            raise HTTPException(409, f"kg_init run {run_id} has a decision already")
        current = await kg_settings.read(conn, access.project_id)
        try:
            decision = init_run.parse_decision(
                data, prose=init_run.mostly_prose(row.profile), entities_now=entities.enabled(current) is not None
            )
        except init_run.DecisionError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            applied = await apply_decision(conn, access, user, row, decision)
        except kg_strategy.StrategyError as exc:
            raise HTTPException(422, str(exc)) from None
        body["decision"] = decision
        body["applied"] = applied
        await repo.set_run(conn, run_id, body=body)
    return {"run": run_id, "applied": applied}


# Starting and listing


def shown(row, project: str) -> InitRun:
    body = row.body if isinstance(row.body, dict) else {}
    return InitRun(
        id=row.id,
        project=project,
        via=row.via,
        state=row.state,
        why=list(row.why or []),
        build_id=row.build_id,
        agent_run_id=row.agent_run_id,
        questions=questions_shown(body),
        summary=kg_init.summary(project, row.state, body),
        decisions={"decision": body.get("decision") or {}, "applied": body.get("applied") or {}},
        notes=[str(n) for n in body.get("notes") or []],
        report=row.report,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        ended_at=row.ended_at,
    )


async def _begin(conn: AsyncConnection, user: Principal, access, why_all: bool):
    """Queue a manual run for ``access``'s project; (the run, started), HTTPException 409 without a build or a config."""
    found = await kg_init_db.active_of(conn, access.project_id)
    if found is not None:
        return found, False
    if not await repo.has_config(conn, access.project_id):
        raise HTTPException(409, f"project {access.name} has no knowledge config on the hub; nothing was started")
    latest = await repo.latest_build(conn, access.project_id)
    profile = ((latest.coverage or {}).get("profile") if latest else None) or None
    row, made = await kg_init_db.create(
        conn,
        access.project_id,
        "manual",
        kg_init.why_manual(user.login, why_all),
        requested_by=user.user_id,
        build_id=None if latest is None else latest.id,
        profile=profile,
    )
    if made:
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=KG_INIT,
            target=f"{access.name} init:{row.id}",
            project_id=access.project_id,
        )
        log.info("kg init run asked for", extra={"project": access.name, "login": user.login, "id": row.id})
    return row, made


async def start(engine: AsyncEngine, user: Principal, project: str) -> tuple[InitRun, bool]:
    """Start a kg_init run of ``project`` for ``user`` (a writer): (the run, True), or (the run in flight, False)."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if not holds(access.role, "writer"):
            raise HTTPException(403, f"asking for a kg_init run of project {access.name} needs the writer role on it")
        row, made = await _begin(conn, user, access, False)
    return shown(row, access.name), made


async def start_all(engine: AsyncEngine, user: Principal) -> InitAll:
    """Queue a run for every project with a knowledge config that has none in flight (an admin's), in one
    transaction."""
    out = InitAll(started=[], skipped={})
    async with UnitOfWork(engine) as conn:
        for name in await repo.project_names(conn):
            access = await project_access(conn, user, name)
            try:
                _, made = await _begin(conn, user, access, True)
            except HTTPException as exc:
                out.skipped[name] = str(exc.detail)
                continue
            if made:
                out.started.append(name)
            else:
                out.skipped[name] = "a run is in flight"
    return out


async def listing(engine: AsyncEngine, user: Principal, project: str, limit: int = MAX_LISTED) -> InitRuns:
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the kg_init runs of project {access.name} needs a grant on it")
        rows = await repo.runs_of(conn, access.project_id, limit)
    shown_runs = [shown(row, access.name) for row in rows]
    return InitRuns(active=next((run for run in shown_runs if run.state in INIT_ACTIVE), None), runs=shown_runs)
