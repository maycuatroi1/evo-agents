"""The outputs of runs on the hub (``evo_agents.hub.outputs``): what a machine sends, what readers list, deleting one,
and a project's switch of tool images. ``output_store`` records and ties them, ``output_content`` serves their bytes.

A machine sends the outputs of a run in two calls, its uploads (``.../output-uploads``, the presigned PUTs of the
bytes the project does not hold yet) and the outputs (``.../outputs``, which commit those uploads and record the
files), and reads how much the run may still take (``.../output-settings``):

- the worker that claimed the run, with its token: POST /v1/worker/runs/{id}/output-uploads, POST
  /v1/worker/runs/{id}/outputs and GET /v1/worker/runs/{id}/output-settings, while it holds the run and after, as its
  log is taken (``run_events``);
- the member whose local session it is, with their token: the same under /v1/projects/{p}/local-runs/{id}/.

Readers of a run (``views.readable_run``: a local run is its member's alone) list its outputs, oldest first, with GET
/v1/projects/{p}/runs/{id}/outputs (``source`` and ``after`` filter them), and read one with GET .../outputs/{oid}.
The run's dispatcher, or an admin of the project, deletes one with DELETE .../outputs/{oid}: it goes from the run, from
the questions and chat messages that carried it, and its bytes go when nothing refers to them any longer
(``output_prune``); the run's log gets a ``system`` event naming it (``outputs_deleted``), each chat whose message
carried it a ``message_changed`` event, and the audit trail ``run.output_delete``.

GET /v1/projects/{p}/output-settings answers any member the project's switch of the images taken out of tools'
results (``tool_images``, on unless an admin turned it off) and the limits; PUT, for an admin of the project, switches
it (audit ``settings.outputs``). Off, the hub refuses outputs of source tool (409), and the machines send none.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, chats, output_prune, outputs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, output_views
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import Uploads
from evo_agents.hub.server.chat_store import write_event as write_chat_event
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.local_runs import owned_local_run
from evo_agents.hub.server.output_store import (
    OutputBatch,
    OutputUploadRequest,
    Reach,
    RecordedOutputs,
    Target,
    issue,
    record,
    target_of,
    tool_images,
    usage,
)
from evo_agents.hub.server.output_views import RunOutput
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_events import claimed_run
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.runs.service.audits import audit_run, run_target
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)
NO_QUESTION_OUTPUTS = "run {run} is a question run: it sends no outputs, only the answer in its report of done"

OutputId = Annotated[int, Path(ge=1, le=MAX_ID)]
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
SEND_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)}
Sink = Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)]

router = APIRouter(prefix="/v1/projects", tags=["run outputs"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


# Models


class RunOutputs(BaseModel):
    run_id: int
    outputs: list[RunOutput] = Field(description="oldest first, those the filters keep")
    count: int = Field(description="every output the run holds")
    bytes: int = Field(description="their bytes")
    tool_images: bool = Field(description="the project takes images out of tools' results")


class OutputSettings(BaseModel):
    project: str
    tool_images: bool = Field(description="the project takes images out of tools' results (outputs of source tool)")
    max_file_bytes: int = Field(description="the most bytes one output has")
    max_outputs: int = Field(description="the most outputs a run holds")
    max_run_bytes: int = Field(description="the most bytes of outputs a run holds, a SHA-256 counted once")


class RunOutputSettings(OutputSettings):
    run_id: int
    outputs: int = Field(description="outputs the run holds")
    bytes: int = Field(description="their bytes")


class OutputSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_images: bool = Field(description="take images out of tools' results: false refuses outputs of source tool")


# Settings


def _settings(project: str, on: bool) -> OutputSettings:
    return OutputSettings(
        project=project,
        tool_images=on,
        max_file_bytes=outputs.MAX_FILE_BYTES,
        max_outputs=outputs.MAX_RUN_OUTPUTS,
        max_run_bytes=outputs.MAX_RUN_BYTES,
    )


async def _run_settings(conn: AsyncConnection, target: Target) -> RunOutputSettings:
    count, size = await usage(conn, target.run_id)
    settings = _settings(target.project, await tool_images(conn, target.project_id))
    return RunOutputSettings(**settings.model_dump(), run_id=target.run_id, outputs=count, bytes=size)


@router.get("/{project}/output-settings", response_model=OutputSettings, responses=READ_REFUSALS)
async def show_settings(request: Request, project: ProjectName, user: CurrentUser) -> OutputSettings:
    """The project's switch of tool images and the limits of outputs, for any member of it."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        plan_routes._reader(access)
        return _settings(access.name, await tool_images(conn, access.project_id))


@router.put("/{project}/output-settings", response_model=OutputSettings, responses=READ_REFUSALS)
async def put_settings(request: Request, project: ProjectName, body: OutputSettingsIn, user: CurrentUser):
    """Switch the images taken out of tools' results on or off for the project; an admin of it only."""
    p = tables.projects
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "admin"):
            raise HTTPException(403, f"switching the tool images of project {access.name} needs the admin role on it")
        before = await tool_images(conn, access.project_id)
        if before != body.tool_images:
            await conn.execute(update(p).values(output_tool_images=body.tool_images).where(p.c.id == access.project_id))
            target = f"{access.name}/outputs tool_images={'on' if body.tool_images else 'off'}"
            await audit_run(conn, user, access, audit.SETTINGS_OUTPUTS, target)
    log.info("output settings", extra={"project": project, "tool_images": body.tool_images, "login": user.login})
    return _settings(access.name, body.tool_images)


# Readers


@router.get("/{project}/runs/{run_id}/outputs", response_model=RunOutputs, responses=READ_REFUSALS)
async def list_outputs(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    source: Annotated[list[Literal[outputs.SOURCES]], Query(description="only these sources; repeat it")] = [],  # noqa: B006
    after: Annotated[int, Query(ge=0, le=MAX_ID, description="only the outputs after this id")] = 0,
    sink: Sink = None,
) -> RunOutputs:
    """The run's outputs, oldest first, each with the URL of its bytes."""
    async with request.app.state.engine.begin() as conn:
        access = await readable_run(conn, user, project, run_id, sink)
        found = await output_views.of_run(conn, access.name, run_id, list(dict.fromkeys(source)), after)
        count, size = await usage(conn, run_id)
        on = await tool_images(conn, access.project_id)
    return RunOutputs(run_id=run_id, outputs=found, count=count, bytes=size, tool_images=on)


async def _one(conn: AsyncConnection, project: str, run_id: int, output_id: int) -> RunOutput:
    o = tables.run_outputs
    row = (
        await conn.execute(select(*output_views.columns()).where(o.c.id == output_id, o.c.run_id == run_id))
    ).one_or_none()
    if row is None:
        raise HTTPException(404, f"run {run_id} has no output {output_id}: see GET .../runs/{run_id}/outputs")
    return output_views.view(row, output_views.run_url(project, run_id, output_id))


@router.get("/{project}/runs/{run_id}/outputs/{output_id}", response_model=RunOutput, responses=READ_REFUSALS)
async def show_output(
    request: Request, project: ProjectName, run_id: RunId, output_id: OutputId, user: CurrentUser, sink: Sink = None
) -> RunOutput:
    async with request.app.state.engine.begin() as conn:
        access = await readable_run(conn, user, project, run_id, sink)
        return await _one(conn, access.name, run_id, output_id)


async def _carried_by(conn: AsyncConnection, output_id: int) -> list:
    """(chat_message_id, chat_id) of each chat message that carries output ``output_id``."""
    k, cm = tables.run_output_links, tables.chat_messages
    query = select(k.c.chat_message_id, cm.c.chat_id).join_from(k, cm, cm.c.id == k.c.chat_message_id)
    return (await conn.execute(query.where(k.c.output_id == output_id))).all()


@router.delete("/{project}/runs/{run_id}/outputs/{output_id}", response_model=RunOutput, responses=READ_REFUSALS)
async def delete_output(
    request: Request, project: ProjectName, run_id: RunId, output_id: OutputId, user: CurrentUser, sink: Sink = None
) -> RunOutput:
    """Delete an output of the run, from the questions and chat messages that carry it too, and its bytes when
    nothing else refers to them; the run's dispatcher or an admin of the project only. The answer is what it was."""
    engine = request.app.state.engine
    r, o = tables.runs, tables.run_outputs
    async with engine.begin() as conn:
        access = await readable_run(conn, user, project, run_id, sink)
        locked = select(r.c.dispatched_by, r.c.plan_id, r.c.step_key).where(r.c.id == run_id).with_for_update()
        run = (await conn.execute(locked)).one()
        if run.dispatched_by != user.user_id and not has_role(access.role, "admin"):
            raise HTTPException(
                403, f"only who dispatched run {run_id}, or an admin of {access.name}, deletes its outputs"
            )
        await blob_gc.lock_exclusive(conn)  # after the run's row, as recording takes them
        gone = await _one(conn, access.name, run_id, output_id)
        messages = await _carried_by(conn, output_id)
        await output_prune.drop(conn, o.c.id == output_id)
        said = f"output #{output_id} deleted by {user.login}"
        await write_event(conn, run_id, {"text": said, "outputs_deleted": [output_id]})
        for message_id, chat_id in messages:
            await write_chat_event(conn, chat_id, chats.MESSAGE_CHANGED, {"message_id": message_id})
        target = f"{run_target(access.name, run.plan_id, run.step_key, run_id)} output:{output_id}"
        await audit_run(conn, user, access, audit.RUN_OUTPUT_DELETE, target)
    await output_prune.delete_objects(engine, request.app.state.blobs)
    log.info("run output deleted", extra={"run_id": run_id, "output_id": output_id, "login": user.login})
    return gone


# The worker of a run


def _worker_reach(user: Principal, run_id: int) -> Reach:
    async def reach(conn: AsyncConnection) -> Target:
        project = (await claimed_run(conn, user, run_id))[4]
        target = await target_of(conn, run_id, project)
        if target.kind == "question":  # nothing but its answer leaves a question run
            raise HTTPException(404, NO_QUESTION_OUTPUTS.format(run=run_id))
        return target

    return reach


@worker_router.get("/runs/{run_id}/output-settings", response_model=RunOutputSettings, responses=READ_REFUSALS)
async def worker_settings(request: Request, run_id: RunId, user: CurrentUser) -> RunOutputSettings:
    """The switch of tool images of the run's project, the limits, and what the run holds, for its worker."""
    async with request.app.state.engine.begin() as conn:
        return await _run_settings(conn, await _worker_reach(user, run_id)(conn))


@worker_router.post("/runs/{run_id}/output-uploads", response_model=Uploads, responses=SEND_REFUSALS)
async def worker_uploads(request: Request, run_id: RunId, body: OutputUploadRequest, user: CurrentUser):
    """Presigned PUTs for the bytes of outputs of a run this worker claimed, those its project does not hold yet."""
    return await issue(request, user, _worker_reach(user, run_id), body.items)


@worker_router.post("/runs/{run_id}/outputs", response_model=RecordedOutputs, responses=SEND_REFUSALS)
async def worker_outputs(request: Request, run_id: RunId, body: OutputBatch, user: CurrentUser):
    """Commit the uploads of outputs of a run this worker claimed and record the outputs."""
    return await record(request, user, _worker_reach(user, run_id), body)


# The member's local session


def _local_reach(user: Principal, project: str, run_id: int) -> Reach:
    async def reach(conn: AsyncConnection) -> Target:
        _, row = await owned_local_run(conn, user, project, run_id)
        return Target(run_id, row.name, row.project_id, row.kind, row.state)

    return reach


@router.get(
    "/{project}/local-runs/{run_id}/output-settings",
    response_model=RunOutputSettings,
    responses=READ_REFUSALS,
    tags=["local runs"],
)
async def local_settings(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser):
    """The switch of tool images of the project, the limits, and what the member's local run holds."""
    async with request.app.state.engine.begin() as conn:
        return await _run_settings(conn, await _local_reach(user, project, run_id)(conn))


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
    return await issue(request, user, _local_reach(user, project, run_id), body.items)


@router.post(
    "/{project}/local-runs/{run_id}/outputs",
    response_model=RecordedOutputs,
    responses=SEND_REFUSALS,
    tags=["local runs"],
)
async def local_outputs(request: Request, project: ProjectName, run_id: RunId, body: OutputBatch, user: CurrentUser):
    """Commit the uploads of outputs of the member's local run and record the outputs."""
    return await record(request, user, _local_reach(user, project, run_id), body)
