"""What a task waits on: its blockers, set and cleared by hand (``put``, ``change``) and read with the open decisions
of its runs (``open_for``).

A blocker is a flag beside a task's status, never a status. It names why (``reason``) and who or what the task waits
for, in one of three forms: a member with a grant on the project, another task of the project (never the task itself),
or a text for someone outside. A task has as many open blockers as it has rows not cleared; setting and clearing one
are rows of the task's history (field ``blocker``). An open decision of a run that belongs to the task is a blocker
too, read when asked (``repository.blockers``): the run's owner is who it waits for, the time the decision was asked
is since, and answering it ends it. The caller holds the transaction and has checked the writer role.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.tasks.models.rows import DECISION, MANUAL, BlockerRow, TaskRow, Waiting
from evo_agents.hub.server.tasks.models.values import Settings, TasksError, Writer, number_of, visible
from evo_agents.hub.server.tasks.repository import blockers as repository
from evo_agents.hub.server.tasks.repository import history
from evo_agents.hub.server.tasks.repository import tasks as task_rows
from evo_agents.hub.server.tasks.service.project import members
from evo_agents.hub.tables_tasks import MAX_REASON_CHARS, MAX_WAITING_CHARS

FIELD = "blocker"  # of the history of a task


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
    for row in await repository.manual_rows(conn, task_ids):
        found[row.task_id].append(_manual(config, row))
    for row in await repository.decision_rows(conn, task_ids):
        view = BlockerRow(
            None, DECISION, row.question, "member", row.login, row.asked_at, None,
            decision_id=row.decision_id, run_id=row.run_id,
        )  # fmt: skip
        found[row.task_id].append(view)
    return found


async def one(conn: AsyncConnection, config: Settings, task_id: int, blocker_id: int) -> BlockerRow | None:
    row = await repository.manual_row(conn, task_id, blocker_id)
    return _manual(config, row) if row else None


async def _one(conn: AsyncConnection, config: Settings, task_id: int, blocker_id: int) -> BlockerRow:
    """Blocker ``blocker_id`` of task ``task_id``, which the caller wrote in this transaction."""
    found = await one(conn, config, task_id, blocker_id)
    assert found is not None
    return found


# Writing


def _shown(blocker: BlockerRow) -> str:
    return f"waiting on {blocker.waiting_on}: {blocker.reason}"


async def _waiting_columns(conn: AsyncConnection, writer: Writer, row: TaskRow, given: Waiting) -> dict:
    """The columns of the waiting side that ``given`` names, each of the other two cleared; 422 when it names none, or
    a member without a grant, a task the member does not see, another project's or the task itself."""
    if given.given() != 1:
        raise TasksError(422, "a blocker waits on one of a member, a task of the project, or a text: give exactly one")
    access = writer.access
    columns: dict[str, int | str | None] = {"waiting_user_id": None, "waiting_task_id": None, "waiting_text": None}
    if given.member is not None:
        login = given.member.strip()
        columns["waiting_user_id"] = (await members(conn, access, {login}))[login]
    elif given.task is not None:
        found = await task_rows.task_label(conn, access.project_id, number_of(given.task))
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
    blocker_id = await repository.insert_blocker(conn, values)
    blocker = await _one(conn, writer.settings, row.id, blocker_id)
    await history.record(conn, row.id, writer.user_id, FIELD, None, _shown(blocker))
    await task_rows.update_task(conn, row.id, {})
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
    if not values and not clear:
        raise TasksError(422, "give what to change: a reason, who it waits on, or clear it")
    await repository.update_blocker(conn, blocker_id, values, cleared_by=writer.user_id if clear else None)
    after = await _one(conn, writer.settings, row.id, blocker_id)
    if clear:
        await history.record(conn, row.id, writer.user_id, FIELD, _shown(before), None)
    elif _shown(before) != _shown(after):
        await history.record(conn, row.id, writer.user_id, FIELD, _shown(before), _shown(after))
    await task_rows.update_task(conn, row.id, {})
    return after


__all__ = ["FIELD", "change", "one", "open_for", "put"]
