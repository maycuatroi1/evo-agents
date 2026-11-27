"""The routes of the files people attach to messages (see the package): each hands the request's parts to the
service, which checks the caller and answers."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.attachments.models import BODY, DROP_REFUSALS, REFUSALS, AttachmentId, FileName
from evo_agents.hub.server.attachments.service import content, uploads
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.chats.models.members import ChatId
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.outputs.models import CONTENT, Download, IfNoneMatch, RangeHeader, Sink
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.models.attachment_parts import Attachment
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
chat_router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


# Uploading


@router.post(
    "/{project}/runs/{run_id}/attachments",
    status_code=201,
    response_model=Attachment,
    responses=REFUSALS,
    openapi_extra=BODY,
)
async def upload_to_run(
    request: Request, project: ProjectName, run_id: RunId, user: CurrentUser, name: FileName
) -> Attachment:
    """Upload a file for a message to the run's agent: the run's owner, while the run takes messages."""
    return await uploads.to_run(request, user, project, run_id, name)


@chat_router.post(
    "/{chat_id}/attachments", status_code=201, response_model=Attachment, responses=REFUSALS, openapi_extra=BODY
)
async def upload_to_chat(request: Request, chat_id: ChatId, user: CurrentUser, name: FileName) -> Attachment:
    """Upload a file for a message of the chat: a member of it."""
    return await uploads.to_chat(request, user, chat_id, name)


# Taking an upload back


@router.delete("/{project}/runs/{run_id}/attachments/{attachment_id}", status_code=204, responses=DROP_REFUSALS)
async def delete_run_upload(
    request: Request, project: ProjectName, run_id: RunId, attachment_id: AttachmentId, user: CurrentUser
) -> Response:
    """Take back an upload of yours for the run that no message holds."""
    return await uploads.take_back_from_run(request, user, project, run_id, attachment_id)


@chat_router.delete("/{chat_id}/attachments/{attachment_id}", status_code=204, responses=DROP_REFUSALS)
async def delete_chat_upload(
    request: Request, chat_id: ChatId, attachment_id: AttachmentId, user: CurrentUser
) -> Response:
    """Take back an upload of yours for the chat that no message holds."""
    return await uploads.take_back_from_chat(request, user, chat_id, attachment_id)


# Reading the bytes


@router.get("/{project}/runs/{run_id}/attachments/{attachment_id}/content", response_class=Response, responses=CONTENT)
async def run_content(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    attachment_id: AttachmentId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
    sink: Sink = None,
) -> Response:
    """The bytes of a file of a message to the run, for its readers."""
    return await content.of_run(request, user, project, run_id, attachment_id, download, range_, if_none_match, sink)


@chat_router.get("/{chat_id}/attachments/{attachment_id}/content", response_class=Response, responses=CONTENT)
async def chat_content(
    request: Request,
    chat_id: ChatId,
    attachment_id: AttachmentId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """The bytes of a file a message of the chat carries, for a member who reads that message."""
    return await content.of_chat(request, user, chat_id, attachment_id, download, range_, if_none_match)


@worker_router.get("/runs/{run_id}/attachments/{attachment_id}/content", response_class=Response, responses=CONTENT)
async def worker_content(
    request: Request,
    run_id: RunId,
    attachment_id: AttachmentId,
    user: CurrentUser,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """The bytes of a file for the agent of a run the worker claimed: a file of a message its inbox took, or of a chat
    message the agent may read."""
    return await content.of_worker(request, user, run_id, attachment_id, range_, if_none_match)
