"""The knowledge config of a project's graph and the sources it reads (``evo_agents.hub.server.kg``): PUT config saves
the config a build reads, refusing one that drops or raises above the hub sink a source the hub holds runs of, and
DELETE sources/{source} takes every run of a source off the hub first.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from evo_agents.hub import kg_upload_sources
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import REFUSALS, SourceId
from evo_agents.hub.server.kg.models.config import ConfigSaved, KgConfig, SourceForgotten
from evo_agents.hub.server.kg.service import config as service
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}})


@router.put("/{project}/config", response_model=ConfigSaved, responses=REFUSALS)
async def put_config(request: Request, project: ProjectName, body: KgConfig, user: CurrentUser) -> ConfigSaved:
    """Save the knowledge config the project's graph is built with."""
    saved = await service.save_config(request.app.state.engine, user, project, body)
    if saved.changed:
        log.info("kg config saved", extra={"project": project, "login": user.login, "digest": saved.digest})
    return saved


@router.delete("/{project}/sources/{source}", response_model=SourceForgotten, responses=REFUSALS)
async def forget_source(request: Request, project: ProjectName, source: SourceId, user: CurrentUser) -> SourceForgotten:
    """Take every run of a source off the hub, ingested or pending, and queue a build without them: how a source the
    hub holds runs of leaves knowledge.yaml (PUT config refuses to drop it before)."""
    state = request.app.state
    out, uploads = await service.forget(state.engine, state.jobs, user, project, source)
    if uploads.keys or uploads.multiparts:
        await asyncio.to_thread(kg_upload_sources.release, blob_store(request), uploads)
    log.info(
        "kg source forgotten",
        extra={"project": project, "login": user.login, "runs": out.ingested + out.pending, "logs": out.logs}
        | {"queued": out.queued, "uploads": out.uploads},
    )
    return out
