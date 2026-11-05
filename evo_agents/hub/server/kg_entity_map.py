"""The entity maps of strategy entity-graph-agent on the hub (``evo_agents.hub.kg_entity_maps``).

- POST /v1/kg/{project}/entity-map {format: "evo-entity-map/1", model, entities, relations}: the agent of a run of
  this project (a run's token, ``Principal.acts_for_run``) with the writer role posts the map it made of the project
  (``evo_agents.kg.entity_map``). The hub checks each entry against its latest build as the run's sink reads it
  (AGENT_SINK, the agents' sink, as ``kg_run_access.sink`` reads a KG run's): an entry that cites a node that build
  lacks, or one above that sink's clearance, alike, is rejected with its reason, and the rest is kept as a blob of kind
  kg-entity-map, stamped with the run, and a build is queued, which reads every map the project holds; the run's entity
  map run, when it is one's (``kg_entity_map_run``), counts the map. A member's own credential is refused: a map comes
  from a run.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from evo_agents.hub import kg_entity_map_db
from evo_agents.hub import kg_entity_maps as maps
from evo_agents.hub import kg_entity_relay as relay
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.server import kg_run_access, project_archive
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import REFUSALS, queue, visible_nodes, writer
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg import entity_map

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}})
UNAVAILABLE = "the blob store did not answer; try again shortly"


class EntityMap(BaseModel):
    """A map of format evo-entity-map/1 (``evo_agents.kg.entity_map``); the hub stamps the run itself."""

    model_config = ConfigDict(extra="allow")

    format: str = Field(description=f"{entity_map.FORMAT}")
    model: str | None = Field(default=None, description="the model of the agent that read the project")
    entities: list[dict] = Field(default_factory=list)
    relations: list[dict] = Field(default_factory=list)


class Rejected(BaseModel):
    entry: str = Field(description="the entry, as entities[i] (name), relations[i] (src - dst) or a part of one")
    reason: str


class MapKept(BaseModel):
    entities: int = Field(description="entities kept")
    relations: int = Field(description="relations kept")
    mentions: int = Field(description="citations of documents, sections, plans and steps kept")
    code_links: int = Field(description="links to directories, files and symbols kept")
    rejected: list[Rejected]
    sha256: str | None = Field(description="the blob the map is kept as; None when nothing was kept")
    queued: bool = Field(description="whether a build was queued to read it; false when one was waiting already")


def _cited(found: dict) -> list[str]:
    ids = [m["node"] for e in found["entities"] for m in e["mentions"]]
    ids += [c["node"] for e in found["entities"] for c in e["code"]]
    return ids + [m["node"] for r in found["relations"] for m in r["mentions"]]


@router.post("/{project}/entity-map", response_model=MapKept, responses=REFUSALS | {409: {"model": ErrorBody}})
async def post_entity_map(request: Request, project: ProjectName, body: EntityMap, user: CurrentUser) -> MapKept:
    """Check a run's entity map against the latest build, keep what passes and queue a build that reads it."""
    if user.acts_for_run is None:
        raise HTTPException(
            403, "an entity map comes from a run of the project: post it with the run's token, inside the run"
        )
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        writer(access, "posting an entity map of")
    try:
        found, rejected = entity_map.parse(body.model_dump())
    except entity_map.EntityMapError as exc:
        raise HTTPException(422, str(exc)) from None
    sink = kg_run_access.sink(user, AGENT_SINK)
    nodes = await visible_nodes(request.app.state, access, user, project, sink, _cited(found))
    kept, refused = entity_map.check(found, nodes.get)
    rejected += refused
    kept["run"] = user.acts_for_run
    counts = {
        "entities": len(kept["entities"]),
        "relations": len(kept["relations"]),
        "mentions": sum(len(e["mentions"]) for e in kept["entities"]),
        "code_links": sum(len(e["code"]) for e in kept["entities"]),
    }
    shown = [Rejected(**r) for r in rejected]
    if not kept["entities"]:
        return MapKept(**counts, rejected=shown, sha256=None, queued=False)
    data, sha = entity_map.encode(kept)
    if len(data) > maps.MAX_MAP_BYTES:
        raise HTTPException(413, f"the map is {len(data)} bytes, over {maps.MAX_MAP_BYTES}: post it in parts")
    store = blob_store(request)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "entity-map.json"
        path.write_bytes(data)
        try:
            await asyncio.to_thread(store.put_file, sha, path)
            async with request.app.state.engine.begin() as conn:
                await relay.hold(conn, store, access.project_id, sha, path, len(data), maps.MAP_KIND, user.user_id)
                await project_archive.refuse_archived(conn, access.project_id, "its graph is not built again")
                await kg_entity_map_db.posted(conn, user.acts_for_run)  # the entity map run it belongs to, if any
                _, queued = await queue(request, conn, access.project_id, project, user.user_id)
        except BlobStoreUnavailable:
            raise HTTPException(503, UNAVAILABLE) from None
    log.info(
        "kg entity map kept",
        extra={"project": project, "run_id": user.acts_for_run, **counts, "rejected": len(rejected), "sha256": sha},
    )
    return MapKept(**counts, rejected=shown, sha256=sha, queued=queued)
