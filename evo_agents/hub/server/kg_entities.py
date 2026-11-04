"""The entities step's requests and answers on the hub (``evo_agents.hub.kg_entity_relay``).

- GET /v1/kg/{project}/entities/requests: the items the latest succeeded build deferred to a run that has Claude Code,
  each with its key, item, text, node kinds, relations and model. The writer role: the text is the text of the
  project's items that the mapping's llm-backend sink cleared.
- POST /v1/kg/{project}/entities/answers {answers: {key: {nodes, edges, usage}}}: the agent of a run of this project
  (a run's token, ``Principal.acts_for_run``) with the writer role posts answers to those requests; the hub keeps those
  whose key the requests name as a kg-entity-answers blob and queues a build, which reads them. A member's own
  credential is refused: the answers come from the login of the project's agent pool, never from a personal key.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from evo_agents.hub import kg_entity_relay as relay
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server import project_archive
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import blob_store, fetch_bytes
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import REFUSALS, queue, writer
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}})
UNAVAILABLE = "the blob store did not answer; try again shortly"


class EntityRequest(BaseModel):
    key: str
    item: str | None = None
    text: str
    node_kinds: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    model: str


class EntityRequests(BaseModel):
    build_id: int | None = Field(description="the build that deferred them; None when the latest deferred nothing")
    requests: list[EntityRequest]


class EntityAnswers(BaseModel):
    answers: dict[str, dict] = Field(description="{key of a request: {nodes, edges, usage}}")


class AnswersKept(BaseModel):
    kept: int = Field(description="answers whose key the latest requests name")
    ignored: int = Field(description="answers to no request of the latest build")
    queued: bool = Field(description="whether a build was queued to read them; false when one was waiting already")


async def _requests(request: Request, project_id: int) -> tuple[int | None, list[dict]]:
    async with request.app.state.engine.begin() as conn:
        found = await relay.latest_requests(conn, project_id)
    if found is None:
        return None, []
    build_id, sha, _ = found
    try:
        data = await asyncio.to_thread(fetch_bytes, blob_store(request), sha, relay.MAX_REQUESTS_BYTES)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    if data is None:
        raise HTTPException(503, f"the entity requests of build {build_id} are not in the blob store")
    try:
        return build_id, relay.parse_requests(data)
    except relay.RelayError as exc:
        raise HTTPException(503, f"the entity requests of build {build_id} cannot be read: {exc}") from None


@router.get("/{project}/entities/requests", response_model=EntityRequests, responses=REFUSALS)
async def entity_requests(request: Request, project: ProjectName, user: CurrentUser) -> EntityRequests:
    """The items the latest succeeded build deferred to a run that has Claude Code."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        writer(access, "reading the entity requests of")
    build_id, rows = await _requests(request, access.project_id)
    return EntityRequests(build_id=build_id, requests=[EntityRequest(**row) for row in rows])


@router.post("/{project}/entities/answers", response_model=AnswersKept, responses=REFUSALS)
async def entity_answers(request: Request, project: ProjectName, body: EntityAnswers, user: CurrentUser):
    """Keep the answers of a run of this project to the latest entity requests and queue a build that reads them."""
    if user.acts_for_run is None:
        raise HTTPException(
            403,
            "entity answers come from a run of the project's agent pool, on its login: "
            "run `evo-agents hub kg entities` inside such a run",
        )
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        writer(access, "answering the entity requests of")
    try:
        answers = relay.clean_answers(body.answers)
    except relay.RelayError as exc:
        raise HTTPException(422, str(exc)) from None
    _, rows = await _requests(request, access.project_id)
    asked = {row["key"] for row in rows}
    kept = {key: answer for key, answer in answers.items() if key in asked}
    ignored = len(answers) - len(kept)
    if not kept:
        return AnswersKept(kept=0, ignored=ignored, queued=False)
    data, sha = relay.encode_answers(kept)
    if len(data) > relay.MAX_ANSWERS_BYTES:
        raise HTTPException(413, f"the answers are {len(data)} bytes, over {relay.MAX_ANSWERS_BYTES}: post fewer")
    store = blob_store(request)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "answers.json"
        path.write_bytes(data)
        try:
            await asyncio.to_thread(store.put_file, sha, path)
            async with request.app.state.engine.begin() as conn:
                await relay.hold(conn, store, access.project_id, sha, path, len(data), relay.ANSWERS_KIND, user.user_id)
                await project_archive.refuse_archived(conn, access.project_id, "its graph is not built again")
                _, queued = await queue(request, conn, access.project_id, project, user.user_id)
        except BlobStoreUnavailable:
            raise HTTPException(503, UNAVAILABLE) from None
    log.info(
        "kg entity answers kept",
        extra={"project": project, "run_id": user.acts_for_run, "kept": len(kept), "ignored": ignored},
    )
    return AnswersKept(kept=len(kept), ignored=ignored, queued=queued)
