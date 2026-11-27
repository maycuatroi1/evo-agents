"""The routes of the outputs of runs on the hub (``evo_agents.hub.outputs``): what a machine sends, what readers list,
deleting one, a project's switch of tool images, their bytes, and the notification settings of a member. Each handler
passes the request's parts to the service.

A machine sends the outputs of a run in two calls, its uploads (``.../output-uploads``, the presigned PUTs of the
bytes the project does not hold yet) and the outputs (``.../outputs``, which commit those uploads and record the
files), and reads how much the run may still take (``.../output-settings``):

- the worker that claimed the run, with its token: POST /v1/worker/runs/{id}/output-uploads, POST
  /v1/worker/runs/{id}/outputs and GET /v1/worker/runs/{id}/output-settings, while it holds the run and after, as its
  log is taken (``run_events``);
- the member whose local session it is, with their token: the same under /v1/projects/{p}/local-runs/{id}/.

Readers of a run list its outputs with GET /v1/projects/{p}/runs/{id}/outputs and read one with GET .../outputs/{oid};
its dispatcher, or an admin of the project, deletes one with DELETE .../outputs/{oid} (``service.run_outputs``).
GET /v1/projects/{p}/output-settings answers any member the project's switch of tool images and the limits; PUT, for
an admin of the project, switches it.

The bytes (``service.content``, ``service.serving``):

- GET /v1/projects/{p}/runs/{id}/outputs/{oid}/content, to the run's readers;
- GET /v1/projects/{p}/decisions/{id}/outputs/{oid}/content, to the decision's readers, for an output the decision
  carries;
- GET /v1/chats/{id}/outputs/{oid}/content, to a member of the chat who reads a message of it that carries the output;
- GET /v1/projects/{p}/runs/{id}/outputs/{oid}/link, to the run's readers: a presigned GET of the bytes.

GET and PUT /v1/me/notification-settings read and set the caller's notification settings (``service.notices``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response

from evo_agents.hub import outputs
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.blobs import Uploads
from evo_agents.hub.server.chats.models.members import ChatId
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.outputs.models import (
    CONTENT,
    LINK_REFUSALS,
    READ_REFUSALS,
    SEND_REFUSALS,
    DecisionId,
    Download,
    IfNoneMatch,
    OutputBatch,
    OutputId,
    OutputLink,
    OutputSettings,
    OutputSettingsIn,
    OutputUploadRequest,
    RangeHeader,
    RecordedOutputs,
    RunOutput,
    RunOutputs,
    RunOutputSettings,
    Sink,
)
from evo_agents.hub.server.outputs.models.notices import NotificationSettings, NotificationSettingsIn
from evo_agents.hub.server.outputs.service import content, notices, run_outputs, store
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.security import CurrentUser

notice_router = APIRouter(prefix="/v1/me", tags=["notifications"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["run outputs"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
content_router = APIRouter(prefix="/v1/projects", tags=["run outputs"], responses={401: {"model": ErrorBody}})
chat_router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


# The notification settings of a member


@notice_router.get("/notification-settings", response_model=NotificationSettings)
async def show_notification_settings(request: Request, user: CurrentUser) -> NotificationSettings:
    """The kinds of notice the caller may turn off and whether each is on, the tier of each kind of notification, their
    quiet hours, time zone and time of the summary push, and whether push hides details."""
    return await notices.settings(request, user)


@notice_router.put("/notification-settings", response_model=NotificationSettings, responses={422: {"model": ErrorBody}})
async def set_notification_settings(
    request: Request, body: NotificationSettingsIn, user: CurrentUser
) -> NotificationSettings:
    """Turn kinds of notice on or off for the caller, choose the tier of kinds of notification, their quiet hours,
    time zone and time of the summary push, and whether push hides details; the Inbox gets every notification still."""
    return await notices.set_settings(request, user, body)


# Settings


@router.get("/{project}/output-settings", response_model=OutputSettings, responses=READ_REFUSALS)
async def show_settings(request: Request, project: ProjectName, user: CurrentUser) -> OutputSettings:
    """The project's switch of tool images and the limits of outputs, for any member of it."""
    return await run_outputs.show_settings(request, user, project)


@router.put("/{project}/output-settings", response_model=OutputSettings, responses=READ_REFUSALS)
async def put_settings(request: Request, project: ProjectName, body: OutputSettingsIn, user: CurrentUser):
    """Switch the images taken out of tools' results on or off for the project; an admin of it only."""
    return await run_outputs.put_settings(request, user, project, body)


# Readers


@router.get("/{project}/runs/{run_id}/outputs", response_model=RunOutputs, responses=READ_REFUSALS)
async def list_outputs(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    source: Annotated[list[Literal[outputs.SOURCES]], Query(description="only these sources; repeat it")] = [],  # noqa: B006 # pyright: ignore[reportInvalidTypeForm]
    after: Annotated[int, Query(ge=0, le=MAX_ID, description="only the outputs after this id")] = 0,
    sink: Sink = None,
) -> RunOutputs:
    """The run's outputs, oldest first, each with the URL of its bytes."""
    return await run_outputs.of_run(request, user, project, run_id, source, after, sink)


@router.get("/{project}/runs/{run_id}/outputs/{output_id}", response_model=RunOutput, responses=READ_REFUSALS)
async def show_output(
    request: Request, project: ProjectName, run_id: RunId, output_id: OutputId, user: CurrentUser, sink: Sink = None
) -> RunOutput:
    return await run_outputs.one(request, user, project, run_id, output_id, sink)


@router.delete("/{project}/runs/{run_id}/outputs/{output_id}", response_model=RunOutput, responses=READ_REFUSALS)
async def delete_output(
    request: Request, project: ProjectName, run_id: RunId, output_id: OutputId, user: CurrentUser, sink: Sink = None
) -> RunOutput:
    """Delete an output of the run, from the questions and chat messages that carry it too, and its bytes when
    nothing else refers to them; the run's dispatcher or an admin of the project only. The answer is what it was."""
    return await run_outputs.delete(request, user, project, run_id, output_id, sink)


# The worker of a run


@worker_router.get("/runs/{run_id}/output-settings", response_model=RunOutputSettings, responses=READ_REFUSALS)
async def worker_settings(request: Request, run_id: RunId, user: CurrentUser) -> RunOutputSettings:
    """The switch of tool images of the run's project, the limits, and what the run holds, for its worker."""
    return await run_outputs.reached_settings(request, run_outputs.worker_reach(user, run_id))


@worker_router.post("/runs/{run_id}/output-uploads", response_model=Uploads, responses=SEND_REFUSALS)
async def worker_uploads(request: Request, run_id: RunId, body: OutputUploadRequest, user: CurrentUser):
    """Presigned PUTs for the bytes of outputs of a run this worker claimed, those its project does not hold yet."""
    return await store.issue(request, user, run_outputs.worker_reach(user, run_id), body.items)


@worker_router.post("/runs/{run_id}/outputs", response_model=RecordedOutputs, responses=SEND_REFUSALS)
async def worker_outputs(request: Request, run_id: RunId, body: OutputBatch, user: CurrentUser):
    """Commit the uploads of outputs of a run this worker claimed and record the outputs."""
    return await store.record(request, user, run_outputs.worker_reach(user, run_id), body)


# The member's local session


@router.get(
    "/{project}/local-runs/{run_id}/output-settings",
    response_model=RunOutputSettings,
    responses=READ_REFUSALS,
    tags=["local runs"],
)
async def local_settings(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser):
    """The switch of tool images of the project, the limits, and what the member's local run holds."""
    return await run_outputs.reached_settings(request, run_outputs.local_reach(user, project, run_id))


@router.post(
    "/{project}/local-runs/{run_id}/output-uploads",
    response_model=Uploads,
    responses=SEND_REFUSALS,
    tags=["local runs"],
)
async def local_uploads(
    request: Request, project: ProjectName, run_id: RunId, body: OutputUploadRequest, user: CurrentUser
):
    """Presigned PUTs for the bytes of outputs of the member's local run, those the project does not hold yet."""
    return await store.issue(request, user, run_outputs.local_reach(user, project, run_id), body.items)


@router.post(
    "/{project}/local-runs/{run_id}/outputs",
    response_model=RecordedOutputs,
    responses=SEND_REFUSALS,
    tags=["local runs"],
)
async def local_outputs(request: Request, project: ProjectName, run_id: RunId, body: OutputBatch, user: CurrentUser):
    """Commit the uploads of outputs of the member's local run and record the outputs."""
    return await store.record(request, user, run_outputs.local_reach(user, project, run_id), body)


# The bytes


@content_router.get("/{project}/runs/{run_id}/outputs/{output_id}/content", response_class=Response, responses=CONTENT)
async def run_content(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    output_id: OutputId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
    sink: Sink = None,
) -> Response:
    """The bytes of an output of the run, for its readers."""
    return await content.of_run(request, user, project, run_id, output_id, download, range_, if_none_match, sink)


@content_router.get(
    "/{project}/runs/{run_id}/outputs/{output_id}/link", response_model=OutputLink, responses=LINK_REFUSALS
)
async def run_link(
    request: Request, project: ProjectName, run_id: RunId, output_id: OutputId, user: CurrentUser, sink: Sink = None
) -> OutputLink:
    """A presigned GET of the bytes of an output of the run, as an attachment, for its readers."""
    return await content.link(request, user, project, run_id, output_id, sink)


@content_router.get(
    "/{project}/decisions/{decision_id}/outputs/{output_id}/content", response_class=Response, responses=CONTENT
)
async def decision_content(
    request: Request,
    project: ProjectName,
    decision_id: DecisionId,
    output_id: OutputId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
    sink: Sink = None,
) -> Response:
    """The bytes of an output the decision carries, for the decision's readers."""
    return await content.of_decision(
        request, user, project, decision_id, output_id, download, range_, if_none_match, sink
    )


@chat_router.get("/{chat_id}/outputs/{output_id}/content", response_class=Response, responses=CONTENT)
async def chat_content(
    request: Request,
    chat_id: ChatId,
    output_id: OutputId,
    user: CurrentUser,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """The bytes of an output a message of the chat carries, for a member who reads that message; an interactive answer
    loads libraries from the CDNs of INTERACTIVE_POLICY unless the chat is restricted."""
    return await content.of_chat(request, user, chat_id, output_id, download, range_, if_none_match)
