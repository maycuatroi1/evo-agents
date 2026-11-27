"""A person's pass of a hold of the hub's merge queue on GitLab (``merge_gate.PASSABLE_GATES``): gate 3, score
hacking, holds a merge request for a person, and POST /v1/projects/{p}/merge-queue/{id}/pass (``routes.view``) lets an
admin of the project pass it with a reason (``pass_hold``). The pass's query is ``repository.passes``'s.

The merge request goes back to ``gating`` at the same head; its ledger has a line of the gate passed that names the
admin and the reason (the key PERSON of its detail), and the audit says merge.pass. The job's next pass reads its gates
again at that head (``service.gate``): gate 3 lets the signs through for the pass (``passes``),
and the risk, the checks and the Judges (unless their verdicts hold for the patch), its turn, the pipeline at its
rebased head and the merge still apply. The Judge's claim says the pass covers the head it judges (``covers``), so
the worker's Judge weighs the signs against the plan (``judge.WEIGH_SIGNS``) instead of failing the change unjudged,
and the hub takes its verdict past gate 3 on the signs of that patch. At the rebased head
(``service.turn``) gate 3 lets the signs through again while the patch is the one the gates passed.
A new head (a push of someone else, or a rebase that changed the patch: the ledger's ``head``) takes the pass back,
and the hold it comes to again needs a new pass.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import merge_gate
from evo_agents.hub.server import audit
from evo_agents.hub.server.merge.models.passes import AUDIT_REASON_CHARS, MAX_REASON_CHARS, PERSON, Pass
from evo_agents.hub.server.merge.repository import gate as queue
from evo_agents.hub.server.merge.repository.passes import standing
from evo_agents.hub.server.merge.service.access import admin_only


async def pass_hold(conn: AsyncConnection, user, access, queue_id: int, reason: str):
    """An admin of the project passes the hold of merge request ``queue_id`` at its gate with ``reason``: it is
    ``gating`` again at the same head, its ledger names the pass, the audit says merge.pass; its row now."""
    admin_only(user, access)
    row = await queue.row_of(conn, queue_id, lock=True)
    if row is None or row.project_id != access.project_id:
        raise HTTPException(404, f"project {access.name} has no merge request {queue_id} in its merge queue")
    refused = merge_gate.pass_refusal(row.state, row.reason, row.source)
    if refused is not None:
        raise HTTPException(409, f"no person passes !{row.iid} of {row.repo}: {refused}")
    text = " ".join((reason or "").split())[:MAX_REASON_CHARS]
    if not text:
        raise HTTPException(422, "a pass needs a reason: why the hold may go on")
    gate = merge_gate.held_at(row.reason)
    await queue.set_row(conn, row.id, state="gating", reason=None)
    detail = {PERSON: user.login, "held": row.reason}
    line = merge_gate.pass_line(user.login, text)
    await queue.record(conn, row, "gate", gate=gate, passed=True, reason=line, sha=row.head_sha, detail=detail)
    target = f"{access.name}/{row.repo} !{row.iid} gate={gate} sha={row.head_sha} reason={text[:AUDIT_REASON_CHARS]}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.MERGE_PASS,
        target=target,
        project_id=access.project_id,
    )
    return await queue.row_of(conn, row.id)


async def covers(conn: AsyncConnection, row) -> Pass | None:
    """The admin's pass of gate 3 that covers the signs of score hacking at the head of merge request ``row``, the one
    its Judge judges: a pass that still holds and was given at that head; None without one. Its judge run is told so
    (``merge_judge.merge_claim``, ``signs_passed``): the Judge weighs the signs against the plan instead of failing the
    change unjudged, and the hub does not hold it at gate 3 again on the worker's reading of the same patch."""
    found = await standing(conn, row.id, 3)
    if found is None or row.head_sha is None or found.sha != row.head_sha:
        return None
    return found


async def passes(
    engine: AsyncEngine, row_id: int, why: str, *, sha: str | None = None, same_patch: bool = False
) -> tuple[str, dict] | None:
    """(the reason, the detail) of a line of gate 3 passed although it found ``why`` (signs of score hacking), for an
    admin's pass that still holds and was given at head ``sha``, or, at a rebased head, for the patch the gates passed
    (``same_patch``); None when no pass lets it through."""
    async with engine.begin() as conn:
        found = await standing(conn, row_id, 3)
    if found is None or not (same_patch or (sha is not None and found.sha == sha)):
        return None
    return merge_gate.reason_line(f"{why}; {found.line}"), {"pass": found.id, "passed_by": found.login}
