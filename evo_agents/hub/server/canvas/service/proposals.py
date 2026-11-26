"""Proposals of a canvas: what an agent or the hub proposes instead of writing it (``propose``), the proposals a member
reads (``proposals``), and a writer's decision on one (``accept``, ``reject``).

A run that no member commanded, a task run of @strategist (its brainstorm), the value check (naming the evidence it
proposes a change of status on) and later the Loop, only proposes: a new claim, which starts as an assumption, or a change of a claim named at the rev it was read at
(``base_rev``, else a 409 as for an edit). The rules of ``rules`` are checked when it is made and again when it is
accepted, so an agent's proposal never moves a claim to validated, and so are the links it adds (``links``). A proposal
keeps the fields it sets, why, whether
it rests on simulated users (``synthetic``) and its provenance. A writer accepts it, which writes the rev as that
writer's own (kind ``proposal``; the proposal names the claim and the rev), or rejects it, with why; a writer rejects
open proposals of a canvas together too, with one reason for all (``reject_many``). Once its claim changes, an open
proposal goes stale (``writes.write_rev``) and is accepted no more; so do the open proposals the Loop's brainstorm of a
goal made once that goal's statement or metric changes (``brainstorms.stale_for_goal``, from ``server.goals.service``): they
were brainstormed for a goal that is no more. The caller holds the transaction and the lock of the canvas and has
checked the role; a refusal raises CanvasError and the transaction writes nothing.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.canvas.models import rules
from evo_agents.hub.server.canvas.models.content import CanvasError, Content, changed, diff, number_of
from evo_agents.hub.server.canvas.models.rows import CanvasRow, ProposalRow
from evo_agents.hub.server.canvas.models.writing import MAX_OPEN, MAX_PROPOSED, NEW_CLAIM_FIELDS, Change, Writer
from evo_agents.hub.server.canvas.repository import proposals as proposal_rows
from evo_agents.hub.server.canvas.repository import store
from evo_agents.hub.server.canvas.service import links, reads, writes
from evo_agents.hub.tables_canvas import MAX_REASON_CHARS


def _reason(reason: str) -> str:
    if not reason.strip() or len(reason) > MAX_REASON_CHARS:
        raise CanvasError(422, f"a proposal says why in 1 to {MAX_REASON_CHARS} characters")
    return reason


async def _room(conn: AsyncConnection, canvas: CanvasRow, adding: int) -> None:
    if not 1 <= adding <= MAX_PROPOSED:
        raise CanvasError(422, f"one call proposes 1 to {MAX_PROPOSED} changes")
    held = await proposal_rows.open_count(conn, canvas.id)
    if held + adding > MAX_OPEN:
        raise CanvasError(
            422,
            f"canvas {canvas.slug} has {held} open proposals; {adding} more would pass its {MAX_OPEN}: members accept "
            "or reject some first",
        )


async def _new_values(conn: AsyncConnection, writer: Writer, canvas: CanvasRow, values: dict) -> dict:
    """The fields a proposed new claim sets, checked as a new claim's: it starts as an assumption, not archived."""
    extra = sorted(set(values) - set(NEW_CLAIM_FIELDS))
    if extra:
        raise CanvasError(
            422, f"a proposed claim starts as an assumption and sets {', '.join(NEW_CLAIM_FIELDS)} only, not {extra[0]}"
        )
    content = await writes.new_content(conn, canvas, values)
    content = await links.resolved(conn, writer.access, writer.sink, None, content)
    shown = content.as_json()
    return {name: shown[name] for name in values}


async def propose(
    conn: AsyncConnection,
    writer: Writer,
    canvas: CanvasRow,
    changes: list[Change],
    reason: str,
    *,
    synthetic: bool = False,
    evidence_id: int | None = None,
) -> list[int]:
    """Keep ``changes`` as open proposals of ``canvas``, all or nothing: a change of a claim at the rev it was read
    at, else a 409; a new claim for a change that names none. Their ids, in the order of ``changes``. ``evidence_id``
    names the evidence a value check proposes on."""
    reads.writable(writer.access, canvas)
    _reason(reason)
    await _room(conn, canvas, len(changes))
    named = [change for change in changes if change.claim is not None]
    numbers = writes.unique_numbers([change.claim for change in named])
    held = await writes.locked(conn, writer, canvas, numbers)
    await writes.check_revs(
        conn, writer, canvas, held, {n: change.base_rev for n, change in zip(numbers, named, strict=True)}
    )
    evidence = await writes.facts(conn, [row.id for row in held.values()])
    common = {
        "canvas_id": canvas.id,
        "reason": reason,
        "synthetic": synthetic,
        "label": canvas.label,
        "evidence_id": evidence_id,
    }
    made = []
    for change in changes:
        if change.claim is None:
            if change.base_rev is not None:
                raise CanvasError(422, "a proposed new claim has no base_rev: name the claim a change is of")
            row = {"kind": "create", "fields": await _new_values(conn, writer, canvas, change.values)}
        else:
            claim = held[number_of(change.claim)]
            after = changed(claim.content(), change.values)
            after = await links.resolved(conn, writer.access, writer.sink, claim.content(), after)
            if after == claim.content():
                raise CanvasError(422, f"the proposal for {claim.code} leaves it as it is")
            rules.check(claim.number, claim.content(), after, evidence[claim.id], writer.author.actor)
            shown = after.as_json()
            fields = {name: shown[name] for name in change.values}
            row = {"kind": "change", "claim_id": claim.id, "base_rev": claim.rev, "fields": fields}
        made.append(await proposal_rows.add(conn, {**common, **row, **writer.author.columns()}))
    return made


async def proposals(conn: AsyncConnection, canvas: CanvasRow, statuses: tuple[str, ...] = ()) -> list[ProposalRow]:
    """The proposals of ``canvas`` of ``statuses`` (every status when none), newest first."""
    return await proposal_rows.proposals_of(conn, canvas.id, statuses)


async def proposal(conn: AsyncConnection, canvas: CanvasRow, proposal_id: int, *, lock: bool = False) -> ProposalRow:
    """Proposal ``proposal_id`` of ``canvas``; 404 when the canvas has none. ``lock`` takes its row lock."""
    found = await proposal_rows.proposal_of(conn, canvas.id, proposal_id, lock=lock)
    if found is None:
        raise CanvasError(404, f"canvas {canvas.slug} has no proposal {proposal_id}")
    return found


async def changes_of(conn: AsyncConnection, row: ProposalRow) -> list[dict]:
    """What ``row`` changes, field by field: from its claim at its base rev, or each field a new claim sets."""
    if row.kind == "create":
        content = Content.from_json({"position": 0, **row.fields})
        card = {f"test_card.{name}" for name in content.test_card or {}}
        return [item for item in diff(None, content) if item["field"] in {*row.fields, *card}]
    if row.claim_id is None or row.base_rev is None:
        return []
    base = await store.rev(conn, row.claim_id, row.base_rev)
    return diff(base.content, changed(base.content, row.fields)) if base is not None else []


def _open(row: ProposalRow) -> None:
    if row.status != "open":
        raise CanvasError(409, f"proposal {row.id} is {row.status}: only an open proposal is accepted or rejected")


def _person(writer: Writer, doing: str) -> None:
    if writer.author.actor != "person":
        raise CanvasError(403, f"only a member {doing} a proposal; an agent or a run does not")


async def _decided(conn: AsyncConnection, writer: Writer, proposal_id: int, **values) -> None:
    await proposal_rows.decided(conn, proposal_id, writer.author.user_id, **values)


async def accept(conn: AsyncConnection, writer: Writer, canvas: CanvasRow, proposal_id: int) -> tuple[int, int]:
    """Write open proposal ``proposal_id`` as ``writer``'s own rev: (the claim's number, the rev written)."""
    _person(writer, "accepts")
    reads.writable(writer.access, canvas)
    row = await proposal(conn, canvas, proposal_id, lock=True)
    _open(row)
    why = f"proposal {row.id}: {row.reason}"[:MAX_REASON_CHARS]
    if row.kind == "create":
        await writes.room(conn, canvas, 1)  # a new claim is an assumption: no rule of its status holds it back
        content = await writes.new_content(conn, canvas, row.fields)
        content = await links.resolved(conn, writer.access, writer.sink, None, content)
        claim_id, number = await writes.insert_claim(conn, writer, canvas, content, why, "proposal")
        rev = 1
        await _decided(conn, writer, row.id, status="accepted", claim_id=claim_id, rev=rev)
    else:
        number = number_of(row.claim)
        claim = await reads.claim(conn, writer.access, canvas, number, lock=True)
        if claim.rev != row.base_rev:  # writes.write_rev makes it stale first; a row written around it lands here
            raise CanvasError(
                409, f"{claim.code} changed since rev {row.base_rev}, which proposal {row.id} was made at"
            )
        after = await links.resolved(
            conn, writer.access, writer.sink, claim.content(), changed(claim.content(), row.fields)
        )
        evidence = await writes.facts(conn, [claim.id])
        rules.check(number, claim.content(), after, evidence[claim.id], writer.author.actor)
        rev = await writes.write_rev(conn, writer, claim, after, why, "proposal")  # stale, as every open one of it
        await _decided(conn, writer, row.id, status="accepted", rev=rev)
    await writes.touch(conn, canvas)
    return number, rev


async def reject(conn: AsyncConnection, writer: Writer, canvas: CanvasRow, proposal_id: int, reason: str = "") -> None:
    """Close open proposal ``proposal_id`` without writing it, with why when given."""
    _person(writer, "rejects")
    if len(reason) > MAX_REASON_CHARS:
        raise CanvasError(422, f"a reason takes at most {MAX_REASON_CHARS} characters")
    row = await proposal(conn, canvas, proposal_id, lock=True)
    _open(row)
    await _decided(conn, writer, row.id, status="rejected", decided_reason=reason.strip() or None)


async def reject_many(
    conn: AsyncConnection, writer: Writer, canvas: CanvasRow, proposal_ids: list[int], reason: str
) -> tuple[list[int], list[tuple[int, str]]]:
    """Close each open proposal of ``proposal_ids`` without writing it, with ``reason`` for all: (the ids rejected,
    (id, status) of each one no longer open, left as it is). A 404 when the canvas has no proposal of one of the ids,
    and nothing is rejected then."""
    _person(writer, "rejects")
    if not reason.strip() or len(reason) > MAX_REASON_CHARS:
        raise CanvasError(422, f"rejecting proposals together says why in 1 to {MAX_REASON_CHARS} characters")
    wanted = sorted(set(proposal_ids))
    if not 1 <= len(wanted) <= MAX_OPEN:
        raise CanvasError(422, f"one call rejects 1 to {MAX_OPEN} proposals")
    held = await proposal_rows.statuses(conn, canvas.id, wanted)
    missing = [number for number in wanted if number not in held]
    if missing:
        raise CanvasError(404, f"canvas {canvas.slug} has no proposal {missing[0]}")
    rejected = [number for number in wanted if held[number] == "open"]
    if rejected:
        await proposal_rows.reject_all(conn, rejected, writer.author.user_id, reason.strip())
    return rejected, [(number, held[number]) for number in wanted if held[number] != "open"]


__all__ = [
    "accept",
    "changes_of",
    "proposal",
    "proposals",
    "propose",
    "reject",
    "reject_many",
]
