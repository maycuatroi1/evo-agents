"""Keeping canvases: a new canvas, archiving one, an edit of its claims (``edit``), an undo (``undo``), and evidence
(``add_evidence``, ``withdraw_evidence``, ``confirm_evidence``).

Every change of a claim is a new rev with the claim's whole content and its provenance (``Author``). An edit names
each claim it changes with the rev it was read at (``base_rev``): the claim's rev goes up by an UPDATE that holds only
while the rev is still that one, in the transaction that writes the rev, and a claim changed since makes the whole
edit a 409 whose detail holds, per claim, the rev asked for, the rev it is at, who changed it, both contents and what
differs. An undo takes back the claim's current rev by writing the content of the rev before it; undoing a first rev
archives the claim. The rules of ``rules`` hold on every path, and a link an edit adds is checked against the project
(``links``). Every rev emits the event ``canvas.claim``
(``events``) in its transaction. Evidence is no part of a rev: it is only added, and withdrawn by marking it. The
caller holds the transaction and the lock of the canvas (``reads.canvas(lock=True)``) and has checked the role; a
refusal raises CanvasError, and the transaction writes nothing.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import null
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.canvas.models import rules
from evo_agents.hub.server.canvas.models.content import CanvasError, Content, changed, code, number_of
from evo_agents.hub.server.canvas.models.rows import CanvasRow, ClaimRow, EvidenceRow
from evo_agents.hub.server.canvas.models.writing import (
    ADDED_KINDS,
    MAX_CHANGES,
    MAX_CLAIMS,
    MAX_EVIDENCE,
    Author,
    Change,
    EvidenceInput,
    NextRev,
    Outcome,
    Writer,
)
from evo_agents.hub.server.canvas.repository import claims, store
from evo_agents.hub.server.canvas.repository import evidence as evidence_rows
from evo_agents.hub.server.canvas.repository.claims import ClaimStore
from evo_agents.hub.server.canvas.service import events, links, reads
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_canvas import DIRECTIONS, EVIDENCE_KINDS, MAX_REASON_CHARS, MAX_SUMMARY_CHARS, MAX_URL_CHARS


def person(access: ProjectAccess, user_id: int, sink: str | None = None) -> Writer:
    """A member writing for themselves, on the web, the command line or the API."""
    return Writer(access, Author("person", user_id), sink)


# Canvases


async def create_canvas(
    conn: AsyncConnection, writer: Writer, slug: str, name: str, description: str = "", label: dict | None = None
) -> None:
    """A new canvas of the project, under ``label`` (an admin's to give) or the project's default."""
    access = writer.access
    if await store.slug_taken(conn, access.project_id, slug):
        raise CanvasError(409, f"project {access.name} has a canvas {slug} already")
    values = {
        "project_id": access.project_id,
        "slug": slug,
        "name": name,
        "description": description,
        "label": access.push_label(label),
        "created_by": writer.author.user_id,
    }
    await store.insert_canvas(conn, values)


async def set_archived(conn: AsyncConnection, access: ProjectAccess, user_id: int, canvas: CanvasRow, archived: bool):
    """Archive ``canvas`` or unarchive it; 409 when it is so already."""
    if (canvas.archived_at is not None) == archived:
        raise CanvasError(409, f"canvas {canvas.slug} of project {access.name} is {'' if archived else 'not '}archived")
    await store.set_archived(conn, canvas.id, user_id, archived)


async def touch(conn: AsyncConnection, canvas: CanvasRow) -> None:
    await store.touch(conn, canvas.id)


# Revs


def _kind(before: Content, after: Content, kind: str) -> str:
    if kind == "edit" and before.archived != after.archived:
        return "archive" if after.archived else "unarchive"
    return kind


async def write_rev(
    conn: AsyncConnection,
    writer: Writer,
    row: ClaimRow,
    after: Content,
    reason: str,
    kind: str,
    *,
    undo_of: int | None = None,
    baseline_id: int | None = None,
) -> int:
    """Move claim ``row`` to ``after`` as its next rev, while it is still at ``row.rev``; the new rev. The open
    proposals of the claim go stale: they were made at a rev it no longer is at. ``baseline_id`` names the baseline
    whose decision writes it (kind ``baseline``)."""
    change = _kind(row.content(), after, kind)
    rev = row.rev + 1
    values = NextRev(after, change, reason, writer.author, undo_of=undo_of, baseline_id=baseline_id)
    await ClaimStore(writer.access.project_id, row.canvas_id).insert(conn, row.number, row, rev, values)
    moved = {"rev": rev, "cell": after.cell, "change": change, "before": row.status, "after": after.status}
    await events.claim_rev(
        conn,
        writer.access.project_id,
        writer.author,
        canvas_id=row.canvas_id,
        claim_id=row.id,
        number=row.number,
        **moved,
    )
    return rev


async def room(conn: AsyncConnection, canvas: CanvasRow, adding: int) -> None:
    """Refuse ``adding`` claims more than a canvas holds."""
    if not adding:
        return
    held = await claims.claim_count(conn, canvas.id)
    if held + adding > MAX_CLAIMS:
        raise CanvasError(422, f"canvas {canvas.slug} holds {held} claims; {adding} more would pass its {MAX_CLAIMS}")


async def new_content(conn: AsyncConnection, canvas: CanvasRow, values: dict, *, archived_ok: bool = False) -> Content:
    """The content of a new claim of ``canvas``: ``values`` checked, at the end of its cell unless it names a
    position; archived only for an import (``archived_ok``)."""
    if "cell" not in values or "text" not in values:
        raise CanvasError(422, "a new claim takes a cell and a text")
    if values.get("archived") and not archived_ok:
        raise CanvasError(422, "a new claim is not archived")
    content = changed(Content(cell=values["cell"], position=0, text=values["text"]), values)
    if values.get("position") is None:
        content = replace(content, position=await claims.next_position(conn, canvas.id, content.cell))
    return content


async def insert_claim(
    conn: AsyncConnection,
    writer: Writer,
    canvas: CanvasRow,
    content: Content,
    reason: str,
    kind: str,
    *,
    number: int | None = None,
) -> tuple[int, int]:
    """Keep a new claim of ``canvas`` at rev 1, numbered ``number`` or the project's next; (its id, its number)."""
    number = number or await claims.next_number(conn, writer.access.project_id)
    values = NextRev(content, kind, reason, writer.author)
    claim_id = await ClaimStore(writer.access.project_id, canvas.id).create(conn, canvas, number, content, values)
    made = {"rev": 1, "cell": content.cell, "change": kind, "before": None, "after": content.status}
    await events.claim_rev(
        conn, writer.access.project_id, writer.author, canvas_id=canvas.id, claim_id=claim_id, number=number, **made
    )
    return claim_id, number


# Edits and undos


def unique_numbers(refs: list[str]) -> list[int]:
    """The numbers ``refs`` name, in their order; 422 when one is named twice."""
    numbers = [number_of(ref) for ref in refs]
    repeated = sorted({number for number in numbers if numbers.count(number) > 1})
    if repeated:
        raise CanvasError(422, f"one request changes each claim once; {', '.join(map(code, repeated))} is named twice")
    return numbers


async def locked(conn: AsyncConnection, writer: Writer, canvas: CanvasRow, numbers: list[int]) -> dict[int, ClaimRow]:
    """The claims ``numbers`` of ``canvas``, each row locked, in the order of their numbers; 404 for one it lacks."""
    held = {}
    for number in sorted(numbers):
        held[number] = await reads.claim(conn, writer.access, canvas, number, lock=True)
    return held


async def check_revs(
    conn: AsyncConnection, writer: Writer, canvas: CanvasRow, held: dict[int, ClaimRow], asked: dict[int, int | None]
) -> None:
    """422 for the first claim of ``asked`` that names no rev; 409 with every claim of ``held`` whose rev is not the
    one ``asked`` names (``ClaimStore.check_all``)."""
    named: dict[int, int] = {}
    for number, base_rev in asked.items():
        if base_rev is None:
            raise CanvasError(422, f"a change of {code(number)} names the rev it was read at (base_rev)")
        named[number] = base_rev
    await ClaimStore(writer.access.project_id, canvas.id).check_all(conn, held, named)


async def facts(conn: AsyncConnection, claim_ids: list[int]) -> dict[int, list[rules.Evidence]]:
    """The evidence of each claim, as the rules read it."""
    return {
        claim_id: [row.facts() for row in rows] for claim_id, rows in (await store.evidence(conn, claim_ids)).items()
    }


def _check_reason(reason: str) -> str:
    if len(reason) > MAX_REASON_CHARS:
        raise CanvasError(422, f"a reason takes at most {MAX_REASON_CHARS} characters")
    return reason


async def edit(
    conn: AsyncConnection, writer: Writer, canvas: CanvasRow, changes: list[Change], reason: str = "", *, kind="edit"
) -> list[Outcome]:
    """Apply ``changes`` to ``canvas`` in one go: every claim named at the rev it was read at, else a 409 and nothing
    written; a new claim for each change that names none. A change that leaves a claim as it is writes no rev."""
    reads.writable(writer.access, canvas)
    _check_reason(reason)
    if not changes or len(changes) > MAX_CHANGES:
        raise CanvasError(422, f"an edit takes 1 to {MAX_CHANGES} changes")
    named = [change for change in changes if change.claim is not None]
    numbers = unique_numbers([change.claim for change in named])
    held = await locked(conn, writer, canvas, numbers)
    await check_revs(conn, writer, canvas, held, {n: change.base_rev for n, change in zip(numbers, named, strict=True)})
    await room(conn, canvas, len(changes) - len(named))
    evidence = await facts(conn, [row.id for row in held.values()])
    outcomes = []
    for change in changes:
        if change.claim is None:
            outcomes.append(await _create(conn, writer, canvas, change, reason, kind))
            continue
        row = held[number_of(change.claim)]
        after = await links.resolved(
            conn, writer.access, writer.sink, row.content(), changed(row.content(), change.values)
        )
        if after == row.content():
            outcomes.append(Outcome(row.number, False))
            continue
        rules.check(row.number, row.content(), after, evidence[row.id], writer.author.actor)
        await write_rev(conn, writer, row, after, reason, kind)
        outcomes.append(Outcome(row.number, True))
    await touch(conn, canvas)
    return outcomes


async def _create(
    conn: AsyncConnection, writer: Writer, canvas: CanvasRow, change: Change, reason: str, kind: str
) -> Outcome:
    if change.base_rev is not None:
        raise CanvasError(422, "a new claim has no base_rev: name the claim a change is of")
    content = await links.resolved(
        conn, writer.access, writer.sink, None, await new_content(conn, canvas, change.values)
    )
    number = await claims.next_number(conn, writer.access.project_id)
    rules.check(number, None, content, [], writer.author.actor)
    await insert_claim(conn, writer, canvas, content, reason, "create" if kind == "edit" else kind, number=number)
    return Outcome(number, True)


async def undone(conn: AsyncConnection, row: ClaimRow, since: int | None = None) -> Content:
    """The content taking back the revs of ``row`` from ``since`` (its current when None) gives: the rev before
    them, or, from its first, the claim as it is now archived."""
    since = row.rev if since is None else since
    if since == 1:
        if row.archived_at is not None:
            raise CanvasError(409, f"{row.code} is at its first rev and archived already: there is nothing to undo")
        return replace(row.content(), archived=True)
    before = await store.rev(conn, row.id, since - 1)
    if before is None:  # a rev is never deleted; this is a broken row
        raise CanvasError(409, f"{row.code} has no rev {since - 1} to go back to")
    return before.content


async def undo(
    conn: AsyncConnection, writer: Writer, canvas: CanvasRow, targets: list[tuple[str, int]], reason: str = ""
) -> list[Outcome]:
    """Take back rev ``rev`` of each claim of ``targets`` (claim, rev) in one go, each the claim's current rev, else
    a 409 and nothing written: a new rev with the content of the rev before it, ``undo_of`` naming the rev."""
    reads.writable(writer.access, canvas)
    _check_reason(reason)
    if not targets or len(targets) > MAX_CHANGES:
        raise CanvasError(422, f"an undo takes 1 to {MAX_CHANGES} claims")
    numbers = unique_numbers([ref for ref, _ in targets])
    held = await locked(conn, writer, canvas, numbers)
    await check_revs(conn, writer, canvas, held, {n: rev for n, (_, rev) in zip(numbers, targets, strict=True)})
    evidence = await facts(conn, [row.id for row in held.values()])
    outcomes = []
    for number in numbers:
        row = held[number]
        after = await undone(conn, row)
        rules.check(row.number, row.content(), after, evidence[row.id], writer.author.actor)
        await write_rev(conn, writer, row, after, reason, "undo", undo_of=row.rev)
        outcomes.append(Outcome(number, True))
    await touch(conn, canvas)
    return outcomes


# Evidence


def _check_evidence(writer: Writer, given: EvidenceInput) -> None:
    hub = writer.author.actor == "hub"
    kinds = EVIDENCE_KINDS if hub else ADDED_KINDS
    if given.kind not in kinds:
        raise CanvasError(422, f"evidence added here is of kind {', '.join(kinds)}; {given.kind} is the hub's to write")
    if not hub and (given.basis is not None or given.rests_on_run or given.label is not None):
        raise CanvasError(422, "only the hub says what evidence rests on")
    if given.direction not in DIRECTIONS:
        raise CanvasError(422, f"evidence points one of {', '.join(DIRECTIONS)}; {given.direction!r} is none")
    if not given.summary.strip() or len(given.summary) > MAX_SUMMARY_CHARS:
        raise CanvasError(422, f"a summary of evidence takes 1 to {MAX_SUMMARY_CHARS} characters")
    if given.url is not None and not 1 <= len(given.url) <= MAX_URL_CHARS:
        raise CanvasError(422, f"a link of evidence takes 1 to {MAX_URL_CHARS} characters")


async def add_evidence(conn: AsyncConnection, writer: Writer, claim: ClaimRow, given: EvidenceInput) -> int:
    """Keep a piece of evidence of ``claim`` under the claim's label; its id. The claim's rev stays."""
    _check_evidence(writer, given)
    held = await evidence_rows.count(conn, claim.id)
    if held >= MAX_EVIDENCE:
        raise CanvasError(422, f"{claim.code} holds {held} pieces of evidence, as many as a claim holds")
    values = {
        "claim_id": claim.id,
        "kind": given.kind,
        "direction": given.direction,
        "summary": given.summary,
        "url": given.url,
        "observed_on": given.observed_on,
        "synthetic": given.synthetic or given.kind == "simulation",
        "label": given.label or claim.label,
        "basis": null() if given.basis is None else given.basis,  # SQL NULL, not a JSON null
        "rests_on_run": given.rests_on_run,
        **writer.author.columns(),
    }
    return await evidence_rows.add(conn, values)


async def _evidence_row(conn: AsyncConnection, claim: ClaimRow, evidence_id: int) -> EvidenceRow:
    await evidence_rows.lock(conn, claim.id, evidence_id)
    found = [row for row in (await store.evidence(conn, [claim.id]))[claim.id] if row.id == evidence_id]
    if not found:
        raise CanvasError(404, f"{claim.code} has no evidence {evidence_id}")
    return found[0]


async def withdraw_evidence(conn: AsyncConnection, writer: Writer, claim: ClaimRow, evidence_id: int, reason: str):
    """Mark evidence ``evidence_id`` of ``claim`` withdrawn, why and by whom; it stays, and counts no more."""
    row = await _evidence_row(conn, claim, evidence_id)
    if row.withdrawn_at is not None:
        raise CanvasError(409, f"evidence {evidence_id} of {claim.code} was withdrawn already")
    if not reason.strip() or len(reason) > MAX_REASON_CHARS:
        raise CanvasError(422, f"withdrawing evidence takes a reason of 1 to {MAX_REASON_CHARS} characters")
    await evidence_rows.withdraw(conn, evidence_id, writer.author.user_id, reason)


async def confirm_evidence(conn: AsyncConnection, writer: Writer, claim: ClaimRow, evidence_id: int) -> None:
    """A writer vouches for evidence ``evidence_id`` an agent added to ``claim``, so it may validate the claim."""
    if writer.author.actor != "person":
        raise CanvasError(403, "only a person confirms evidence an agent added")
    row = await _evidence_row(conn, claim, evidence_id)
    if row.provenance.actor != "agent":
        raise CanvasError(
            409, f"evidence {evidence_id} of {claim.code} was not added by an agent: it needs no confirming"
        )
    if row.confirmed_by is not None:
        raise CanvasError(409, f"evidence {evidence_id} of {claim.code} was confirmed by {row.confirmed_by} already")
    if row.withdrawn_at is not None:
        raise CanvasError(409, f"evidence {evidence_id} of {claim.code} was withdrawn")
    await evidence_rows.confirm(conn, evidence_id, writer.author.user_id)
