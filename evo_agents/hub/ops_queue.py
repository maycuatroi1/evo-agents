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
parked for an answer, whose ``decision.opened`` is the item, and a worker whose health came back ok) goes nowhere. Any
other becomes an item of the open batch of its cause key (``cause_key``: kind, cause, and the worker or the plan, as
``inbox.fold_same_cause`` folds failed runs), which the first item opens and which closes ``debounce_minutes`` after it
opened, or at once when it holds ``max_items`` pending items. An item tier 0 handled before its batch closed leaves it
(``settle``); a batch that closes with no pending item left is ``empty`` and opens no run, one with some is ``ready``
for the step that opens the Orchestrator's run. Nothing here calls a model.

SQLAlchemy Core on ``evo_agents.hub.tables``; nothing here imports ``evo_agents.hub.server``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, case, delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, system_spec, tables
from evo_agents.hub.ops_events import LOGGED_IN

BATCH_STATES = ("open", "ready", "empty", "running", "done")  # 0240; running and done are the run's (a later step)
CLOSED = ("ready", "empty", "running", "done")
ITEM_STATES = ("pending", "handled")
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


async def cause_of(conn: AsyncConnection, event: hub_events.HubEvent) -> Cause | None:
    """The cause of operations event ``event``, or None when it tells of nothing to do: a run parked for an answer
    (its ``decision.opened`` is the item), a worker whose health is ok, or a kind that is no operations event."""
    payload = event.payload or {}
    kind = event.kind
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
        wrong = health_causes(payload)
        if not wrong:
            return None
        worker_id = payload.get("worker_id")
        return Cause(kind, ",".join(wrong), f"worker:{worker_id}", worker_id=worker_id)
    return None


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
        .on_conflict_do_nothing(index_elements=["project_id", "key"], index_where=text("state = 'open'"))
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
        cause = await cause_of(conn, event)
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
    "ops_config",
    "read_ops",
    "settle",
    "take",
]
