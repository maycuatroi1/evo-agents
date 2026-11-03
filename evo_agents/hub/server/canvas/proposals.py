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
goal made once that goal's statement or metric changes (``stale_for_goal``, from ``server.goals.write``): they were
brainstormed for a goal that is no more. The caller holds the transaction and the lock of the canvas and has checked
the role; a refusal raises CanvasError and the transaction writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.brainstorms import BRAINSTORM, brainstorm_runs, stale_for_goal
from evo_agents.hub.server.canvas import links, rules, store, writes
from evo_agents.hub.server.canvas.content import CanvasError, Content, changed, code, diff, number_of
from evo_agents.hub.server.canvas.store import CanvasRow, Provenance, provenance_of
from evo_agents.hub.tables_canvas import MAX_REASON_CHARS

MAX_PROPOSED = 50  # proposals one call makes
MAX_OPEN = 200  # open proposals of a canvas, and the most one call rejects together
NEW_CLAIM_FIELDS = (  # what a proposed new claim may set
    "cell",
    "text",
    "confidence",
    "delivery",
    "position",
    "test_card",
    "use_cases",
    "signal_keys",
    "problems",
    "opportunities",
)


@dataclass(frozen=True)
class ProposalRow:
    id: int
    kind: str
    claim_id: int | None
    claim: str | None  # the claim's code: the one it changes, or the one accepting it made
    base_rev: int | None
    fields: dict  # the fields of the claim it sets, checked
    reason: str
    synthetic: bool
    provenance: Provenance
    created_at: datetime
    status: str
    rev: int | None
    decided_by: str | None
    decided_at: datetime | None
    decided_reason: str | None
    evidence_id: int | None = None  # the evidence a value check proposed it on


def _reason(reason: str) -> str:
    if not reason.strip() or len(reason) > MAX_REASON_CHARS:
        raise CanvasError(422, f"a proposal says why in 1 to {MAX_REASON_CHARS} characters")
    return reason


async def _room(conn: AsyncConnection, canvas: CanvasRow, adding: int) -> None:
    if not 1 <= adding <= MAX_PROPOSED:
        raise CanvasError(422, f"one call proposes 1 to {MAX_PROPOSED} changes")
    p = tables.canvas.proposals
    query = select(func.count()).select_from(p).where(p.c.canvas_id == canvas.id, p.c.status == "open")
    held = await conn.scalar(query)
    if held + adding > MAX_OPEN:
        raise CanvasError(
            422,
            f"canvas {canvas.slug} has {held} open proposals; {adding} more would pass its {MAX_OPEN}: members accept "
            "or reject some first",
        )


async def _new_values(conn: AsyncConnection, writer: writes.Writer, canvas: CanvasRow, values: dict) -> dict:
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
    writer: writes.Writer,
    canvas: CanvasRow,
    changes: list[writes.Change],
    reason: str,
    *,
    synthetic: bool = False,
    evidence_id: int | None = None,
) -> list[int]:
    """Keep ``changes`` as open proposals of ``canvas``, all or nothing: a change of a claim at the rev it was read
    at, else a 409; a new claim for a change that names none. Their ids, in the order of ``changes``. ``evidence_id``
    names the evidence a value check proposes on."""
    store.writable(writer.access, canvas)
    _reason(reason)
    await _room(conn, canvas, len(changes))
    named = [change for change in changes if change.claim is not None]
    numbers = writes.unique_numbers([change.claim for change in named])
    held = await writes.locked(conn, writer, canvas, numbers)
    await writes.check_revs(conn, held, {n: change.base_rev for n, change in zip(numbers, named, strict=True)})
    evidence = await writes.facts(conn, [row.id for row in held.values()])
    p = tables.canvas.proposals
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
        statement = insert(p).values(**common, **row, **writer.author.columns()).returning(p.c.id)
        made.append(await conn.scalar(statement))
    return made


def _query(canvas_id: int):
    p, k, users = tables.canvas.proposals, tables.canvas.claims, tables.users
    decider = users.alias("decider")
    return (
        select(
            p.c.id,
            p.c.kind,
            p.c.claim_id,
            k.c.number,
            p.c.base_rev,
            p.c.fields,
            p.c.reason,
            p.c.synthetic,
            p.c.actor,
            users.c.login,
            p.c.run_id,
            p.c.runtime,
            p.c.model,
            p.c.chat_message_id,
            p.c.created_at,
            p.c.status,
            p.c.rev,
            decider.c.login.label("decided_by"),
            p.c.decided_at,
            p.c.decided_reason,
            p.c.evidence_id,
        )
        .select_from(
            p.outerjoin(k, k.c.id == p.c.claim_id)
            .outerjoin(users, users.c.id == p.c.user_id)
            .outerjoin(decider, decider.c.id == p.c.decided_by)
        )
        .where(p.c.canvas_id == canvas_id)
    )


def _row(found) -> ProposalRow:
    return ProposalRow(
        id=found.id,
        kind=found.kind,
        claim_id=found.claim_id,
        claim=code(found.number) if found.number is not None else None,
        base_rev=found.base_rev,
        fields=found.fields,
        reason=found.reason,
        synthetic=found.synthetic,
        provenance=provenance_of(found),
        created_at=found.created_at,
        status=found.status,
        rev=found.rev,
        decided_by=found.decided_by,
        decided_at=found.decided_at,
        decided_reason=found.decided_reason,
        evidence_id=found.evidence_id,
    )


async def proposals(conn: AsyncConnection, canvas: CanvasRow, statuses: tuple[str, ...] = ()) -> list[ProposalRow]:
    """The proposals of ``canvas`` of ``statuses`` (every status when none), newest first."""
    p = tables.canvas.proposals
    query = _query(canvas.id).order_by(p.c.id.desc())
    if statuses:
        query = query.where(p.c.status.in_(statuses))
    return [_row(found) for found in await conn.execute(query)]


async def proposal(conn: AsyncConnection, canvas: CanvasRow, proposal_id: int, *, lock: bool = False) -> ProposalRow:
    """Proposal ``proposal_id`` of ``canvas``; 404 when the canvas has none. ``lock`` takes its row lock."""
    p = tables.canvas.proposals
    if lock:
        await conn.execute(select(p.c.id).where(p.c.id == proposal_id, p.c.canvas_id == canvas.id).with_for_update())
    found = (await conn.execute(_query(canvas.id).where(p.c.id == proposal_id))).first()
    if found is None:
        raise CanvasError(404, f"canvas {canvas.slug} has no proposal {proposal_id}")
    return _row(found)


async def changes_of(conn: AsyncConnection, row: ProposalRow) -> list[dict]:
    """What ``row`` changes, field by field: from its claim at its base rev, or each field a new claim sets."""
    if row.kind == "create":
        content = Content.from_json({"position": 0, **row.fields})
        card = {f"test_card.{name}" for name in content.test_card or {}}
        return [item for item in diff(None, content) if item["field"] in {*row.fields, *card}]
    base = await store.rev(conn, row.claim_id, row.base_rev)
    return diff(base.content, changed(base.content, row.fields)) if base is not None else []


def _open(row: ProposalRow) -> None:
    if row.status != "open":
        raise CanvasError(409, f"proposal {row.id} is {row.status}: only an open proposal is accepted or rejected")


def _person(writer: writes.Writer, doing: str) -> None:
    if writer.author.actor != "person":
        raise CanvasError(403, f"only a member {doing} a proposal; an agent or a run does not")


async def _decided(conn: AsyncConnection, writer: writes.Writer, proposal_id: int, **values) -> None:
    p = tables.canvas.proposals
    decided = {"decided_by": writer.author.user_id, "decided_at": func.now()}
    await conn.execute(update(p).where(p.c.id == proposal_id).values(**decided, **values))


async def accept(conn: AsyncConnection, writer: writes.Writer, canvas: CanvasRow, proposal_id: int) -> tuple[int, int]:
    """Write open proposal ``proposal_id`` as ``writer``'s own rev: (the claim's number, the rev written)."""
    _person(writer, "accepts")
    store.writable(writer.access, canvas)
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
        claim = await store.claim(conn, writer.access, canvas, number, lock=True)
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


async def reject(
    conn: AsyncConnection, writer: writes.Writer, canvas: CanvasRow, proposal_id: int, reason: str = ""
) -> None:
    """Close open proposal ``proposal_id`` without writing it, with why when given."""
    _person(writer, "rejects")
    if len(reason) > MAX_REASON_CHARS:
        raise CanvasError(422, f"a reason takes at most {MAX_REASON_CHARS} characters")
    row = await proposal(conn, canvas, proposal_id, lock=True)
    _open(row)
    await _decided(conn, writer, row.id, status="rejected", decided_reason=reason.strip() or None)


async def reject_many(
    conn: AsyncConnection, writer: writes.Writer, canvas: CanvasRow, proposal_ids: list[int], reason: str
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
    p = tables.canvas.proposals
    query = select(p.c.id, p.c.status).where(p.c.canvas_id == canvas.id, p.c.id.in_(wanted)).order_by(p.c.id)
    held = {row.id: row.status for row in await conn.execute(query.with_for_update())}
    missing = [number for number in wanted if number not in held]
    if missing:
        raise CanvasError(404, f"canvas {canvas.slug} has no proposal {missing[0]}")
    rejected = [number for number in wanted if held[number] == "open"]
    if rejected:
        decided = {"decided_by": writer.author.user_id, "decided_at": func.now(), "decided_reason": reason.strip()}
        await conn.execute(update(p).where(p.c.id.in_(rejected)).values(status="rejected", **decided))
    return rejected, [(number, held[number]) for number in wanted if held[number] != "open"]


__all__ = [
    "BRAINSTORM",
    "MAX_OPEN",
    "MAX_PROPOSED",
    "ProposalRow",
    "accept",
    "brainstorm_runs",
    "changes_of",
    "proposal",
    "proposals",
    "propose",
    "reject",
    "reject_many",
    "stale_for_goal",
]
