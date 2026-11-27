"""The routes of the versions of a document: each one hands the request's parts and the app's engine to the service."""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.chats.models.members import ChatId
from evo_agents.hub.server.doc_versions import service
from evo_agents.hub.server.doc_versions.models import REFUSALS, DocName, DocVersions
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.outputs.models import Sink
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

chat_router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})


@chat_router.get("/{chat_id}/doc-versions", response_model=DocVersions, responses=REFUSALS)
async def chat_doc_versions(request: Request, chat_id: ChatId, user: CurrentUser, name: DocName) -> DocVersions:
    """Every version of the document ``name`` in the chat, threads included, oldest first: the files people attached
    and the outputs agents sent under that name, of the messages the caller reads."""
    return await service.in_chat(request.app.state.engine, user, chat_id, name)


@router.get("/{project}/runs/{run_id}/doc-versions", response_model=DocVersions, responses=REFUSALS)
async def run_doc_versions(
    request: Request, project: ProjectName, run_id: RunId, user: CurrentUser, name: DocName, sink: Sink = None
) -> DocVersions:
    """Every version of the document ``name`` in the run, oldest first: the files of the messages sent to its agent
    and the outputs of the run under that name, for the run's readers."""
    return await service.in_run(request.app.state.engine, user, project, run_id, name, sink)
