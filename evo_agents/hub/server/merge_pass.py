"""A person's pass of a hold of the hub's merge queue on GitLab (``merge_gate.PASSABLE_GATES``): gate 3, score
hacking, holds a merge request for a person, and POST /v1/projects/{p}/merge-queue/{id}/pass (``merge_view``) lets an
admin of the project pass it with a reason (``pass_hold``).

The merge request goes back to ``gating`` at the same head; its ledger has a line of the gate passed that names the
admin and the reason (the key PERSON of its detail), and the audit says merge.pass. The job's next pass reads its gates
again at that head (``evo_agents.hub.server.merge_gate``): gate 3 lets the signs through for the pass (``passes``),
and the risk, the checks and the Judges (unless their verdicts hold for the patch), its turn, the pipeline at its
rebased head and the merge still apply. At the rebased head (``evo_agents.hub.server.merge_turn``) gate 3 lets the
signs through again while the patch is the one the gates passed. A new head (a push of someone else, or a rebase that
changed the patch: the ledger's ``head``) takes the pass back, and the hold it comes to again needs a new pass.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import merge_gate, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server import merge_gate as queue
from evo_agents.hub.server.post_merge import admin_only

MAX_REASON_CHARS = 1000  # of an admin's reason for a pass, in the ledger's line
AUDIT_REASON_CHARS = 300  # of it in the audit's target
PERSON = "person"  # the key of the detail of a ledger line that is an admin's pass: their login


@dataclass(frozen=True)
class Pass:
    """An admin's pass of a hold that still holds: its ledger line, the head it was given at, who and why."""

    id: int
    sha: str | None
    login: str
    line: str


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


async def standing(conn: AsyncConnection, row_id: int, gate: int) -> Pass | None:
    """The newest admin's pass of the hold of merge request ``row_id`` at ``gate`` that still holds: no head came after
    it; None without one."""
    e = tables.merge_events
    query = (
        select(e.c.id, e.c.sha, e.c.reason, e.c.detail)
        .where(
            e.c.queue_id == row_id,
            e.c.kind == "gate",
            e.c.gate == gate,
            e.c.passed.is_(True),
            e.c.detail.has_key(PERSON),
        )
        .order_by(e.c.id.desc())
        .limit(1)
    )
    found = (await conn.execute(query)).first()
    if found is None:
        return None
    later = exists().where(e.c.queue_id == row_id, e.c.kind == "head", e.c.id > found.id)
    if (await conn.execute(select(later))).scalar_one():
        return None
    return Pass(found.id, found.sha, str(found.detail.get(PERSON)), found.reason or "")


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
