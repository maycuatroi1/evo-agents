"""A canvas as JSON and back: ``export`` writes ``evo-canvas/1`` (the canvas, every claim with its code, rev and
content, and the evidence the member may read), ``run_import`` reads it into the project.

An import keeps the codes of the claims: a claim the canvas holds already is changed as an edit is, at the rev the
file gives (the rev it was exported at), so a claim changed since makes the whole import a 409 and nothing is written;
a claim the project does not hold is made with the code the file gives, or the project's next when it gives none; a
code another canvas of the project holds refuses the import. Evidence a claim holds already (by id) is left; other
evidence is added as the member's, except what an agent added and nobody confirmed, which stays an agent's to
confirm. The kinds signal and feedback are the hub's to write and are left out, as is withdrawn evidence; each such
piece is a warning. So is each link to a use case or a signal key the project does not hold (``links.kept``), which is
left out. A dry run does all of it in a savepoint it rolls back, so it reports exactly what the import
would do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.canvas import links, rules, store, writes
from evo_agents.core_plugins.canvas.content import CanvasError, changed, code, number_of
from evo_agents.core_plugins.canvas.store import CanvasRow, ClaimRow, EvidenceRow
from evo_agents.core_plugins.canvas.writes import Author, EvidenceInput, Writer
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_canvas import (
    ACTORS,
    DIRECTIONS,
    EVIDENCE_KINDS,
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_URL_CHARS,
    SLUG,
)

FORMAT = "evo-canvas/1"
HUB_KINDS = rules.HUB_KINDS


class FileEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = Field(None, description="the evidence's id on the hub it was exported from")
    kind: Literal[EVIDENCE_KINDS]
    direction: Literal[DIRECTIONS]
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    url: str | None = Field(None, min_length=1, max_length=MAX_URL_CHARS)
    observed_on: date | None = None
    synthetic: bool = False
    actor: Literal[ACTORS] = "person"
    confirmed: bool = False
    withdrawn: bool = False


class FileClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str | None = Field(None, max_length=20, description="CL-<n>; left out: the project's next")
    rev: int | None = Field(None, ge=1, description="the rev it was exported at; needed for a claim the canvas holds")
    cell: str
    position: int | None = None
    text: str
    status: str = "assumption"
    confidence: str = "medium"
    test_card: dict | None = None
    archived: bool = False
    use_cases: list = Field(default_factory=list, description="the codes of the use cases it links")
    signal_keys: list = Field(default_factory=list, description="the signal keys it links")
    evidence: list[FileEvidence] = Field(default_factory=list, max_length=store.MAX_EVIDENCE)


class FileCanvas(BaseModel):
    model_config = ConfigDict(extra="ignore")

    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field("", max_length=MAX_DESCRIPTION_CHARS)


class CanvasFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    format: Literal["evo-canvas/1"]
    canvas: FileCanvas
    claims: list[FileClaim] = Field(default_factory=list, max_length=store.MAX_CLAIMS)


# Export


def _evidence_json(row: EvidenceRow) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "direction": row.direction,
        "summary": row.summary,
        "url": row.url,
        "observed_on": row.observed_on.isoformat() if row.observed_on else None,
        "synthetic": row.synthetic,
        "actor": row.provenance.actor,
        "confirmed": row.confirmed_by is not None,
        "withdrawn": row.withdrawn_at is not None,
    }


async def export(conn: AsyncConnection, access: ProjectAccess, canvas: CanvasRow, sink: str | None) -> dict:
    """``canvas`` as ``evo-canvas/1``: every claim, archived ones included, with its code, rev, content and the
    evidence the member may read."""
    rows = await store.claims(conn, canvas.id, with_archived=True)
    evidence = await store.evidence(conn, [row.id for row in rows])
    claims = []
    for row in rows:
        shown = [_evidence_json(item) for item in evidence[row.id] if store.visible(access, item.label, sink)]
        claims.append({"code": row.code, "rev": row.rev, **row.content().as_json(), "evidence": shown})
    return {
        "format": FORMAT,
        "exported_at": datetime.now(UTC),
        "project": access.name,
        "canvas": {
            "slug": canvas.slug,
            "name": canvas.name,
            "description": canvas.description,
            "label": canvas.label,
        },
        "claims": claims,
    }


# Import


@dataclass
class Report:
    """What an import did, or would do."""

    canvas: str
    canvas_created: bool = False
    created: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    evidence: int = 0
    problems: list[dict] = field(default_factory=list)

    def warn(self, claim: str | None, message: str) -> None:
        self.problems.append({"level": "warning", "claim": claim, "message": message})


@dataclass(frozen=True)
class Placed:
    """Where a claim of the file goes: the number it takes, and the claim of the canvas it changes (None: a new
    one)."""

    claim: FileClaim
    number: int | None
    existing: ClaimRow | None = None


def parse(data) -> CanvasFile:
    """``data`` read as ``evo-canvas/1``; 422 listing where it is wrong."""
    try:
        return CanvasFile.model_validate(data)
    except ValidationError as exc:
        detail = [
            {"level": "error", "claim": None, "message": f"{'.'.join(map(str, e['loc']))}: {e['msg']}"}
            for e in exc.errors()
        ]
        raise CanvasError(422, f"the file is no {FORMAT} canvas", detail) from None


async def _canvas(conn: AsyncConnection, writer: Writer, given: FileCanvas, label: dict | None, report: Report):
    try:
        return await store.canvas(conn, writer.access, given.slug, writer.sink, lock=True)
    except CanvasError as exc:
        if exc.status != 404:
            raise
    await writes.create_canvas(conn, writer, given.slug, given.name, given.description, label)
    report.canvas_created = True
    return await store.canvas(conn, writer.access, given.slug, writer.sink, lock=True)


async def _placed(conn: AsyncConnection, writer: Writer, canvas: CanvasRow, file: CanvasFile) -> list[Placed]:
    """Where each claim of ``file`` goes; 422 listing the claims that cannot go anywhere."""
    problems, placed, seen = [], [], set()
    for claim in file.claims:
        if claim.code is None:
            placed.append(Placed(claim, None))
            continue
        number = number_of(claim.code)
        where = await store.claim_anywhere(conn, writer.access.project_id, number)
        if number in seen:
            problems.append((code(number), "the file holds it twice"))
        elif where is not None and where[1] != canvas.id:
            problems.append((code(number), "another canvas of the project holds this code"))
        elif where is not None and claim.rev is None:
            problems.append((code(number), f"canvas {canvas.slug} holds it: give the rev it was exported at"))
        elif where is not None:
            placed.append(Placed(claim, number, await store.claim(conn, writer.access, canvas, number, lock=True)))
        else:
            placed.append(Placed(claim, number))
        seen.add(number)
    if problems:
        detail = [{"level": "error", "claim": ref, "message": message} for ref, message in problems]
        raise CanvasError(422, "the file has claims the project cannot take: nothing was imported", detail)
    return placed


def _values(claim: FileClaim) -> dict:
    values = {
        "cell": claim.cell,
        "text": claim.text,
        "status": claim.status,
        "confidence": claim.confidence,
        "test_card": claim.test_card,
        "archived": claim.archived,
        "use_cases": claim.use_cases,
        "signal_keys": claim.signal_keys,
    }
    if claim.position is not None:
        values["position"] = claim.position
    return values


async def _add_evidence(conn: AsyncConnection, writer: Writer, row: ClaimRow, claim: FileClaim, report: Report):
    """Add the evidence of ``claim`` that ``row`` does not hold, as the member's or, unconfirmed, an agent's."""
    held = {item.id for item in (await store.evidence(conn, [row.id]))[row.id]}
    for item in claim.evidence:
        if item.id is not None and item.id in held:
            continue
        if item.kind in HUB_KINDS or item.withdrawn:
            why = "withdrawn" if item.withdrawn else f"of kind {item.kind}, which the hub writes"
            report.warn(row.code, f"evidence {item.id or '(new)'} is {why}: left out")
            continue
        unconfirmed = item.actor == "agent" and not item.confirmed
        author = Author("agent" if unconfirmed else "person", writer.author.user_id)
        given = EvidenceInput(item.kind, item.direction, item.summary, item.url, item.observed_on, item.synthetic)
        await writes.add_evidence(conn, Writer(writer.access, author, writer.sink), row, given)
        report.evidence += 1


async def _change(conn: AsyncConnection, writer: Writer, place: Placed, report: Report) -> None:
    row = place.existing
    await _add_evidence(conn, writer, row, place.claim, report)
    after = changed(row.content(), _values(place.claim))
    after, left_out = await links.kept(conn, writer.access, writer.sink, row.content(), after)
    for why in left_out:
        report.warn(row.code, why)
    if after == row.content():
        report.unchanged.append(row.code)
        return
    evidence = await writes.facts(conn, [row.id])
    rules.check(row.number, row.content(), after, evidence[row.id])
    await writes.write_rev(conn, writer, row, after, "imported", "import")
    report.edited.append(row.code)


async def _make(conn: AsyncConnection, writer: Writer, canvas: CanvasRow, place: Placed, report: Report) -> None:
    content = await writes.new_content(conn, canvas, _values(place.claim), archived_ok=True)
    content, left_out = await links.kept(conn, writer.access, writer.sink, None, content)
    claim_id, number = await writes.insert_claim(
        conn, writer, canvas, content, "imported", "import", number=place.number
    )
    row = await store.claim(conn, writer.access, canvas, number, lock=True)
    for why in left_out:
        report.warn(row.code, why)
    await _add_evidence(conn, writer, row, place.claim, report)
    evidence = await writes.facts(conn, [claim_id])
    rules.check(number, None, content, evidence[claim_id])
    report.created.append(code(number))


async def _apply(conn: AsyncConnection, writer: Writer, file: CanvasFile, label: dict | None) -> Report:
    report = Report(canvas=file.canvas.slug)
    canvas = store.writable(writer.access, await _canvas(conn, writer, file.canvas, label, report))
    placed = await _placed(conn, writer, canvas, file)
    held = {place.number: place.existing for place in placed if place.existing is not None}
    await writes.check_revs(conn, held, {place.number: place.claim.rev for place in placed if place.existing})
    await writes.room(conn, canvas, sum(1 for place in placed if place.existing is None))
    for place in placed:
        try:
            if place.existing is not None:
                await _change(conn, writer, place, report)
            else:
                await _make(conn, writer, canvas, place, report)
        except CanvasError as exc:
            ref = code(place.number) if place.number else f"a claim of cell {place.claim.cell}"
            raise CanvasError(exc.status, f"{ref}: {exc.message}", exc.detail) from None
    await writes.touch(conn, canvas)
    return report


async def run_import(
    conn: AsyncConnection, writer: Writer, data, *, label: dict | None = None, dry_run: bool = False
) -> Report:
    """Import ``data`` (``evo-canvas/1``) into the project; with ``dry_run``, in a savepoint rolled back after."""
    file = parse(data)
    if not dry_run:
        return await _apply(conn, writer, file, label)
    nested = await conn.begin_nested()
    try:
        return await _apply(conn, writer, file, label)
    finally:
        await nested.rollback()
