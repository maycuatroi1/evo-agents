"""The proposals of @builder of kind ``goal`` (``proposals``): where one applies, how it is checked, applied and
rolled back, as its ``Kind``.

A proposal names a goal of the project (``target.goal``, its code or alias) or none, for a new one whose code the hub
picks when it is proposed and keeps in the target. Its version is the goal's revision, 0 for a new one. Rolling back a
new goal closes it (dropped).
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.core_plugins.builder.kinds import (
    Change,
    Kind,
    Target,
    Writing,
    layer_problems,
    refuse,
    said_by_pydantic,
)
from evo_agents.hub import goals
from evo_agents.hub.server import goals as project_goals
from evo_agents.hub.server.projects import ProjectAccess


async def _locate(conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
    """Goal ``target.goal`` as it is now, or a new goal under the next code when the target names none or that one."""
    await project_goals.adopt_charter_goals(conn, access.project_id, access.name)
    ref = change.target.get("goal")
    row = await project_goals.found(conn, access.project_id, ref) if ref is not None else None
    if row is None:
        code = await project_goals.next_code(conn, access.project_id)
        if ref is not None and ref != code:
            raise ProposalError(404, f"project {access.name} has no goal {ref}; a new goal takes the code {code}")
        return Target(f"a new goal {code} of project {access.name}", 0, {}, goal={}, names={"goal": code})
    code, fields = goals.code(row.number), project_goals.fields_of(row)
    held = {name: value for name, value in fields.items() if value is not None}
    return Target(f"goal {code} of project {access.name}", row.revision, held, goal=fields, names={"goal": code})


def _body(change: Change, target: Target) -> project_goals.ProjectGoalWrite:
    """The goal ``change`` makes of the one ``target`` holds, as a write of its route; ProposalError (422) naming
    what is wrong with its shape."""
    problems = [f"{name}: a goal has no such field" for name in sorted(set(change.sets) - set(goals.FIELDS))]
    problems += [f"{name}: a goal always has one" for name in change.unset if name not in goals.CLEARABLE]
    refuse(problems)
    assert target.goal is not None  # located for a goal
    fields = {**target.goal, **change.sets, **dict.fromkeys(change.unset)}
    if change.sets.get("status", goals.CLOSED) != goals.CLOSED and "close_reason" not in change.sets:
        fields["close_reason"] = None  # opened again
    try:
        return project_goals.ProjectGoalWrite.model_validate(fields)
    except ValidationError as exc:
        raise ProposalError(422, f"{said_by_pydantic(exc)}; {NOTHING}") from None


async def _check(
    conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> None:
    body = _body(change, target)
    problems = layer_problems(change, target) if target.version else []
    try:
        problems += await project_goals.check(conn, access, target.names["goal"], body)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None
    refuse(problems)


async def _write(w: Writing, body: project_goals.ProjectGoalWrite, base: int, note: str, via: str) -> int:
    """Write ``body`` as the next revision of the goal of ``w.target`` while it is at revision ``base``; that one."""
    ref = w.target.names["goal"]
    body = body.model_copy(update={"note": note[: goals.MAX_NOTE_CHARS], "if_revision": base})
    try:
        shown, written = await project_goals.write(w.conn, w.user, w.access, ref, body, via=via)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None
    if not written:
        raise ProposalError(409, f"goal {ref} of project {w.access.name} is this already; {NOTHING}")
    return shown.revision


async def _apply(w: Writing) -> int:
    return await _write(w, _body(w.change, w.target), w.version, w.note, f" proposal={w.proposal_id}")


async def _roll_back(w: Writing) -> int:
    """The goal's next revision, a copy of revision ``applied - 1``; a goal the proposal made is closed (dropped)."""
    applied, proposal_id = w.version, w.proposal_id
    ref, before = w.target.names["goal"], applied - 1
    row = await project_goals.found(w.conn, w.access.project_id, ref, before or None)
    if row is None:
        raise ProposalError(409, f"goal {ref} of project {w.access.name} has no revision {before}; {NOTHING}")
    if before == 0:
        body = project_goals.closing(row, "dropped", None, applied)
        note = f"rollback of proposal {proposal_id} of @builder: the goal it made, closed"
    else:
        body = project_goals.ProjectGoalWrite.model_validate(project_goals.fields_of(row))
        note = f"rollback of proposal {proposal_id} of @builder: a copy of revision {before}"
    return await _write(w, body, applied, note, f" proposal={proposal_id} copy-of={before}")


KIND = Kind(_locate, _check, _apply, _roll_back)
