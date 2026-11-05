"""Where the flow after an archive's upload stands, and the notice once it ends (plan kg-zip-to-answers, step 4).
``evo_agents.hub.kg_pipeline`` holds the phases without the database; this module reads the rows they come from.

- GET /v1/kg/{project}/uploads/{upload_id}/pipeline (``evo-agents hub kg pipeline --project P [--upload ID]``, and
  ``hub kg upload`` as it waits): the flow's state, the phase running or failed, each phase with its state and detail,
  and the build, kg_init run and rebuild it went through. A grant on the project that sees the upload's label reads it.
- ``tell``, called each minute by ``curator.collect`` after the kg_init runs moved: for each upload whose flow ended
  ready or failed and that nobody was told of (``kg_uploads.told_at``), one notice NOTICE to its sender and to the
  project's admins, through their channels, linking the graph or the Upload data page. A replaced upload is marked
  told with no notice.

The build is the one the upload queued; the kg_init run is the newest of the project whose build is that one or later
(a run takes the newest build as it starts), so a run asked for again after one failed, or one that followed a later
build, replaces it and only the newest counts; and the rebuild is the one its decision queued. A build whose worker
stopped queued the next one of the project (``kg_build.STOPPED``), which the flow follows in its place. Logs name the project and
the upload, never an item.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_pipeline as model
from evo_agents.hub import tables
from evo_agents.hub.kg_build import STOPPED
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.tables_kg import INIT_ACTIVE

log = logging.getLogger(__name__)

MAX_TOLD = 200  # uploads a pass of ``tell`` looks at
MAX_REQUEUED = 20  # builds stopped and queued again one after the other that a read follows

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})

UploadId = Annotated[int, PathParam(ge=1, description="the upload, as `hub kg uploads` lists it")]


class KgPipelinePhase(BaseModel):
    name: Literal["uploading", "extracting", "building", "entities", "questions", "curator"]
    state: Literal["pending", "running", "done", "skipped", "failed"]
    detail: str | None = Field(description="what it does or waits for, what it gave, or why it failed or was skipped")


class KgPipeline(BaseModel):
    upload_id: int
    project: str
    state: Literal["running", "ready", "failed", "replaced"]
    phase: str | None = Field(description="the phase running, or the one that failed; null once ready or replaced")
    phases: list[KgPipelinePhase] = Field(description="in the order they run")
    error: str | None = Field(description="why the failed phase failed")
    build_id: int | None = Field(description="the build the upload queued")
    init_id: int | None = Field(description="the kg_init run of the Curator after that build")
    rebuild_id: int | None = Field(description="the build the Curator's decision queued")
    told_at: datetime | None = Field(description="when the sender and the admins got the notice kg_pipeline")


def _upload_columns():
    u = tables.kg_uploads
    return (
        u.c.id,
        u.c.project_id,
        u.c.state,
        u.c.label,
        u.c.error,
        u.c.build_id,
        u.c.report,
        u.c.uploaded_at,
        u.c.created_by,
        u.c.told_at,
    )


async def _build(conn: AsyncConnection, build_id: int | None, project_id: int) -> dict | None:
    """Build ``build_id``, or the build that took its place: a build whose worker stopped (``kg_build.STOPPED``)
    queued another of the project, so the flow goes on with the next build of the project."""
    if build_id is None:
        return None
    b = tables.kg_builds
    columns = (b.c.id, b.c.status, b.c.error, b.c.nodes, b.c.edges, b.c.coverage)
    found = (await conn.execute(select(*columns).where(b.c.id == build_id, b.c.project_id == project_id))).first()
    for _ in range(MAX_REQUEUED):
        if found is None or found.status != "failed" or found.error != STOPPED:
            break
        after = (
            await conn.execute(
                select(*columns).where(b.c.project_id == project_id, b.c.id > found.id).order_by(b.c.id).limit(1)
            )
        ).first()
        if after is None:
            break
        found = after
    return None if found is None else dict(found._mapping)


async def _init(conn: AsyncConnection, project_id: int, build_id: int) -> tuple[dict | None, dict | None]:
    """(the newest kg_init run of the project on build ``build_id`` or later, which alone counts; else a run in flight
    from before)."""
    t = tables.kg_init_runs
    columns = (t.c.id, t.c.state, t.c.build_id, t.c.agent_run_id, t.c.body, t.c.error)
    found = (
        await conn.execute(
            select(*columns)
            .where(t.c.project_id == project_id, t.c.build_id >= build_id)
            .order_by(t.c.id.desc())
            .limit(1)
        )
    ).first()
    if found is not None:
        return dict(found._mapping), None
    active = (
        await conn.execute(select(*columns).where(t.c.project_id == project_id, t.c.state.in_(INIT_ACTIVE)))
    ).first()
    return None, None if active is None else dict(active._mapping)


async def read(conn: AsyncConnection, project: str, row) -> KgPipeline:
    """The flow of upload ``row`` (``_upload_columns``) of project ``project``."""
    report = row.report or {}
    upload = {
        "state": row.state,
        "error": row.error,
        "build_id": row.build_id,
        "uploaded_at": row.uploaded_at,
        "files": report.get("files"),
        "replaced_by": report.get("replaced_by"),
    }
    build = await _build(conn, row.build_id, row.project_id)
    init = waiting = rebuild = None
    if build is not None and build["status"] == "succeeded":
        init, waiting = await _init(conn, row.project_id, build["id"])
        rebuild = await _build(conn, model.rebuild_of(init), row.project_id)
    found = model.derive(upload, build, init, waiting=waiting, rebuild=rebuild)
    return KgPipeline(
        upload_id=row.id,
        project=project,
        **found,
        build_id=row.build_id,
        init_id=None if init is None else init["id"],
        rebuild_id=model.rebuild_of(init) if rebuild is None else rebuild["id"],
        told_at=row.told_at,
    )


@router.get(
    "/{project}/uploads/{upload_id}/pipeline",
    response_model=KgPipeline,
    responses={code: {"model": ErrorBody} for code in (403, 404)},
)
async def pipeline(request: Request, project: ProjectName, upload_id: UploadId, user: CurrentUser) -> KgPipeline:
    """Where the flow after upload ``upload_id`` stands."""
    u = tables.kg_uploads
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the uploads of project {project} needs a grant on it")
        row = (
            await conn.execute(
                select(*_upload_columns()).where(u.c.id == upload_id, u.c.project_id == access.project_id)
            )
        ).first()
        if row is None or not access.rules.visible_by_grant(row.label, access.max_level):
            raise HTTPException(404, f"project {project} has no upload {upload_id} your grant sees")
        return await read(conn, access.name, row)


def _project_link(project: str, state: str) -> str:
    return f"/p/{project}/kg" if state == "ready" else f"/p/{project}/kg/upload"


def _notice(project: str, upload_id: int, found: KgPipeline) -> tuple[str, str]:
    if found.state == "ready":
        title = f"Knowledge graph of {project} ready"
        done = ", ".join(f"{p.name} {p.state}" for p in found.phases)
        return title, f"The flow after upload #{upload_id} ended: {done}. Ask it with kg_search or the graph's page."
    title = f"Knowledge graph of {project} failed at {found.phase}"
    return title, f"The flow after upload #{upload_id} failed at the phase {found.phase}: {found.error}"


async def _tell_one(conn: AsyncConnection, row, project: str) -> str | None:
    """Notify the sender and the admins of the end of upload ``row``'s flow and mark it told; what it ended as, None
    while it moves."""
    from evo_agents.hub.server.notifications import notify  # notifications import the claim, which imports the KG

    found = await read(conn, project, row)
    if found.state not in model.ENDED:
        return None
    u = tables.kg_uploads
    marked = await conn.execute(
        update(u).where(u.c.id == row.id, u.c.told_at.is_(None)).values(told_at=func.now()).returning(u.c.id)
    )
    if marked.first() is None or found.state == "replaced":
        return found.state
    g = tables.grants
    admins = (
        await conn.execute(select(g.c.user_id).where(g.c.project_id == row.project_id, g.c.role == "admin"))
    ).scalars()
    title, body = _notice(project, row.id, found)
    details = {"upload": row.id, "state": found.state, "phase": found.phase, "build": found.build_id}
    told: set[int] = {*admins, *([row.created_by] if row.created_by is not None else [])}
    for user_id in sorted(told):
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=model.NOTICE,
            project_id=row.project_id,
            title=title,
            body=body,
            details=details,
            link=_project_link(project, found.state),
        )
    return found.state


async def tell(engine: AsyncEngine) -> dict:
    """One pass over the uploads nobody was told of yet; how many ended in each state."""
    u, p = tables.kg_uploads, tables.projects
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(*_upload_columns(), p.c.name.label("project"))
                .join_from(u, p, p.c.id == u.c.project_id)
                .where(u.c.told_at.is_(None), u.c.state != "uploading")
                .order_by(u.c.id)
                .limit(MAX_TOLD)
            )
        ).all()
    outcomes: dict[str, int] = {}
    for row in rows:
        try:
            async with engine.begin() as conn:
                word = await _tell_one(conn, row, row.project)
        except Exception:  # one upload failing leaves the others their turn
            log.exception("telling the end of a kg upload's flow failed", extra={"project": row.project, "id": row.id})
            word = "failed_to_tell"
        if word is not None:
            outcomes[word] = outcomes.get(word, 0) + 1
            log.info("kg upload flow ended", extra={"project": row.project, "upload_id": row.id, "state": word})
    return dict(sorted(outcomes.items()))
