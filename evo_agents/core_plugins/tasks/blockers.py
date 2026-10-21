"""What a task waits on: its blockers, set and cleared by hand (``put``, ``change``) and read with the open decisions
of its runs (``open_for``).

A blocker is a flag beside a task's status, never a status. It names why (``reason``) and who or what the task waits
for, in one of three forms: a member with a grant on the project, another task of the project (never the task itself),
or a text for someone outside. A task has as many open blockers as it has rows not cleared; setting and clearing one
are rows of the task's history (field ``blocker``). An open decision of a run that belongs to the task is a blocker
too, read when asked (``blocker_queries``): the run's owner is who it waits for, the time the decision was asked is
since, and answering it ends it. The caller holds the transaction and has checked the writer role.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.tasks import blocker_queries
from evo_agents.core_plugins.tasks.settings import Settings, TasksError, number_of
from evo_agents.core_plugins.tasks.store import TaskRow, members, visible
from evo_agents.core_plugins.tasks.writes import Writer, record
from evo_agents.hub import tables
from evo_agents.hub.tables_tasks import MAX_REASON_CHARS, MAX_WAITING_CHARS

MANUAL, DECISION = "manual", "decision"
KINDS = (MANUAL, DECISION)
WAITING_KINDS = ("member", "task", "text")


@dataclass(frozen=True)
class BlockerRow:
    """A blocker as it is read: ``id`` is the row's for a blocker set by hand, None for an open decision."""

    id: int | None
    kind: str
    reason: str
    waiting_kind: str
    waiting_on: str
    since: datetime
    set_by: str | None
    cleared_at: datetime | None = None
    cleared_by: str | None = None
    decision_id: int | None = None
    run_id: int | None = None


@dataclass
class Waiting:
    """Who or what a blocker waits for: exactly one of the three is given."""

    member: str | None = None
    task: str | None = None
    text: str | None = None

    def given(self) -> int:
        return sum(value is not None for value in (self.member, self.task, self.text))


def _manual_query(config: Settings):
    b, u = tables.tasks.blockers, tables.users
    setter, waiter, cleaner, other = (
        u.alias("setter"),
        u.alias("waiter"),
        u.alias("cleaner"),
        tables.tasks.items.alias("other"),
    )
    query = (
        select(
            b.c.id,
            b.c.task_id,
            b.c.reason,
            b.c.since,
            b.c.cleared_at,
            b.c.waiting_text,
            setter.c.login.label("set_by"),
            waiter.c.login.label("waiting_login"),
            cleaner.c.login.label("cleared_by"),
            other.c.number.label("waiting_number"),
        )
        .select_from(
            b.join(setter, setter.c.id == b.c.created_by)
            .outerjoin(waiter, waiter.c.id == b.c.waiting_user_id)
            .outerjoin(cleaner, cleaner.c.id == b.c.cleared_by)
            .outerjoin(other, other.c.id == b.c.waiting_task_id)
        )
        .order_by(b.c.id)
    )
    return query


def _manual(config: Settings, row) -> BlockerRow:
    if row.waiting_login is not None:
        kind, on = "member", row.waiting_login
    elif row.waiting_number is not None:
        kind, on = "task", config.ref(row.waiting_number)
    else:
        kind, on = "text", row.waiting_text
    return BlockerRow(row.id, MANUAL, row.reason, kind, on, row.since, row.set_by, row.cleared_at, row.cleared_by)


async def open_for(conn: AsyncConnection, config: Settings, task_ids: list[int]) -> dict[int, list[BlockerRow]]:
    """task id -> its open blockers, those set by hand first (the oldest first), then the open decisions of its runs
    (the oldest first)."""
    found: dict[int, list[BlockerRow]] = {task_id: [] for task_id in task_ids}
    if not task_ids:
        return found
    b = tables.tasks.blockers
    query = _manual_query(config).where(b.c.task_id.in_(task_ids), b.c.cleared_at.is_(None))
    for row in await conn.execute(query):
        found[row.task_id].append(_manual(config, row))
    u = tables.users
    decisions = blocker_queries.open_decisions(task_ids).subquery()
    asked = (
        select(decisions, u.c.login)
        .join_from(decisions, u, u.c.id == decisions.c.dispatched_by)
        .order_by(decisions.c.asked_at, decisions.c.decision_id)
    )
    for row in await conn.execute(asked):
        view = BlockerRow(
            None, DECISION, row.question, "member", row.login, row.asked_at, None,
            decision_id=row.decision_id, run_id=row.run_id,
        )  # fmt: skip
        found[row.task_id].append(view)
    return found


async def one(conn: AsyncConnection, config: Settings, task_id: int, blocker_id: int) -> BlockerRow | None:
    b = tables.tasks.blockers
    row = (await conn.execute(_manual_query(config).where(b.c.task_id == task_id, b.c.id == blocker_id))).first()
    return _manual(config, row) if row else None


# Writing


def _shown(blocker: BlockerRow) -> str:
    return f"waiting on {blocker.waiting_on}: {blocker.reason}"


async def _waiting_columns(conn: AsyncConnection, writer: Writer, row: TaskRow, given: Waiting) -> dict:
    """The columns of the waiting side that ``given`` names, each of the other two cleared; 422 when it names none, or
    a member without a grant, a task the member does not see, another project's or the task itself."""
    if given.given() != 1:
        raise TasksError(422, "a blocker waits on one of a member, a task of the project, or a text: give exactly one")
    access = writer.access
    columns = {"waiting_user_id": None, "waiting_task_id": None, "waiting_text": None}
    if given.member is not None:
        login = given.member.strip()
        columns["waiting_user_id"] = (await members(conn, access, {login}))[login]
    elif given.task is not None:
        t = tables.tasks.items
        query = select(t.c.id, t.c.label).where(
            t.c.project_id == access.project_id, t.c.number == number_of(given.task)
        )
        found = (await conn.execute(query)).first()
        if found is None or not visible(access, found.label, writer.sink):
            raise TasksError(422, f"no task {given.task} in project {access.name} that you can see, to wait on")
        if found.id == row.id:
            raise TasksError(422, f"task {given.task} is this task: a task never waits on itself")
        columns["waiting_task_id"] = found.id
    else:
        text = (given.text or "").strip()
        if not text or len(text) > MAX_WAITING_CHARS:
            raise TasksError(422, f"a blocker waits on a text of 1 to {MAX_WAITING_CHARS} characters")
        columns["waiting_text"] = text
    return columns


def _reason(text: str) -> str:
    reason = text.strip()
    if not reason or len(reason) > MAX_REASON_CHARS:
        raise TasksError(422, f"a blocker has a reason of 1 to {MAX_REASON_CHARS} characters")
    return reason


async def put(conn: AsyncConnection, writer: Writer, row: TaskRow, reason: str, waiting: Waiting) -> BlockerRow:
    """Set a blocker on task ``row``, whose row lock the caller holds; the blocker as it is now."""
    values = {
        "task_id": row.id,
        "reason": _reason(reason),
        "created_by": writer.user_id,
        **await _waiting_columns(conn, writer, row, waiting),
    }
    b = tables.tasks.blockers
    blocker_id = await conn.scalar(insert(b).values(**values).returning(b.c.id))
    blocker = await one(conn, writer.settings, row.id, blocker_id)
    await record(conn, row.id, writer.user_id, FIELD, None, _shown(blocker))
    await _touch(conn, row.id)
    return blocker


async def change(
    conn: AsyncConnection,
    writer: Writer,
    row: TaskRow,
    blocker_id: int,
    *,
    reason: str | None = None,
    waiting: Waiting | None = None,
    clear: bool = False,
) -> BlockerRow:
    """Edit blocker ``blocker_id`` of task ``row`` (its reason, who it waits on) or clear it; 404 for a blocker the
    task does not have, 409 for one already cleared. The blocker as it is now."""
    before = await one(conn, writer.settings, row.id, blocker_id)
    if before is None:
        raise TasksError(404, f"task {writer.settings.ref(row.number)} has no blocker {blocker_id}")
    if before.cleared_at is not None:
        raise TasksError(409, f"blocker {blocker_id} was cleared already")
    values: dict = {}
    if reason is not None:
        values["reason"] = _reason(reason)
    if waiting is not None and waiting.given():
        values.update(await _waiting_columns(conn, writer, row, waiting))
    elif waiting is not None:
        raise TasksError(422, "a blocker waits on one of a member, a task of the project, or a text: give exactly one")
    if clear:
        values.update(cleared_at=func.now(), cleared_by=writer.user_id)
    if not values:
        raise TasksError(422, "give what to change: a reason, who it waits on, or clear it")
    b = tables.tasks.blockers
    await conn.execute(update(b).where(b.c.id == blocker_id).values(**values))
    after = await one(conn, writer.settings, row.id, blocker_id)
    if clear:
        await record(conn, row.id, writer.user_id, FIELD, _shown(before), None)
    elif _shown(before) != _shown(after):
        await record(conn, row.id, writer.user_id, FIELD, _shown(before), _shown(after))
    await _touch(conn, row.id)
    return after


async def _touch(conn: AsyncConnection, task_id: int) -> None:
    t = tables.tasks.items
    await conn.execute(update(t).where(t.c.id == task_id).values(updated_at=func.now()))


FIELD = "blocker"  # of the history of a task

__all__ = [
    "DECISION",
    "FIELD",
    "KINDS",
    "MANUAL",
    "WAITING_KINDS",
    "BlockerRow",
    "Waiting",
    "change",
    "one",
    "open_for",
    "put",
]
