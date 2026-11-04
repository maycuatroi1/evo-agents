"""The proposals of @builder of kind ``problem`` (``proposals``; plan problem-intake, criterion a1): where one
applies, how it is checked, applied and rolled back, as its ``Kind``.

A proposal names a problem of the project (``target.problem``, its code) or none, for a new one whose code the hub
picks when it is proposed and keeps in the target. Its version is the problem's revision, 0 for a new one. It may
carry lines of evidence, written with the problem when a writer approves; a proposal of lines alone names an existing
problem and writes no revision. Rolling back takes the lines it made away, and drops a problem it made.
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
from evo_agents.hub import problems as model
from evo_agents.hub.server import problem_evidence
from evo_agents.hub.server import problems as routes
from evo_agents.hub.server.projects import ProjectAccess


def evidence_of(given: object) -> tuple[list[dict], list[str]]:
    """The lines of evidence a proposal of a problem carries as the hub keeps them, and what is wrong with them."""
    if not isinstance(given, list) or len(given) > model.MAX_PROPOSED_EVIDENCE:
        return [], [f"evidence is a list of at most {model.MAX_PROPOSED_EVIDENCE} lines"]
    lines, wrong = [], []
    for at, item in enumerate(given, start=1):
        try:
            lines.append(problem_evidence.ProblemEvidenceWrite.model_validate(item).model_dump())
        except ValidationError as exc:
            wrong.append(f"evidence {at}: {said_by_pydantic(exc, 'evidence')}")
    return lines, wrong


async def _locate(conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
    """Problem ``target.problem`` as it is now, or a new problem under the next code when the target names none or
    that one."""
    ref = change.target.get("problem")
    row = await routes.found(conn, access.project_id, ref) if ref is not None else None
    if row is None:
        code = await routes.next_code(conn, access.project_id)
        if ref is not None and ref != code:
            raise ProposalError(404, f"project {access.name} has no problem {ref}; a new problem takes the code {code}")
        return Target(f"a new problem {code} of project {access.name}", 0, {}, problem={}, names={"problem": code})
    code, fields = model.code(row.number), routes.fields_of(row)
    held = {name: value for name, value in fields.items() if value is not None}
    return Target(
        f"problem {code} of project {access.name}", row.revision, held, problem=fields, names={"problem": code}
    )


def _body(change: Change, target: Target) -> routes.ProjectProblemWrite:
    """The problem ``change`` makes of the one ``target`` holds, as a write of its route; ProposalError (422) naming
    what is wrong with its shape."""
    wrong = [f"{name}: a problem has no such field" for name in sorted(set(change.sets) - set(model.FIELDS))]
    wrong += [f"{name}: a problem always has one" for name in change.unset if name not in model.CLEARABLE]
    refuse(wrong)
    assert target.problem is not None  # located for a problem
    fields = {**target.problem, **change.sets, **dict.fromkeys(change.unset)}
    if change.sets.get("status") == model.OPEN and "close_reason" not in change.sets:
        fields["close_reason"] = None  # opened again
    try:
        return routes.ProjectProblemWrite.model_validate(fields)
    except ValidationError as exc:
        raise ProposalError(422, f"{said_by_pydantic(exc, 'problem')}; {NOTHING}") from None


async def _evidence_wrong(conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target) -> list[str]:
    """What is wrong with the lines of evidence ``change`` carries as additions to the problem ``target`` holds (none
    yet for a new problem)."""
    if not change.evidence:
        return []
    held = await routes.found(conn, access.project_id, target.names["problem"]) if target.version else None
    lines = [problem_evidence.ProblemEvidenceWrite.model_validate(line) for line in change.evidence]
    return await problem_evidence.wrong_lines(conn, None if held is None else held.id, lines)


async def _wrong(conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target) -> list[str]:
    wrong = await _evidence_wrong(conn, access, change, target)
    if change.evidence_only:
        return wrong
    body = _body(change, target)
    wrong += layer_problems(change, target) if target.version else []
    try:
        return wrong + await routes.check(conn, access, target.names["problem"], body)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None


async def _check(
    conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> None:
    refuse(await _wrong(conn, access, change, target))


async def _write(w: Writing, body: routes.ProjectProblemWrite, base: int, note: str, via: str) -> int:
    """Write ``body`` as the next revision of the problem of ``w.target`` while it is at revision ``base``; that
    one."""
    ref = w.target.names["problem"]
    body = body.model_copy(update={"note": note[: model.MAX_NOTE_CHARS], "if_revision": base})
    try:
        shown, written = await routes.write(w.conn, w.user, w.access, ref, body, via=via)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None
    if not written:
        raise ProposalError(409, f"problem {ref} of project {w.access.name} is this already; {NOTHING}")
    return shown.revision


async def _held_id(w: Writing) -> int:
    """The id of the problem of ``w.target``, which is there."""
    held = await routes.found(w.conn, w.access.project_id, w.target.names["problem"])
    assert held is not None  # located, or written by the proposal
    return held.id


async def _apply(w: Writing) -> int:
    """Write the problem the change makes (unless it carries lines of evidence alone), then the lines it carries with
    the proposal's id; the problem's revision."""
    change, version = w.change, w.version
    if change.evidence_only:
        await routes._lock(w.conn, w.access.project_id)
    else:
        version = await _write(w, _body(change, w.target), w.version, w.note, f" proposal={w.proposal_id}")
    if change.evidence:
        held_id = await _held_id(w)
        lines = [problem_evidence.ProblemEvidenceWrite.model_validate(line) for line in change.evidence]
        try:
            await problem_evidence.add_lines(w.conn, w.user, w.access, held_id, lines, proposal_id=w.proposal_id)
        except HTTPException as exc:
            raise ProposalError(exc.status_code, str(exc.detail)) from None
    return version


async def _roll_back_revision(w: Writing) -> int:
    applied, proposal_id = w.version, w.proposal_id
    ref, before = w.target.names["problem"], applied - 1
    row = await routes.found(w.conn, w.access.project_id, ref, before or None)
    if row is None:
        raise ProposalError(409, f"problem {ref} of project {w.access.name} has no revision {before}; {NOTHING}")
    if before == 0:
        reason = f"rollback of proposal {proposal_id} of @builder"
        body = routes.closing(row, model.DROPPED, reason, None, applied)
        note = f"rollback of proposal {proposal_id} of @builder: the problem it made, dropped"
    else:
        body = routes.ProjectProblemWrite.model_validate(routes.fields_of(row))
        note = f"rollback of proposal {proposal_id} of @builder: a copy of revision {before}"
    return await _write(w, body, applied, note, f" proposal={proposal_id} copy-of={before}")


async def _roll_back(w: Writing) -> int:
    """The problem's next revision, a copy of revision ``applied - 1``; a problem the proposal made is dropped. The
    lines of evidence it made are taken away; a proposal of lines alone writes no revision."""
    if w.change.evidence_only:
        await routes._lock(w.conn, w.access.project_id)
        version = w.version
    else:
        version = await _roll_back_revision(w)
    await problem_evidence.remove_proposed(w.conn, w.user, w.access, await _held_id(w), w.proposal_id)
    return version


KIND = Kind(_locate, _check, _apply, _roll_back)
