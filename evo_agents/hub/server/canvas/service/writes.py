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
caller holds the transaction and the lock of the canvas (``store.canvas(lock=True)``) and has checked the role; a
refusal raises CanvasError, and the transaction writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from sqlalchemy import func, insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.canvas import events, links, rules, store
from evo_agents.hub.server.canvas.content import LINKS, CanvasError, Content, changed, code, diff, number_of
from evo_agents.hub.server.canvas.store import CanvasRow, ClaimRow
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_canvas import DIRECTIONS, EVIDENCE_KINDS, MAX_REASON_CHARS, MAX_SUMMARY_CHARS, MAX_URL_CHARS

ADDED_KINDS = ("interview", "experiment", "market", "doc", "simulation")  # what a person or an agent adds
MAX_CHANGES = 100  # claims one edit or undo touches


@dataclass(frozen=True)
class Author:
    """The provenance of a write: a person, an agent or the hub; the member it is written for, the run of an agent
    with its runtime and model, and the chat message that asked for it."""

    actor: str = "person"
    user_id: int | None = None
    run_id: int | None = None
    runtime: str | None = None
    model: str | None = None
    chat_message_id: int | None = None

    def columns(self) -> dict:
        return {
            "actor": self.actor,
            "user_id": self.user_id,
            "run_id": self.run_id,
            "runtime": self.runtime,
            "model": self.model,
            "chat_message_id": self.chat_message_id,
        }


@dataclass(frozen=True)
class Writer:
    """Who writes, where, and through which sink they read."""

    access: ProjectAccess
    author: Author
    sink: str | None = None


def person(access: ProjectAccess, user_id: int, sink: str | None = None) -> Writer:
    """A member writing for themselves, on the web, the command line or the API."""
    return Writer(access, Author("person", user_id), sink)


@dataclass(frozen=True)
class Change:
    """One change of an edit: the fields ``values`` sets on claim ``claim`` read at ``base_rev``, or a new claim when
    ``claim`` is None (``values`` then gives its cell and text, and may give the rest)."""

    claim: str | None = None
    base_rev: int | None = None
    values: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    """What an edit or an undo did to one claim: its number, and whether it wrote a rev."""

    number: int
    written: bool


# Canvases


async def create_canvas(
    conn: AsyncConnection, writer: Writer, slug: str, name: str, description: str = "", label: dict | None = None
) -> None:
    """A new canvas of the project, under ``label`` (an admin's to give) or the project's default."""
    access, c = writer.access, tables.canvas.canvases
    taken = await conn.scalar(select(c.c.id).where(c.c.project_id == access.project_id, c.c.slug == slug))
    if taken is not None:
        raise CanvasError(409, f"project {access.name} has a canvas {slug} already")
    values = {
        "project_id": access.project_id,
        "slug": slug,
        "name": name,
        "description": description,
        "label": access.push_label(label),
        "created_by": writer.author.user_id,
    }
    await conn.execute(insert(c).values(**values))


async def set_archived(conn: AsyncConnection, access: ProjectAccess, user_id: int, canvas: CanvasRow, archived: bool):
    """Archive ``canvas`` or unarchive it; 409 when it is so already."""
    if (canvas.archived_at is not None) == archived:
        raise CanvasError(409, f"canvas {canvas.slug} of project {access.name} is {'' if archived else 'not '}archived")
    c = tables.canvas.canvases
    values = (
        {"archived_at": func.now(), "archived_by": user_id} if archived else {"archived_at": None, "archived_by": None}
    )
    await conn.execute(update(c).where(c.c.id == canvas.id).values(**values, updated_at=func.now()))


async def touch(conn: AsyncConnection, canvas: CanvasRow) -> None:
    c = tables.canvas.canvases
    await conn.execute(update(c).where(c.c.id == canvas.id).values(updated_at=func.now()))


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
    k, r = tables.canvas.claims, tables.canvas.revs
    archived_at = (row.archived_at or func.now()) if after.archived else None
    values = {
        "cell": after.cell,
        "position": after.position,
        "text": after.text,
        "status": after.status,
        "confidence": after.confidence,
        "delivery": after.delivery,
        "test_card": null() if after.test_card is None else after.test_card,
        "archived_at": archived_at,
        **{name: list(getattr(after, name)) for name in LINKS},
        "rev": k.c.rev + 1,
        "updated_at": func.now(),
    }
    moved = update(k).where(k.c.id == row.id, k.c.rev == row.rev).values(**values).returning(k.c.rev)
    rev = await conn.scalar(moved)
    if rev is None:
        raise CanvasError(409, f"{row.code} changed since rev {row.rev}: read it again")
    p = tables.canvas.proposals  # a proposal made at an older rev can no longer be accepted
    await conn.execute(update(p).where(p.c.claim_id == row.id, p.c.status == "open").values(status="stale"))
    change = _kind(row.content(), after, kind)
    await conn.execute(
        insert(r).values(
            claim_id=row.id,
            rev=rev,
            content=after.as_json(),
            change=change,
            undo_of=undo_of,
            reason=reason,
            baseline_id=baseline_id,
            **writer.author.columns(),
        )
    )
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


async def _next_number(conn: AsyncConnection, project_id: int) -> int:
    k = tables.canvas.claims
    last = await conn.scalar(select(func.max(k.c.number)).where(k.c.project_id == project_id))
    return (last or 0) + 1


async def _next_position(conn: AsyncConnection, canvas_id: int, cell: str) -> int:
    k = tables.canvas.claims
    last = await conn.scalar(select(func.max(k.c.position)).where(k.c.canvas_id == canvas_id, k.c.cell == cell))
    return 0 if last is None else last + 1


async def room(conn: AsyncConnection, canvas: CanvasRow, adding: int) -> None:
    """Refuse ``adding`` claims more than a canvas holds."""
    if not adding:
        return
    k = tables.canvas.claims
    held = await conn.scalar(select(func.count()).select_from(k).where(k.c.canvas_id == canvas.id))
    if held + adding > store.MAX_CLAIMS:
        raise CanvasError(
            422, f"canvas {canvas.slug} holds {held} claims; {adding} more would pass its {store.MAX_CLAIMS}"
        )


async def new_content(conn: AsyncConnection, canvas: CanvasRow, values: dict, *, archived_ok: bool = False) -> Content:
    """The content of a new claim of ``canvas``: ``values`` checked, at the end of its cell unless it names a
    position; archived only for an import (``archived_ok``)."""
    if "cell" not in values or "text" not in values:
        raise CanvasError(422, "a new claim takes a cell and a text")
    if values.get("archived") and not archived_ok:
        raise CanvasError(422, "a new claim is not archived")
    content = changed(Content(cell=values["cell"], position=0, text=values["text"]), values)
    if values.get("position") is None:
        content = replace(content, position=await _next_position(conn, canvas.id, content.cell))
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
    k, r = tables.canvas.claims, tables.canvas.revs
    number = number or await _next_number(conn, writer.access.project_id)
    values = {
        "project_id": writer.access.project_id,
        "canvas_id": canvas.id,
        "number": number,
        "cell": content.cell,
        "position": content.position,
        "text": content.text,
        "status": content.status,
        "confidence": content.confidence,
        "delivery": content.delivery,
        "test_card": null() if content.test_card is None else content.test_card,
        "archived_at": func.now() if content.archived else None,
        **{name: list(getattr(content, name)) for name in LINKS},
        "label": canvas.label,
        "created_by": writer.author.user_id,
    }
    claim_id = await conn.scalar(insert(k).values(**values).returning(k.c.id))
    revision = {"claim_id": claim_id, "rev": 1, "content": content.as_json(), "change": kind, "reason": reason}
    await conn.execute(insert(r).values(**revision, **writer.author.columns()))
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
        held[number] = await store.claim(conn, writer.access, canvas, number, lock=True)
    return held


async def conflict(conn: AsyncConnection, row: ClaimRow, base_rev: int) -> dict:
    """What a member reads when claim ``row`` changed since ``base_rev``: both contents, who changed it, the diff."""
    base = await store.rev(conn, row.id, base_rev) if base_rev < row.rev else None
    [last] = await store.revs(conn, row.id, since=row.rev)
    current = row.content()
    return {
        "claim": row.code,
        "base_rev": base_rev,
        "rev": row.rev,
        "changed_by": last.provenance.login or last.provenance.actor,
        "base": base.content.as_json() if base else None,
        "current": current.as_json(),
        "changes": diff(base.content, current) if base else [],
    }


async def check_revs(conn: AsyncConnection, held: dict[int, ClaimRow], asked: dict[int, int | None]) -> None:
    """409 with every claim of ``held`` whose rev is not the one ``asked`` names."""
    found = []
    for number, base_rev in asked.items():
        if base_rev is None:
            raise CanvasError(422, f"a change of {code(number)} names the rev it was read at (base_rev)")
        if held[number].rev != base_rev:
            found.append(await conflict(conn, held[number], base_rev))
    if found:
        names = ", ".join(item["claim"] for item in found)
        raise CanvasError(409, f"{names} changed since you read them: nothing was written; read them again", found)


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
    store.writable(writer.access, canvas)
    _check_reason(reason)
    if not changes or len(changes) > MAX_CHANGES:
        raise CanvasError(422, f"an edit takes 1 to {MAX_CHANGES} changes")
    named = [change for change in changes if change.claim is not None]
    numbers = unique_numbers([change.claim for change in named])
    held = await locked(conn, writer, canvas, numbers)
    await check_revs(conn, held, {n: change.base_rev for n, change in zip(numbers, named, strict=True)})
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
    number = await _next_number(conn, writer.access.project_id)
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
    store.writable(writer.access, canvas)
    _check_reason(reason)
    if not targets or len(targets) > MAX_CHANGES:
        raise CanvasError(422, f"an undo takes 1 to {MAX_CHANGES} claims")
    numbers = unique_numbers([ref for ref, _ in targets])
    held = await locked(conn, writer, canvas, numbers)
    await check_revs(conn, held, {n: rev for n, (_, rev) in zip(numbers, targets, strict=True)})
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


@dataclass(frozen=True)
class EvidenceInput:
    """A piece of evidence to add: its kind, which way it points, a summary, a link, the day it was taken, and
    whether it is synthetic (a simulation always is). The hub's own (the value check) also says what it rests on
    (``basis``), whether that is signals the agent of a run pushed (``rests_on_run``), and the label it carries when
    the signals it sums up carry a higher one than the claim's."""

    kind: str
    direction: str
    summary: str
    url: str | None = None
    observed_on: date | None = None
    synthetic: bool = False
    basis: dict | None = None
    rests_on_run: bool = False
    label: dict | None = None


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
    e = tables.canvas.evidence
    held = await conn.scalar(select(func.count()).select_from(e).where(e.c.claim_id == claim.id))
    if held >= store.MAX_EVIDENCE:
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
    return await conn.scalar(insert(e).values(**values).returning(e.c.id))


async def _evidence_row(conn: AsyncConnection, claim: ClaimRow, evidence_id: int) -> store.EvidenceRow:
    e = tables.canvas.evidence
    await conn.execute(select(e.c.id).where(e.c.id == evidence_id, e.c.claim_id == claim.id).with_for_update())
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
    e = tables.canvas.evidence
    values = {"withdrawn_at": func.now(), "withdrawn_by": writer.author.user_id, "withdrawn_reason": reason}
    await conn.execute(update(e).where(e.c.id == evidence_id).values(**values))


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
    e = tables.canvas.evidence
    values = {"confirmed_by": writer.author.user_id, "confirmed_at": func.now()}
    await conn.execute(update(e).where(e.c.id == evidence_id).values(**values))
