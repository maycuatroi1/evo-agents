"""A member's decision on an item of a baseline (criterion a6 of plan living-lean-canvas; ``baselines``).

Who decides. A writer of the project, never the agent of a run (its routes refuse a run's token, and no tool of
@strategist decides), and never one of the item's ``proposers``: a member who changed the claim's status since the
status in effect, or accepted the proposal that did, or opened the baseline at once; an admin is no exception. With the
config's ``self_approval`` on, that last rule alone goes: a proposer decides the item as any writer does, and the item
records who decided it either way. What the value check or an overdue test card asks, the hub proposed: any other
writer decides it. An item is decided once.

What each decision does, in the transaction that records it:

- accepted: the status asked will take effect. On a change members made, nothing is written; the rule of validated runs
  again when it asks validated. On the value check's, the hub's proposal is accepted as the reviewer's rev.
- rejected: the status in effect stays: the hub writes a rev that takes the claim back to it when it is elsewhere, and
  rejects the value check's proposal. That rev can be undone like any other.
- deferred: nothing changes; the next baseline asks it again.
- extended (an overdue test card): a rev with the new deadline, a day after today.
- retired (an overdue test card): a rev that moves the claim to retired.

A rev a decision writes is of kind ``baseline`` and names the baseline, under the reviewer's name; the rules of status
hold as on every path. A decision other than deferred needs the claim at the status the item was proposed at, not
archived, else a 409 asks to defer it. Once no item waits, the baseline is approved (``baselines.approve``); an approved
or expired baseline takes no decision, so an approved one never changes. The caller holds the transaction and the
lock of the canvas.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.canvas import baselines, proposals, rules, store, writes
from evo_agents.hub.server.canvas.baselines import DECIDES, BaselineRow, ItemRow
from evo_agents.hub.server.canvas.content import CanvasError, changed, number_of
from evo_agents.hub.server.canvas.store import CanvasRow, ClaimRow
from evo_agents.hub.tables_canvas import MAX_REASON_CHARS


@dataclass(frozen=True)
class Decision:
    """What a reviewer decides of the item of one claim: one of ``baselines.DECIDES`` of its kind, the new deadline an
    extension takes, and why."""

    claim: str
    decision: str
    deadline: date | None = None
    reason: str = ""


def _why(row: BaselineRow, item: ItemRow, decision: Decision) -> str:
    said = f"baseline {row.number}: {item.code} {decision.decision}"
    return f"{said}: {decision.reason}"[:MAX_REASON_CHARS] if decision.reason else said


def _check(
    writer: writes.Writer, row: BaselineRow, item: ItemRow, decision: Decision, today: date, self_approval: bool
) -> None:
    if row.status != "proposed":
        raise CanvasError(409, f"baseline {row.number} is {row.status}: it takes no decision")
    if decision.decision not in DECIDES[item.kind]:
        allowed = ", ".join(DECIDES[item.kind]) or "none: it joins the snapshot as it is"
        raise CanvasError(422, f"the item of {item.code} is {item.kind}; its decisions are {allowed}")
    if item.decision is not None:
        raise CanvasError(409, f"the item of {item.code} in baseline {row.number} was {item.decision} already")
    if writer.author.user_id in item.barred(self_approval):
        raise CanvasError(
            403,
            f"you changed the status of {item.code} since the status in effect, accepted the proposal that did, or "
            f"opened baseline {row.number}: another writer decides it",
        )
    if (decision.decision == "extended") != (decision.deadline is not None):
        raise CanvasError(422, "extending an overdue test card takes its new deadline, and only extending does")
    if decision.deadline is not None and decision.deadline <= today:
        raise CanvasError(422, f"the new deadline {decision.deadline.isoformat()} is not after today, {today}")
    if len(decision.reason) > MAX_REASON_CHARS:
        raise CanvasError(422, f"a reason takes at most {MAX_REASON_CHARS} characters")


async def _claim(conn: AsyncConnection, writer: writes.Writer, canvas: CanvasRow, row: BaselineRow, item: ItemRow):
    """The item's claim, locked, while it is at the status the item was proposed at; 409 otherwise."""
    claim = await store.claim(conn, writer.access, canvas, item.number, lock=True)
    if claim.archived_at is not None or claim.status != item.at_status:
        now = "archived" if claim.archived_at is not None else f"at {claim.status}"
        raise CanvasError(
            409,
            f"{item.code} is {now} since baseline {row.number} was proposed with it at {item.at_status}: defer it, "
            "and the next baseline asks it again",
        )
    return claim


async def _write(conn, writer, claim: ClaimRow, values: dict, why: str, row: BaselineRow) -> int:
    after = changed(claim.content(), values)
    evidence = await writes.facts(conn, [claim.id])
    rules.check(claim.number, claim.content(), after, evidence[claim.id], writer.author.actor)
    return await writes.write_rev(conn, writer, claim, after, why, "baseline", baseline_id=row.id)


async def _accept(conn, writer, canvas: CanvasRow, claim: ClaimRow, item: ItemRow, why: str, row) -> int | None:
    if item.kind == "status":
        if item.to_status == "validated":  # the rule of validated, again, on the evidence as it is now
            evidence = await writes.facts(conn, [claim.id])
            before = replace(claim.content(), status=item.from_status)
            rules.check(claim.number, before, claim.content(), evidence[claim.id], writer.author.actor)
        return None
    found = await proposals.proposal(conn, canvas, item.proposal_id, lock=True)
    if found.status != "open" or found.base_rev != claim.rev:
        raise CanvasError(
            409, f"the value check's proposal {item.proposal_id} of {item.code} is {found.status}: defer it"
        )
    rev = await _write(conn, writer, claim, {"status": item.to_status}, why, row)
    p = tables.canvas.proposals
    decided = {"status": "accepted", "rev": rev, "decided_by": writer.author.user_id, "decided_at": func.now()}
    await conn.execute(update(p).where(p.c.id == found.id).values(**decided))
    return rev


async def _reject(conn, writer, claim: ClaimRow, item: ItemRow, decision: Decision, why: str, row) -> int | None:
    if item.kind == "value_check":
        p = tables.canvas.proposals
        closed = {"status": "rejected", "decided_by": writer.author.user_id, "decided_at": func.now()}
        closed["decided_reason"] = decision.reason.strip() or None
        await conn.execute(update(p).where(p.c.id == item.proposal_id, p.c.status == "open").values(**closed))
    if claim.status == item.from_status:
        return None
    return await _write(conn, writer, claim, {"status": item.from_status}, why, row)


async def _apply(conn, writer, canvas: CanvasRow, row: BaselineRow, item: ItemRow, decision: Decision) -> int | None:
    """What ``decision`` writes; the rev it wrote, if any."""
    if decision.decision == "deferred":
        return None
    claim = await _claim(conn, writer, canvas, row, item)
    why = _why(row, item, decision)
    if decision.decision == "accepted":
        return await _accept(conn, writer, canvas, claim, item, why, row)
    if decision.decision == "rejected":
        return await _reject(conn, writer, claim, item, decision, why, row)
    if decision.decision == "retired":
        return await _write(conn, writer, claim, {"status": "retired"}, why, row)
    card = {**(claim.test_card or {}), "deadline": decision.deadline.isoformat()}
    return await _write(conn, writer, claim, {"test_card": card}, why, row)


async def decide(
    conn: AsyncConnection, writer: writes.Writer, canvas: CanvasRow, number: int, decision: Decision, today: date
) -> BaselineRow:
    """Record ``writer``'s decision on the item of one claim of baseline ``number`` of ``canvas``, and approve the
    baseline when no item waits any more; the baseline as it is then."""
    if writer.author.actor != "person":
        raise CanvasError(403, "only a member decides an item of a baseline; an agent or a run does not")
    row = await baselines.baseline(conn, canvas, number, lock=True)
    held = {item.number: item for item in await baselines.items(conn, row.id)}
    item = held.get(number_of(decision.claim))
    if item is None:
        raise CanvasError(404, f"baseline {row.number} of canvas {canvas.slug} holds no item of {decision.claim}")
    _check(writer, row, item, decision, today, await baselines.self_approval(conn, writer.access.project_id))
    rev = await _apply(conn, writer, canvas, row, item, decision)
    i = tables.canvas.baseline_items
    values = {
        "decision": decision.decision,
        "decided_by": writer.author.user_id,
        "decided_at": func.now(),
        "new_deadline": decision.deadline,
        "reason": decision.reason.strip() or None,
        "rev": rev,
    }
    await conn.execute(update(i).where(i.c.id == item.id).values(**values))
    await writes.touch(conn, canvas)
    if not any(other.waiting for other in await baselines.items(conn, row.id)):
        await baselines.approve(conn, writer.access.project_id, canvas, row)
    return await baselines.baseline(conn, canvas, number)


__all__ = ["Decision", "decide"]
