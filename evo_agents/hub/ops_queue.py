"""The queue of a project's operations events (plan hub-steward, criterion a7): it gathers the operations events of
the project by cause, so that one short run of the Orchestrator of the project's default system reads a batch of them
instead of a person reading each in the Inbox.

Config. The queue reads its config from the key ``ops`` of the spec of the project's default system (``ops_config``:
enabled, debounce_minutes, max_items; ``system_spec.ops_of``); a project without one, or whose spec has no ``ops``,
has its queue off. The first pass after the queue is turned on puts its cursor (ops_cursors) at the newest event of the
project, so the queue never reads what happened before; a pass while it is off drops the cursor.

Queue. Each pass (``take``, in the reaper's pass every minute) reads the project's hub_events past its cursor, as the
Loop does, and keeps the operations events (``hub_events.OPS``). An event a rule of tier 0 handles (``Rule``, the
rules a later step passes in) goes no further; an event that tells of nothing to do (``cause_of`` gives None: a run
parked for an answer, whose ``decision.opened`` is the item, a worker whose health came back ok, and one whose only
fault is the login of a runtime its runs do not use or that the hub leases them an account of: ``idle_logins``) goes
nowhere. Any other becomes an item of the open batch of its cause key (``cause_key``: kind, cause, and the worker or
the plan, as ``inbox.fold_same_cause`` folds failed runs, or the machine for a worker's low disk alone, which the
workers of one machine share), which the first item opens and which closes
``debounce_minutes`` after it opened, or at once when it holds ``max_items`` pending items. An item tier 0 handled
before its batch closed leaves it (``settle``); a batch that closes with no pending item left is ``empty`` and opens
no run, one with some is ``ready`` for the step that opens the Orchestrator's run. Nothing here calls a model.

SQLAlchemy Core on ``evo_agents.hub.tables``; nothing here imports ``evo_agents.hub.server``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, case, delete, exists, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, system_spec, tables, triage_rules
from evo_agents.hub.account_providers import MODEL_ENV_VARS
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.gateway_runs import GATEWAY_RUNTIMES, PICK_PREFIX
from evo_agents.hub.ops_events import LOGGED_IN

BATCH_STATES = ("open", "ready", "empty", "running", "done")  # 0240; running and done are the run's (a later step)
CLOSED = ("ready", "empty", "running", "done")
ITEM_STATES = ("pending", "handled")
# The variables of a hub account that log each runtime in: a run leased one authenticates with it, whatever the
# machine's own login is. opencode reads the key of each provider it knows from that provider's variable (zhipu-glm
# sets ZHIPU_API_KEY). A provider account, reached through the gateway, logs every runtime of the gateway in.
LEASE_VARS: dict[str, tuple[str, ...]] = {
    "claude-code": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "codex": ("OPENAI_API_KEY",),
    "opencode": ("ZHIPU_API_KEY", "ZAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
}
LEASE_ENV_VARS = tuple(sorted({name for names in LEASE_VARS.values() for name in names}))
LEASE_DAYS = 7  # the days a run that took such an account speaks for its worker
# The kinds whose event names a run, which ``cause_of`` skips when the run is one of an ops thread.
RUN_KINDS = (hub_events.RUN_ENDED, hub_events.RUN_PARKED, hub_events.DECISION_OPENED)
# The failure causes of the worker's machine, not of the work: their runs gather by worker, not by plan.
WORKER_CAUSES = (
    "checkout",
    "worker_stopped",
    "worker_revoked",
    "lease_lost",
    "network",
    "hub_5xx",
    "account_auth",
    "rate_limit",
    "runtime",
    "environment",
    "missing_tool",
    "os_permission",
)
TAKE_LIMIT = hub_events.MAX_READ  # events one pass reads of a project

# A rule of tier 0: given the connection, the project and an operations event, True when it handled the event, which
# then becomes no item.
Rule = Callable[[AsyncConnection, int, hub_events.HubEvent], Awaitable[bool]]


@dataclass(frozen=True)
class Cause:
    """What an operations event is about: its cause key's parts, and the ids an item keeps."""

    kind: str
    cause: str
    subject: str
    run_id: int | None = None
    decision_id: int | None = None
    worker_id: int | None = None

    @property
    def key(self) -> str:
        return cause_key(self.kind, self.cause, self.subject)


def cause_key(kind: str, cause: str, subject: str) -> str:
    """The key the items of one batch share in a project: the event's kind, its cause, and the worker or the plan."""
    return f"{kind}|{cause}|{subject}"


def _subject(worker_id, plan_id: str | None, cause: str) -> str:
    if cause in WORKER_CAUSES or not plan_id:
        return f"worker:{worker_id}" if worker_id is not None else "-"
    return f"plan:{plan_id}"


def health_causes(payload: dict) -> list[str]:
    """What a ``worker.health`` event says is wrong: ``disk_low``, and ``login:<runtime>:<state>`` for each runtime
    whose login is neither ok nor leased (``ops_events.LOGGED_IN``); empty when the worker is healthy."""
    found = ["disk_low"] if payload.get("disk") == "low" else []
    logins = payload.get("logins") or {}
    found += [f"login:{name}:{state}" for name, state in sorted(logins.items()) if state not in LOGGED_IN]
    return found


async def cause_of(conn: AsyncConnection, event: hub_events.HubEvent, project_id: int | None = None) -> Cause | None:
    """The cause of operations event ``event`` of project ``project_id``, or None when it tells of nothing to do: a
    run parked for an answer (its ``decision.opened`` is the item), an event of a run of an ops thread (``ops_run``), a
    worker whose health is ok, or whose only fault is the login of a runtime no run there uses (``idle_logins``), or a
    kind that is no operations event."""
    payload = event.payload or {}
    kind = event.kind
    if kind in RUN_KINDS and await ops_run(conn, payload.get("run_id")):
        return None
    if kind == hub_events.RUN_ENDED:
        run_id = payload.get("run_id")
        cause = payload.get("failure_cause") or "agent"
        plan_id = await _plan_of(conn, run_id)
        subject = _subject(payload.get("worker_id"), plan_id, cause)
        return Cause(kind, cause, subject, run_id=run_id, worker_id=payload.get("worker_id"))
    if kind == hub_events.DECISION_OPENED:
        run_id = payload.get("run_id")
        plan_id = await _plan_of(conn, run_id)
        subject = f"plan:{plan_id}" if plan_id else f"user:{payload.get('asked_of')}"
        return Cause(
            kind, str(payload.get("category") or "-"), subject, run_id=run_id, decision_id=payload.get("decision_id")
        )
    if kind == hub_events.CURATOR_PAUSED:
        return Cause(kind, str(payload.get("cause_class") or "unknown"), "curator")
    if kind == hub_events.WORKER_HEALTH:
        worker_id = payload.get("worker_id")
        wrong = health_causes(payload)
        if any(cause.startswith("login:") for cause in wrong):
            idle = await idle_logins(conn, worker_id, project_id)
            wrong = [cause for cause in wrong if not (cause.startswith("login:") and cause.split(":")[1] in idle)]
        if not wrong:
            return None
        subject = await _host(conn, worker_id) if wrong == ["disk_low"] else None
        return Cause(kind, ",".join(wrong), subject or f"worker:{worker_id}", worker_id=worker_id)
    return None


async def _host(conn: AsyncConnection, worker_id) -> str | None:
    """``host:<hostname>``, the subject of a worker's ``disk_low``: the workers of one machine share its drive, so their
    low disk is one batch, not one per worker; None for a worker whose machine name the hub does not know."""
    if not isinstance(worker_id, int):
        return None
    w = tables.workers
    hostname = await conn.scalar(select(w.c.hostname).where(w.c.id == worker_id))
    return f"host:{hostname}" if hostname else None


async def ops_run(conn: AsyncConnection, run_id) -> bool:
    """Whether run ``run_id`` is a run of an ops thread (rule ``ops``): the Orchestrator reading a batch. Its own
    events are no item of the queue, or the decision it asks the owner would come back to it as a batch of its own."""
    if not isinstance(run_id, int):
        return False
    r, m, t = tables.runs, tables.chat_messages, tables.orchestrator_threads
    root = func.coalesce(m.c.thread_root_id, m.c.id)
    query = (
        select(r.c.id)
        .select_from(
            r.join(m, m.c.id == r.c.chat_message_id).join(
                t, and_(t.c.chat_id == r.c.chat_id, t.c.root_message_id == root)
            )
        )
        .where(r.c.id == run_id, t.c.rule == triage_rules.OPS)
        .limit(1)
    )
    return await conn.scalar(query) is not None


def lease_runtimes(env_var: str | None) -> set[str]:
    """The runtimes an account of variable ``env_var`` logs in (LEASE_VARS); every runtime of the gateway for a
    provider account picked by hand (run_accounts.env_var ``gateway:<prefix>``)."""
    if not env_var:
        return set()
    if env_var.startswith(PICK_PREFIX):
        return set(GATEWAY_RUNTIMES)
    return {runtime for runtime, names in LEASE_VARS.items() if env_var in names}


def _leases(env_var):
    """An env_var column names a variable of LEASE_VARS, or a provider account picked by hand."""
    return or_(env_var.in_(LEASE_ENV_VARS), env_var.startswith(PICK_PREFIX))


async def idle_logins(conn: AsyncConnection, worker_id, project_id: int | None) -> set[str]:
    """The runtimes whose login on worker ``worker_id`` no run of project ``project_id`` there depends on: those the
    worker does not offer (``unoffered``), and those the hub leases an account of to its runs, by what it would lease
    (``leasable_runtimes``) or what a run there took lately (``leased_runtimes``)."""
    if not isinstance(worker_id, int):
        return set()
    idle = await unoffered(conn, worker_id)
    idle |= await leasable_runtimes(conn, worker_id, project_id)
    return idle | await leased_runtimes(conn, worker_id)


def _offered(report) -> bool:
    """Whether a runtime's stored report says it takes runs (``runs.models.heartbeat.available``)."""
    if isinstance(report, dict):
        return report.get("available", True) is True
    return report is True or (isinstance(report, str) and bool(report))


async def unoffered(conn: AsyncConnection, worker_id: int) -> set[str]:
    """The runtimes of LEASE_VARS worker ``worker_id`` reports it cannot run (workers.runtimes): no run of them goes
    there, so their login is no matter (codex taken off a machine that has no login of it). Empty when it reported no
    runtime at all."""
    w = tables.workers
    reported = await conn.scalar(select(w.c.runtimes).where(w.c.id == worker_id))
    if not isinstance(reported, dict) or not reported:
        return set()
    return {name for name in LEASE_VARS if not _offered(reported.get(name))}


def _live(s):
    """A row of secrets ``s`` the hub leases: not deleted, and not past its end unless it signed in with OAuth, whose
    lease refreshes it first (``pool_accounts.usable_member``). Paused or cooling down it still counts: that is the
    account's state, no fault of the worker."""
    a = tables.accounts
    return and_(
        s.c.deleted_at.is_(None),
        or_(
            s.c.expires_at.is_(None),
            s.c.expires_at > func.now(),
            exists().where(a.c.secret_id == s.c.id, a.c.oauth_sealed.is_not(None)),
        ),
    )


def _reaches(b, worker_id: int):
    """A row of secret_bindings ``b`` reaches worker ``worker_id``: bound to any worker of its owner, to this one, or
    to its pool (``server.worker_placement.bound_on``, which this module does not import)."""
    w = tables.workers
    of_worker = select(w.c.pool_id).where(w.c.id == worker_id).scalar_subquery()
    return or_(
        and_(b.c.worker_id.is_(None), b.c.pool_id.is_(None)),
        b.c.worker_id == worker_id,
        and_(b.c.pool_id.is_not(None), b.c.pool_id == of_worker),
    )


async def leasable_runtimes(conn: AsyncConnection, worker_id: int, project_id: int | None) -> set[str]:
    """The runtimes the hub has an account of to lease the runs of project ``project_id`` on worker ``worker_id``,
    whether or not one ran there yet. On a node of an agent's pool (worker_pools.agent_id) its runs take their model
    account from the agent's account pool alone: its env accounts of a model variable and its provider accounts
    (``credentials.survey_run``, ``gateway_accounts.usable_accounts``). Any worker also gets the env secrets bound to
    the project that reach it, but for a model variable or a provider account only off an agent's node."""
    w, wp, ap = tables.workers, tables.worker_pools, tables.agent_profiles
    s, m, b = tables.secrets, tables.account_pool_members, tables.secret_bindings
    node = (
        await conn.execute(
            select(wp.c.agent_id, ap.c.account_pool_id)
            .select_from(w.outerjoin(wp, wp.c.id == w.c.pool_id).outerjoin(ap, ap.c.id == wp.c.agent_id))
            .where(w.c.id == worker_id)
        )
    ).first()
    if node is None:
        return set()
    model = or_(s.c.kind == PROVIDER_KIND, and_(s.c.kind == "env", s.c.env_var.in_(MODEL_ENV_VARS)))
    rows = []
    if node.account_pool_id is not None:
        of_pool = exists().where(m.c.secret_id == s.c.id, m.c.pool_id == node.account_pool_id)
        rows += (await conn.execute(select(s.c.kind, s.c.env_var).where(of_pool, _live(s), model))).all()
    if project_id is not None:
        bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id, _reaches(b, worker_id))
        leases = or_(s.c.kind == PROVIDER_KIND, and_(s.c.kind == "env", s.c.env_var.in_(LEASE_ENV_VARS)))
        query = select(s.c.kind, s.c.env_var).where(bound, _live(s), leases)
        if node.agent_id is not None:
            query = query.where(~model)
        rows += (await conn.execute(query.distinct())).all()
    found: set[str] = set()
    for row in rows:
        found |= set(GATEWAY_RUNTIMES) if row.kind == PROVIDER_KIND else lease_runtimes(row.env_var)
    return found


async def leased_runtimes(conn: AsyncConnection, worker_id, days: int = LEASE_DAYS) -> set[str]:
    """The runtimes whose runs on worker ``worker_id`` took a hub account (run_accounts) in the last ``days``: a run of
    one logs in with the account its lease gives, so the machine's own login of it is no fault of the worker."""
    if not isinstance(worker_id, int):
        return set()
    r, ra = tables.runs, tables.run_accounts
    query = (
        select(ra.c.env_var)
        .join_from(ra, r, r.c.id == ra.c.run_id)
        .where(r.c.worker_id == worker_id, ra.c.chosen_at >= func.now() - timedelta(days=days))
        .where(_leases(ra.c.env_var))
        .distinct()
    )
    found: set[str] = set()
    for name in (await conn.execute(query)).scalars():
        found |= lease_runtimes(name)
    return found


async def run_leased(conn: AsyncConnection, run_id, runtime: str | None) -> bool:
    """Whether run ``run_id`` took a hub account that logs ``runtime`` in (``lease_runtimes``), or ran through the
    gateway on a runtime of it."""
    if not isinstance(run_id, int) or runtime not in LEASE_VARS:
        return False
    if runtime in GATEWAY_RUNTIMES:
        r = tables.runs
        if await conn.scalar(select(r.c.gateway_provider.is_not(None)).where(r.c.id == run_id)):
            return True
    ra = tables.run_accounts
    names = (await conn.execute(select(ra.c.env_var).where(ra.c.run_id == run_id, _leases(ra.c.env_var)))).scalars()
    return any(runtime in lease_runtimes(name) for name in names)


async def _plan_of(conn: AsyncConnection, run_id) -> str | None:
    if not isinstance(run_id, int):
        return None
    r = tables.runs
    return await conn.scalar(select(r.c.plan_id).where(r.c.id == run_id))


# Config


@dataclass(frozen=True)
class OpsConfig:
    """The key ``ops`` of the spec of a project's default system (``system_spec.ops_of``), each value the default when
    the spec lacks it."""

    enabled: bool = False
    debounce_minutes: int = system_spec.OPS["debounce_minutes"][0]
    max_items: int = system_spec.OPS["max_items"][0]


def read_ops(spec: dict | None) -> OpsConfig:
    """The ops config a system spec holds: ops off for a spec with no ``ops``, a value out of bounds defaulted."""
    found = system_spec.ops_of(spec)
    return OpsConfig(found["enabled"], found["debounce_minutes"], found["max_items"])


def default_system():
    """The query of each project's default system (``agent_systems.is_default``): project_id, id, slug, spec."""
    s = tables.agent_systems
    return select(s.c.project_id, s.c.id, s.c.slug, s.c.spec).where(s.c.is_default)


async def default_system_spec(conn: AsyncConnection, project_id: int) -> dict | None:
    """The spec of the project's default system, or None when it has none."""
    query = default_system().where(tables.agent_systems.c.project_id == project_id)
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else row.spec


async def ops_config(conn: AsyncConnection, project_id: int) -> OpsConfig:
    """The ops config of the project: the key ``ops`` of its default system's spec; off when it has none."""
    return read_ops(await default_system_spec(conn, project_id))


# The queue


async def enqueue(conn: AsyncConnection, project_id: int, config: OpsConfig, event_id: int | None, cause: Cause) -> int:
    """Put ``cause`` as an item into the open batch of its key, opening one that closes ``debounce_minutes`` from now
    when there is none, and close the batch when it holds ``max_items`` pending items. The batch's id."""
    b, i = tables.ops_batches, tables.ops_items
    closes_at = func.now() + timedelta(minutes=int(config.debounce_minutes))
    opened = (
        pg_insert(b)
        .values(
            project_id=project_id,
            key=cause.key,
            kind=cause.kind,
            cause=cause.cause,
            subject=cause.subject,
            closes_at=closes_at,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "key"], index_where=b.c.state == "open")
        .returning(b.c.id)
    )
    batch_id = await conn.scalar(opened)
    if batch_id is None:
        open_one = select(b.c.id).where(b.c.project_id == project_id, b.c.key == cause.key, b.c.state == "open")
        batch_id = await conn.scalar(open_one.with_for_update())
    await conn.execute(
        insert(i).values(
            batch_id=batch_id,
            project_id=project_id,
            event_id=event_id,
            kind=cause.kind,
            cause=cause.cause,
            subject=cause.subject,
            run_id=cause.run_id,
            decision_id=cause.decision_id,
            worker_id=cause.worker_id,
        )
    )
    pending = await conn.scalar(select(func.count()).where(i.c.batch_id == batch_id, i.c.state == "pending"))
    if pending >= int(config.max_items):
        await close_batch(conn, batch_id)
    return batch_id


async def close_batch(conn: AsyncConnection, batch_id: int) -> str | None:
    """Close open batch ``batch_id``: ``ready`` with pending items left, ``empty`` without. Its new state, or None
    when it was not open."""
    b, i = tables.ops_batches, tables.ops_items
    left = select(func.count()).where(i.c.batch_id == batch_id, i.c.state == "pending").scalar_subquery()
    state = case((left > 0, "ready"), else_="empty")
    closing = update(b).values(state=state, closed_at=func.now()).where(b.c.id == batch_id, b.c.state == "open")
    done = await conn.execute(closing.returning(b.c.state))
    return done.scalar_one_or_none()


async def close_due(conn: AsyncConnection, project_id: int | None = None) -> dict[str, int]:
    """Close each open batch whose ``closes_at`` has come (of project ``project_id``, or of every project); how many
    became ready and how many empty."""
    b = tables.ops_batches
    due = select(b.c.id).where(b.c.state == "open", b.c.closes_at <= func.now())
    if project_id is not None:
        due = due.where(b.c.project_id == project_id)
    closed = {"ready": 0, "empty": 0}
    for batch_id in (await conn.execute(due.order_by(b.c.id).with_for_update(skip_locked=True))).scalars().all():
        state = await close_batch(conn, batch_id)
        if state:
            closed[state] += 1
    return closed


async def _ruled(conn: AsyncConnection, project_id: int, event: hub_events.HubEvent, rules: Sequence[Rule]) -> bool:
    for rule in rules:
        if await rule(conn, project_id, event):
            return True
    return False


async def _cursor(conn: AsyncConnection, project_id: int, enabled: bool) -> int | None:
    """The queue's cursor of the project, locked; None while its queue is off (the cursor dropped) or another pass
    holds it, and None too in the pass that turned it on, which puts it at the newest event."""
    c, e = tables.ops_cursors, tables.hub_events
    if not enabled:
        await conn.execute(delete(c).where(c.c.project_id == project_id))
        return None
    newest = select(func.coalesce(func.max(e.c.id), 0)).where(e.c.project_id == project_id).scalar_subquery()
    made = await conn.execute(
        pg_insert(c)
        .values(project_id=project_id, cursor=newest)
        .on_conflict_do_nothing(index_elements=["project_id"])
        .returning(c.c.project_id)
    )
    if made.first() is not None:
        return None
    locked = select(c.c.cursor).where(c.c.project_id == project_id).with_for_update(skip_locked=True)
    return await conn.scalar(locked)


async def take(conn: AsyncConnection, project_id: int, rules: Sequence[Rule] = ()) -> int:
    """Read the project's events past its queue's cursor and queue the operations events no rule of ``rules`` handled;
    move the cursor past what it read. How many items it queued; nothing while the queue is off."""
    config = await ops_config(conn, project_id)
    cursor = await _cursor(conn, project_id, config.enabled)
    if cursor is None:
        return 0
    events = await hub_events.after(conn, project_id, cursor, TAKE_LIMIT)
    queued = 0
    for event in events:
        if event.kind not in hub_events.OPS or await _ruled(conn, project_id, event, rules):
            continue
        cause = await cause_of(conn, event, project_id)
        if cause is None:
            continue
        await enqueue(conn, project_id, config, event.id, cause)
        queued += 1
    if events:
        c = tables.ops_cursors
        moved = update(c).values(cursor=events[-1].id, updated_at=func.now()).where(c.c.project_id == project_id)
        await conn.execute(moved)
    return queued


async def settle(
    conn: AsyncConnection,
    project_id: int,
    *,
    by: str,
    run_ids: Sequence[int] = (),
    decision_ids: Sequence[int] = (),
    worker_ids: Sequence[int] = (),
) -> int:
    """Mark handled, by ``by`` (a rule of tier 0), the pending items of the project's open batches that name one of
    ``run_ids``, ``decision_ids`` or ``worker_ids``: they leave their batch. How many."""
    b, i = tables.ops_batches, tables.ops_items
    named = [
        column.in_(list(ids))
        for column, ids in ((i.c.run_id, run_ids), (i.c.decision_id, decision_ids), (i.c.worker_id, worker_ids))
        if ids
    ]
    if not named:
        return 0
    open_batches = select(b.c.id).where(b.c.project_id == project_id, b.c.state == "open")
    done = await conn.execute(
        update(i)
        .values(state="handled", handled_by=by, handled_at=func.now())
        .where(and_(i.c.project_id == project_id, i.c.state == "pending", i.c.batch_id.in_(open_batches), or_(*named)))
    )
    return done.rowcount or 0


__all__ = [
    "BATCH_STATES",
    "CLOSED",
    "ITEM_STATES",
    "WORKER_CAUSES",
    "Cause",
    "OpsConfig",
    "Rule",
    "cause_key",
    "cause_of",
    "close_batch",
    "close_due",
    "default_system",
    "default_system_spec",
    "enqueue",
    "health_causes",
    "idle_logins",
    "lease_runtimes",
    "leasable_runtimes",
    "leased_runtimes",
    "ops_config",
    "ops_run",
    "read_ops",
    "run_leased",
    "settle",
    "take",
    "unoffered",
]
