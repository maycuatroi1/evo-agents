"""The playbook of the Curator on the hub (``evo_agents.hub.playbook`` holds the model): its entries in
curator_playbook, and every delta in curator_playbook_deltas, never changed or deleted.

POST /v1/worker/runs/{id}/playbook takes one delta of the agent of a review run the worker holds (404 for any other
run), as the member who dispatched it, who must still hold writer on the project, as ``proposals.record_finding``
takes a finding: ``add`` a new entry (a lens of ``review.LENSES`` or none, its text, and 1 to MAX_EVIDENCE pieces of
evidence, which the hub resolves as a finding's: 422 for one it cannot find), stored with the project's default label;
``helpful`` or ``harmful`` one more on the counter of a live entry, with evidence when given, once each way for one run
(409 for the second); ``retire`` a live entry, with a note why. The project holds at most MAX_LIVE live entries, and a
run writes at most MAX_DELTAS deltas (409 past either). Each delta writes a line in the run's log.

GET /v1/projects/{p}/curator/playbook (reader) lists the entries the caller may read through the sink X-Evo-Sink names
(else the project's hub sink), the most helpful first, retired ones with ``retired``; the MCP tool curator_playbook and
``evo-agents hub curator playbook`` read it. ``for_prompt`` gives the lines of the playbook in a review run's prompt,
as its owner reads it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import playbook, review, tables
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.proposals import EvidenceIn, held_review_run, resolve, writer_of
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}


class PlaybookDelta(BaseModel):
    """One delta of the playbook (``evo_agents.hub.playbook.OPS``)."""

    op: Literal[playbook.OPS]
    entry_id: int | None = Field(None, ge=1, le=MAX_ID, description="helpful, harmful, retire: the entry")
    lens: Literal[tuple(review.LENSES)] | None = Field(None, description="add: the lens it is for; none: every lens")
    text: str | None = Field(None, min_length=1, max_length=playbook.MAX_TEXT_CHARS, description="add: the lesson")
    evidence: list[EvidenceIn] = Field(default_factory=list, max_length=review.MAX_EVIDENCE)
    note: str | None = Field(None, min_length=1, max_length=playbook.MAX_NOTE_CHARS, description="retire: why")

    @model_validator(mode="after")
    def _complete(self):
        if self.op == "add":
            if self.entry_id is not None or not self.text or not self.evidence:
                raise ValueError("add takes the text and at least one piece of evidence, and no entry_id")
            self.text = playbook.clean_text(self.text)
        else:
            if self.entry_id is None or self.text is not None or self.lens is not None:
                raise ValueError(f"{self.op} takes the entry_id of an entry, and neither text nor lens")
            if self.op == "retire" and not self.note:
                raise ValueError("retire takes a note: why the entry no longer holds")
        return self


class PlaybookEntry(BaseModel):
    id: int
    ref: str = Field(description="pb-<id>, as the prompt and the command line write it")
    lens: str | None
    text: str
    evidence: list[dict]
    helpful: int
    harmful: int
    created_run_id: int | None
    updated_run_id: int | None
    retired: bool
    retired_note: str | None
    created_at: datetime
    updated_at: datetime


class PlaybookView(BaseModel):
    project: str
    entries: list[PlaybookEntry] = Field(description="the most helpful less harmful first")
    live: int = Field(description="live entries the caller may read")
    retired: int = Field(description="retired entries the caller may read")


def _entries():
    pb = tables.curator_playbook
    return select(
        pb.c.id,
        pb.c.lens,
        pb.c.text,
        pb.c.evidence,
        pb.c.helpful,
        pb.c.harmful,
        pb.c.created_run_id,
        pb.c.updated_run_id,
        pb.c.retired_at,
        pb.c.retired_note,
        pb.c.created_at,
        pb.c.updated_at,
        pb.c.label,
    )


def _entry(row) -> PlaybookEntry:
    data = {key: value for key, value in row._mapping.items() if key not in ("label", "retired_at")}
    return PlaybookEntry(**data, ref=playbook.ref(row.id), retired=row.retired_at is not None)


async def _count(conn: AsyncConnection, *where) -> int:
    return (await conn.execute(select(func.count()).where(*where))).scalar_one()


async def _add(conn, request, run, access, body: PlaybookDelta) -> tuple[int, list[dict]]:
    pb = tables.curator_playbook
    if await _count(conn, pb.c.project_id == run.project_id, pb.c.retired_at.is_(None)) >= playbook.MAX_LIVE:
        raise HTTPException(
            409, f"project {run.project} has {playbook.MAX_LIVE} live entries in its playbook: retire one first"
        )
    evidence = await resolve(conn, request, run, access, body.evidence)
    values = {
        "project_id": run.project_id,
        "lens": body.lens,
        "text": body.text,
        "evidence": evidence,
        "label": access.push_label(),
        "created_run_id": run.id,
        "updated_run_id": run.id,
    }
    return (await conn.execute(insert(pb).values(**values).returning(pb.c.id))).scalar_one(), evidence


async def _change(conn, request, run, access, body: PlaybookDelta) -> list[dict]:
    pb, d = tables.curator_playbook, tables.curator_playbook_deltas
    row = (
        await conn.execute(
            select(pb.c.retired_at).where(pb.c.id == body.entry_id, pb.c.project_id == run.project_id).with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, f"the playbook of project {run.project} has no entry {playbook.ref(body.entry_id)}")
    if row.retired_at is not None:
        raise HTTPException(409, f"entry {playbook.ref(body.entry_id)} is retired: add a new entry instead")
    if body.op in playbook.TAGS:
        again = await _count(conn, d.c.entry_id == body.entry_id, d.c.run_id == run.id, d.c.op == body.op)
        if again:
            raise HTTPException(
                409, f"run {run.id} counted entry {playbook.ref(body.entry_id)} {body.op} already: once a run"
            )
    evidence = await resolve(conn, request, run, access, body.evidence) if body.evidence else []
    values: dict = {"updated_run_id": run.id, "updated_at": func.now()}
    if body.op in playbook.TAGS:
        values[body.op] = pb.c[body.op] + 1
    else:
        values.update(retired_at=func.now(), retired_note=body.note)
    await conn.execute(update(pb).values(**values).where(pb.c.id == body.entry_id))
    return evidence


@worker_router.post("/runs/{run_id}/playbook", status_code=201, response_model=PlaybookEntry, responses=REFUSALS)
async def record_delta(request: Request, run_id: RunId, body: PlaybookDelta, user: CurrentUser) -> PlaybookEntry:
    """One delta of the playbook from the agent of the review run this worker holds."""
    pb, d = tables.curator_playbook, tables.curator_playbook_deltas
    async with request.app.state.engine.begin() as conn:
        run = await held_review_run(conn, user, run_id)
        access = await writer_of(conn, run)
        if await _count(conn, d.c.run_id == run_id) >= playbook.MAX_DELTAS:
            raise HTTPException(
                409, f"run {run_id} wrote {playbook.MAX_DELTAS} deltas of the playbook, the most a run may"
            )
        if body.op == "add":
            entry_id, evidence = await _add(conn, request, run, access, body)
        else:
            entry_id, evidence = body.entry_id, await _change(conn, request, run, access, body)
        delta = {"project_id": run.project_id, "entry_id": entry_id, "run_id": run_id, "op": body.op}
        await conn.execute(insert(d).values(**delta, evidence=evidence, note=body.note))
        row = (await conn.execute(_entries().where(pb.c.id == entry_id))).one()
        what = {"add": "added", "helpful": "counted helpful", "harmful": "counted harmful", "retire": "retired"}
        text = f"playbook entry {playbook.ref(entry_id)} {what[body.op]}: {row.text}"
        await write_event(conn, run_id, {"text": text, "playbook": entry_id, "op": body.op})
    log.info("playbook delta", extra={"run_id": run_id, "entry_id": entry_id, "op": body.op})
    return _entry(row)


# Reading


async def entries_of(conn: AsyncConnection, access: ProjectAccess, through: str | None) -> list:
    pb = tables.curator_playbook
    rows = await conn.execute(_entries().where(pb.c.project_id == access.project_id).order_by(pb.c.id))
    return [row for row in rows if through is not None and access.visible(row.label, through)]


def _ordered(rows, retired: bool, lens: str | None) -> list:
    if lens is not None:
        rows = [row for row in rows if row.lens in (None, lens)]
    live = playbook.ranked([row._mapping for row in rows])
    gone = [row._mapping for row in rows if row.retired_at is not None] if retired else []
    return [*live, *gone]


async def playbook_view(
    conn: AsyncConnection, user: Principal, project: str, sink: str | None, *, lens: str | None, retired: bool
) -> PlaybookView:
    access = await project_access(conn, user, project)
    need_reader(access)
    rows = await entries_of(conn, access, plan_routes._sink(access, sink))
    by_id = {row.id: row for row in rows}
    shown = [_entry(by_id[item["id"]]) for item in _ordered(rows, retired, lens)]
    live = sum(1 for row in rows if row.retired_at is None)
    return PlaybookView(project=access.name, entries=shown, live=live, retired=len(rows) - live)


@router.get("/{project}/curator/playbook", response_model=PlaybookView, responses=READ_REFUSALS)
async def show_playbook(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    lens: Annotated[Literal[tuple(review.LENSES)] | None, Query(description="its entries and those of no lens")] = None,
    retired: Annotated[bool, Query(description="the retired entries too, after the live ones")] = False,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> PlaybookView:
    """The project's playbook of the Curator, the most helpful first."""
    async with request.app.state.engine.begin() as conn:
        return await playbook_view(conn, user, project, sink, lens=lens, retired=retired)


async def for_prompt(conn: AsyncConnection, access: ProjectAccess, lenses: list[str]) -> list[str]:
    """The lines of the playbook in the prompt of a review run of ``lenses``, as its owner (``access``) reads it."""
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    rows = await entries_of(conn, access, through)
    return playbook.prompt_lines([row._mapping for row in rows], lenses)
