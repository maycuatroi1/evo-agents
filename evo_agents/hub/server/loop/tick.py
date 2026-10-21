"""One tick of a project's Loop (plan loop-engine, criterion a4): the job ``hub.loop_tick``, which ``evo_agents.hub.
loop_jobs`` queues every 5 minutes and on each event of a project whose Loop is on, one at a time per project.

A tick, in order, each step in a transaction of its own that first takes the project's row of loop_states:

1. the events of hub_events past the cursor, at most ``hub_events.MAX_READ`` at a time: the engine's ``rules`` for
   each, what they return written, and the cursor moved past them, in one transaction, again while a full batch
   came; not while the Loop's breaker is tripped, so the cursor stands and no event is lost;
2. the engine's ``scheduled``: a goal's new revision, the weekly look at each goal;
3. the engine's ``reconcile`` of each work item not finished: what it waits on read again, stale ones cancelled;
4. when the tick ended.

The engine (``model.Engine``, ``rules.RULES`` in this release) reads and never writes; this module writes what it
returns. A work item is made with INSERT ... ON CONFLICT (project, key) DO NOTHING, so the same delta read twice, an
event replayed included, makes nothing more; a transition moves only a work item not finished; a result is noted in a
claim's chain once by its key. Each write adds its row to the work item's ledger (loop_item_events) in the same
transaction. A tick of a project whose Loop is off does nothing; one with nothing new reads and writes no work item,
and no tick hands out a run or calls a model (runs are handed out by the admission of step 5 of the plan).

Handing out runs and counting work items against the breaker come with the plan's steps 5 and 6.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Sequence

from sqlalchemy import func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import hub_events, tables
from evo_agents.hub.server.loop.model import (
    CREATED,
    CYCLE_OF,
    FINISHED,
    NOTED,
    Effect,
    Engine,
    Event,
    Note,
    Transition,
    WorkSpec,
)
from evo_agents.hub.server.loop.rules import RULES
from evo_agents.hub.server.loop.view import load

log = logging.getLogger(__name__)

ARMED = "armed"  # loop_states.breaker
MAX_KEY_CHARS = 500  # loop_items.key, as 0087 checks it
KEY_DIGEST_CHARS = 16


def fit_key(key: str) -> str:
    """``key``, or a longer one cut with the start of its SHA-256 so it stays unique within MAX_KEY_CHARS."""
    if len(key) <= MAX_KEY_CHARS:
        return key
    digest = hashlib.sha256(key.encode()).hexdigest()[:KEY_DIGEST_CHARS]
    return f"{key[: MAX_KEY_CHARS - KEY_DIGEST_CHARS - 1]}~{digest}"


async def _state(conn: AsyncConnection, project_id: int):
    """The project's row of loop_states, held until the transaction ends; None without one."""
    s = tables.loop_states
    query = select(s.c.enabled, s.c.cursor, s.c.breaker).where(s.c.project_id == project_id).with_for_update()
    return (await conn.execute(query)).first()


async def _ledger(
    conn: AsyncConnection, item_id: int, kind: str, state: str, reason, detail, event_id=None, run_id=None
):
    row = {"item_id": item_id, "kind": kind, "state": state, "reason": reason, "detail": dict(detail or {})}
    await conn.execute(insert(tables.loop_item_events).values(**row, event_id=event_id, run_id=run_id))


# Writing what the engine returns


async def _make(conn: AsyncConnection, project_id: int, spec: WorkSpec, event_id: int | None, engine: str) -> bool:
    i = tables.loop_items
    values = {
        "project_id": project_id,
        "kind": spec.kind,
        "cycle": CYCLE_OF[spec.kind],
        "key": fit_key(spec.key),
        "goal_id": spec.goal_id,
        "claim_id": spec.claim_id,
        "srs_document_id": spec.srs_document_id,
        "use_case": spec.use_case,
        "srs_version": spec.srs_version,
        "task_id": spec.task_id,
        "plan_id": spec.plan_id,
        "parent_id": spec.parent_id,
        "state": spec.state,
        "reason": spec.reason,
        "detail": dict(spec.detail),
        "owner": spec.owner,
        "engine": engine,
        "event_id": event_id,
    }
    query = pg_insert(i).values(values).on_conflict_do_nothing(index_elements=[i.c.project_id, i.c.key])
    item_id = await conn.scalar(query.returning(i.c.id))
    if item_id is None:
        return False
    await _ledger(conn, item_id, CREATED, spec.state, spec.reason, {"key": values["key"]}, event_id)
    return True


async def _move(conn: AsyncConnection, project_id: int, move: Transition, event_id: int | None, engine: str) -> bool:
    i = tables.loop_items
    values: dict = {"state": move.state, "reason": move.reason, "updated_at": func.now()}
    if move.detail:
        values["detail"] = i.c.detail.op("||")(literal(dict(move.detail), JSONB))
    if move.task_id is not None:
        values["task_id"] = move.task_id
    if move.plan_id is not None:
        values["plan_id"] = move.plan_id
    if move.failed:
        values.update(failures=i.c.failures + 1, run_id=None)
    if move.state in FINISHED:
        values["finished_at"] = func.now()
    query = update(i).where(i.c.id == move.item_id, i.c.project_id == project_id, i.c.state.not_in(FINISHED))
    if await conn.scalar(query.values(values).returning(i.c.id)) is None:
        return False
    await _ledger(conn, move.item_id, move.event, move.state, move.reason, move.detail, event_id)
    for spec in move.spawn:
        await _make(conn, project_id, spec, event_id, engine)
    return True


async def _note(conn: AsyncConnection, project_id: int, note: Note, event_id: int | None) -> bool:
    i, e = tables.loop_items, tables.loop_item_events
    chain = (i.c.project_id == project_id) & (i.c.claim_id == note.claim_id)
    newest = (await conn.execute(select(i.c.id, i.c.state, i.c.reason).where(chain).order_by(i.c.id.desc()))).first()
    if newest is None:
        return False
    noted = select(e.c.id).join_from(e, i, i.c.id == e.c.item_id).where(chain, e.c.kind == NOTED)
    if await conn.scalar(noted.where(e.c.detail["key"].astext == note.key).limit(1)) is not None:
        return False
    await _ledger(conn, newest.id, NOTED, newest.state, newest.reason, {**note.detail, "key": note.key}, event_id)
    if note.comment and note.author_id is not None:
        comments = [{"task_id": task, "author_id": note.author_id, "body": note.comment} for task in note.comment_on]
        if comments:
            await conn.execute(insert(tables.tasks.comments).values(comments))
    return True


async def apply(conn: AsyncConnection, project_id: int, effect: Effect, event_id: int | None, engine: str) -> bool:
    """Write ``effect`` of the engine named ``engine``; whether it changed anything."""
    if isinstance(effect, WorkSpec):
        return await _make(conn, project_id, effect, event_id, engine)
    if isinstance(effect, Transition):
        return await _move(conn, project_id, effect, event_id, engine)
    return await _note(conn, project_id, effect, event_id)


# The tick


async def _read(conn: AsyncConnection, project_id: int, events: Sequence[Event], engine: Engine) -> int:
    """The engine's rules for each of ``events``, in order; how many effects changed something."""
    view, changed = await load(conn, project_id, events), 0
    for event in events:
        made = 0
        for effect in engine.rules(event, view):
            made += await apply(conn, project_id, effect, event.id, engine.name)
        if made:
            changed += made
            view = await load(conn, project_id, events)  # a later event may answer a work item this one made
    return changed


async def _events(engine: AsyncEngine, project_id: int, rules: Engine, report: Counter) -> bool:
    """Step 1: read the events past the cursor, a batch at a time; False when the Loop is off."""
    s = tables.loop_states
    while True:
        async with engine.begin() as conn:
            state = await _state(conn, project_id)
            if state is None or not state.enabled:
                return False
            if state.breaker != ARMED:
                report["breaker"] = 1
                return True
            found = await hub_events.after(conn, project_id, state.cursor)
            if not found:
                return True
            events = [Event(event.id, event.kind, event.payload, event.created_at) for event in found]
            report["changed"] += await _read(conn, project_id, events, rules)
            moved = update(s).where(s.c.project_id == project_id).values(cursor=events[-1].id, updated_at=func.now())
            await conn.execute(moved)
            report["events"] += len(events)
        if len(events) < hub_events.MAX_READ:
            return True


async def _clock_and_reconcile(conn: AsyncConnection, project_id: int, rules: Engine, report: Counter) -> None:
    """Steps 2 and 3."""
    view = await load(conn, project_id)
    for effect in rules.scheduled(view):
        report["changed"] += await apply(conn, project_id, effect, None, rules.name)
    for item in view.items:
        move = rules.reconcile(item, view)
        if move is not None:
            report["changed"] += await apply(conn, project_id, move, None, rules.name)


async def tick(engine: AsyncEngine, project_id: int, *, rules: Engine = RULES) -> dict:
    """One tick of project ``project_id``'s Loop with ``rules`` (see the module): what it read and changed."""
    report: Counter = Counter()
    if not await _events(engine, project_id, rules, report):
        return {"off": 1}
    async with engine.begin() as conn:
        state = await _state(conn, project_id)
        if state is None or not state.enabled:
            return {"off": 1}
        await _clock_and_reconcile(conn, project_id, rules, report)
        s = tables.loop_states
        await conn.execute(update(s).where(s.c.project_id == project_id).values(ticked_at=func.now()))
    found = {"events": report["events"], "changed": report["changed"]}
    if report["breaker"]:
        found["breaker"] = 1
    log.info("loop tick", extra={"project_id": project_id, **found})
    return found


__all__ = ["apply", "fit_key", "tick"]
