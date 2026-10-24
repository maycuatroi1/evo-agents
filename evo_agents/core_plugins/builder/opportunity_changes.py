"""The proposals of @builder of kind ``opportunity`` (plan opportunity-intake, criterion a1 and a2): where one applies,
how it is checked, applied and rolled back. ``proposals`` calls these for its kind, as it calls its own for a problem.

A proposal names an opportunity of the project (``target.opportunity``, its code) or none, for a new one whose code the
hub picks when it is proposed and keeps in the target. Its version is the opportunity's revision, 0 for a new one. It
may carry lines of evidence, written with the opportunity when a writer approves; a proposal of lines alone names an
existing opportunity and writes no revision. Rolling back takes the lines it made away, and drops an opportunity it made.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.hub import opportunities as model
from evo_agents.hub.server import opportunities as routes
from evo_agents.hub.server import opportunity_evidence as lines_of
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal


def said(exc: ValidationError, what: str) -> str:
    return "; ".join(f"{'.'.join(map(str, error['loc'])) or what}: {error['msg']}" for error in exc.errors())


def target_problems(target: Mapping) -> list[str]:
    if re.fullmatch(model.REF, str(target.get("opportunity", "O-1"))):
        return []
    return ["target.opportunity is an opportunity's code (O-<number>)"]


def evidence_of(given: object) -> tuple[list[dict], list[str]]:
    """The lines of evidence a proposal of an opportunity carries as the hub keeps them, and what is wrong with them."""
    if not isinstance(given, list) or len(given) > model.MAX_PROPOSED_EVIDENCE:
        return [], [f"evidence is a list of at most {model.MAX_PROPOSED_EVIDENCE} lines"]
    found, wrong = [], []
    for at, item in enumerate(given, start=1):
        try:
            found.append(lines_of.OpportunityEvidenceWrite.model_validate(item).model_dump())
        except ValidationError as exc:
            wrong.append(f"evidence {at}: {said(exc, 'evidence')}")
    return found, wrong


async def locate(conn: AsyncConnection, access: ProjectAccess, target: Mapping) -> tuple[str, int, dict, dict, str]:
    """(title, version, held values, fields, code) of the opportunity ``target`` names as it is now, or of a new one
    under the next code when the target names none or that one."""
    ref = target.get("opportunity")
    row = await routes.found(conn, access.project_id, ref) if ref is not None else None
    if row is None:
        code = await routes.next_code(conn, access.project_id)
        if ref is not None and ref != code:
            raise ProposalError(
                404, f"project {access.name} has no opportunity {ref}; a new opportunity takes the code {code}"
            )
        return f"a new opportunity {code} of project {access.name}", 0, {}, {}, code
    code, fields = model.code(row.number), routes.fields_of(row, access.project_id)
    held = {name: value for name, value in fields.items() if value is not None}
    return f"opportunity {code} of project {access.name}", row.revision, held, fields, code


def body_of(sets: Mapping, unset: tuple[str, ...], fields: Mapping) -> routes.ProjectOpportunityWrite:
    """The opportunity the change makes of the one that ``fields`` holds, as a write of its route; ProposalError (422)
    naming what is wrong with its shape."""
    wrong = [f"{name}: an opportunity has no such field" for name in sorted(set(sets) - set(model.FIELDS))]
    wrong += [f"{name}: an opportunity always has one" for name in unset if name not in model.CLEARABLE]
    if wrong:
        raise ProposalError(422, f"{'; '.join(wrong)}; {NOTHING}")
    values = {**fields, **sets, **dict.fromkeys(unset)}
    if sets.get("status") == model.OPEN and "close_reason" not in sets:
        values["close_reason"] = None  # opened again
    try:
        return routes.ProjectOpportunityWrite.model_validate(values)
    except ValidationError as exc:
        raise ProposalError(422, f"{said(exc, 'opportunity')}; {NOTHING}") from None


async def wrong_evidence(conn: AsyncConnection, access: ProjectAccess, change, code: str, version: int) -> list[str]:
    """What is wrong with the lines ``change`` carries as additions to the opportunity ``code`` (none yet when new)."""
    if not change.evidence:
        return []
    held = await routes.found(conn, access.project_id, code) if version else None
    written = [lines_of.OpportunityEvidenceWrite.model_validate(line) for line in change.evidence]
    return await lines_of.wrong_lines(conn, None if held is None else held.id, written)


async def wrong(
    conn: AsyncConnection, access: ProjectAccess, change, code: str, version: int, fields: dict
) -> list[str]:
    """Each thing wrong with ``change`` as the next revision of the opportunity (the answers of other projects are
    checked when a writer approves)."""
    found = await wrong_evidence(conn, access, change, code, version)
    if change.evidence_only:
        return found
    body = body_of(change.sets, change.unset, fields)
    try:
        return found + await routes.check(conn, access, code, body)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None


async def _write(conn, user, access, code, body, base: int, note: str, via: str) -> int:
    body = body.model_copy(update={"note": note[: model.MAX_NOTE_CHARS], "if_revision": base})
    try:
        shown, written = await routes.write(conn, user, access, code, body, via=via)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None
    if not written:
        raise ProposalError(409, f"opportunity {code} of project {access.name} is this already; {NOTHING}")
    return shown.revision


async def _add_lines(conn, user, access, change, code: str, proposal_id: int) -> None:
    held = await routes.found(conn, access.project_id, code)
    written = [lines_of.OpportunityEvidenceWrite.model_validate(line) for line in change.evidence]
    try:
        await lines_of.add_lines(conn, user, access, held.id, written, proposal_id=proposal_id)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None


async def apply(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    change,
    code: str,
    fields: dict,
    base: int,
    proposal_id: int,
) -> int:
    """Write the opportunity ``change`` makes (unless it carries lines of evidence alone), then the lines it carries
    with the proposal's id; the opportunity's revision."""
    version = base
    if change.evidence_only:
        await routes._lock(conn, access.project_id)
    else:
        body = body_of(change.sets, change.unset, fields)
        note = f"proposal {proposal_id} of @builder: {change.summary}"
        version = await _write(conn, user, access, code, body, base, note, f" proposal={proposal_id}")
    if change.evidence:
        await _add_lines(conn, user, access, change, code, proposal_id)
    return version


async def roll_back(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, change, code: str, applied: int, proposal_id: int
) -> int:
    """The opportunity's next revision, a copy of revision ``applied - 1``; an opportunity the proposal made is
    dropped. The lines it made are taken away; a proposal of lines alone writes no revision."""
    if change.evidence_only:
        await routes._lock(conn, access.project_id)
        held = await routes.found(conn, access.project_id, code)
        await lines_of.remove_proposed(conn, user, access, held.id, proposal_id)
        return applied
    before = applied - 1
    row = await routes.found(conn, access.project_id, code, before or None)
    if row is None:
        raise ProposalError(409, f"opportunity {code} of project {access.name} has no revision {before}; {NOTHING}")
    if before == 0:
        reason = f"rollback of proposal {proposal_id} of @builder"
        body = routes.closing(row, access.project_id, model.DROPPED, reason, None, applied)
        note = f"rollback of proposal {proposal_id} of @builder: the opportunity it made, dropped"
    else:
        body = routes.ProjectOpportunityWrite.model_validate(routes.fields_of(row, access.project_id))
        note = f"rollback of proposal {proposal_id} of @builder: a copy of revision {before}"
    version = await _write(conn, user, access, code, body, applied, note, f" proposal={proposal_id} copy-of={before}")
    held = await routes.found(conn, access.project_id, code)
    await lines_of.remove_proposed(conn, user, access, held.id, proposal_id)
    return version
