"""The flow after an archive's upload (``evo_agents.hub.server.kg_pipeline``): reading where it stands from the rows
``evo_agents.hub.kg_pipeline`` derives it from, who may read it, and ``tell``, the notice once it ends, one transaction
per upload."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_pipeline as model
from evo_agents.hub.kg_build import REPLACED
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.pipeline import MAX_EARLIER_INITS, MAX_REQUEUED, MAX_TOLD, KgPipeline
from evo_agents.hub.server.kg.repository import pipeline as repository
from evo_agents.hub.server.projects import project_access

log = logging.getLogger("evo_agents.hub.server.kg_pipeline")


async def _build(conn: AsyncConnection, build_id: int | None, project_id: int) -> dict | None:
    """Build ``build_id``, or the build that took its place: a build whose worker stopped, or whose job ended without
    recording it (``kg_build.REPLACED``), gave its place to another of the project, so the flow goes on with the next
    build of the project."""
    if build_id is None:
        return None
    found = await repository.build(conn, project_id, build_id)
    for _ in range(MAX_REQUEUED):
        if found is None or found.status != "failed" or found.error not in REPLACED:
            break
        after = await repository.build_after(conn, project_id, found.id)
        if after is None:
            break
        found = after
    return None if found is None else dict(found._mapping)


async def _init(conn: AsyncConnection, project_id: int, build_id: int) -> tuple[dict | None, dict | None]:
    """(the newest kg_init run of the project on build ``build_id`` or later, which alone counts; else a run in flight
    from before)."""
    found = await repository.newest_init(conn, project_id, build_id)
    if found is not None:
        return dict(found._mapping), None
    active = await repository.active_init(conn, project_id)
    return None, None if active is None else dict(active._mapping)


async def _rebuild_id(conn: AsyncConnection, project_id: int, build_id: int, init: dict | None) -> int | None:
    """The build the decision of kg_init run ``init`` queued, else the one the newest kg_init run of the project on
    build ``build_id`` or later queued: a run asked for again after the first one's decision changes nothing, and the
    rebuild of that first decision still runs the entities step."""
    found = model.rebuild_of(init)
    if found is not None or init is None:
        return found
    for body in await repository.earlier_init_bodies(conn, project_id, build_id, init["id"], MAX_EARLIER_INITS):
        found = model.rebuild_of({"body": body})
        if found is not None:
            return found
    return None


async def read(conn: AsyncConnection, project: str, row) -> KgPipeline:
    """The flow of upload ``row`` (``repository.upload_columns``) of project ``project``."""
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
        rebuild = await _build(conn, await _rebuild_id(conn, row.project_id, build["id"], init), row.project_id)
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


async def pipeline(engine: AsyncEngine, user, project: str, upload_id: int) -> KgPipeline:
    """Where the flow after upload ``upload_id`` stands, for a grant on the project that sees the upload's label."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the uploads of project {project} needs a grant on it")
        row = await repository.upload(conn, access.project_id, upload_id)
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
    from evo_agents.hub.server.notifications.service.notifier import (
        NOTIFIER,  # notifications import the claim, which imports the KG
    )

    found = await read(conn, project, row)
    if found.state not in model.ENDED:
        return None
    if not await repository.mark_told(conn, row.id) or found.state == "replaced":
        return found.state
    admins = await repository.admins(conn, row.project_id)
    title, body = _notice(project, row.id, found)
    details = {"upload": row.id, "state": found.state, "phase": found.phase, "build": found.build_id}
    told: set[int] = {*admins, *([row.created_by] if row.created_by is not None else [])}
    for user_id in sorted(told):
        await NOTIFIER.notify(
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
    async with UnitOfWork(engine) as conn:
        rows = await repository.untold(conn, MAX_TOLD)
    outcomes: dict[str, int] = {}
    for row in rows:
        try:
            async with UnitOfWork(engine) as conn:
                word = await _tell_one(conn, row, row.project)
        except Exception:  # one upload failing leaves the others their turn
            log.exception("telling the end of a kg upload's flow failed", extra={"project": row.project, "id": row.id})
            word = "failed_to_tell"
        if word is not None:
            outcomes[word] = outcomes.get(word, 0) + 1
            log.info("kg upload flow ended", extra={"project": row.project, "upload_id": row.id, "state": word})
    return dict(sorted(outcomes.items()))
