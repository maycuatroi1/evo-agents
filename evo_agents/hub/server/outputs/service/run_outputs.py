"""The outputs of a run as its readers and its machines reach them: the project's switch of tool images, listing,
showing and deleting the outputs of a run, and the run a worker or a member's local session sends outputs to
(``Reach``, for ``store.issue`` and ``store.record``).

Readers of a run (``views.readable_run``: a local run is its member's alone) list its outputs, oldest first (``source``
and ``after`` filter them), and read one. The run's dispatcher, or an admin of the project, deletes one: it goes from
the run, from the questions and chat messages that carried it, and its bytes go when nothing refers to them any longer
(``output_prune``); the run's log gets a ``system`` event naming it (``outputs_deleted``), each chat whose message
carried it a ``message_changed`` event, and the audit trail ``run.output_delete``.

Any member reads the project's switch of the images taken out of tools' results (on unless an admin turned it off) and
the limits; an admin of the project switches it (audit ``settings.outputs``). Off, the hub refuses outputs of source
tool (409), and the machines send none.
"""

from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, chats, output_prune
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Forbidden, NotFound
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.repository.store import write_event as write_chat_event
from evo_agents.hub.server.local_runs import owned_local_run
from evo_agents.hub.server.outputs.models import (
    OutputSettings,
    OutputSettingsIn,
    RunOutput,
    RunOutputs,
    RunOutputSettings,
    Target,
    run_url,
    settings,
    view,
)
from evo_agents.hub.server.outputs.repository import store as store_repository
from evo_agents.hub.server.outputs.repository import views as views_repository
from evo_agents.hub.server.outputs.service import views
from evo_agents.hub.server.outputs.service.store import Reach, target_of, tool_images, usage
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_events import claimed_run
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.service.audits import audit_run, run_target
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)
NO_QUESTION_OUTPUTS = "run {run} is a question run: it sends no outputs, only the answer in its report of done"


# Settings


async def run_settings(conn: AsyncConnection, target: Target) -> RunOutputSettings:
    """The project's switch of tool images, the limits, and what run ``target`` holds."""
    count, size = await usage(conn, target.run_id)
    found = settings(target.project, await tool_images(conn, target.project_id))
    return RunOutputSettings(**found.model_dump(), run_id=target.run_id, outputs=count, bytes=size)


async def show_settings(request: Request, user: Principal, project: str) -> OutputSettings:
    """The project's switch of tool images and the limits of outputs, for any member of it."""
    async with UnitOfWork(request.app.state.engine) as conn:
        access = await project_access(conn, user, project)
        plan_service.reader(access)
        return settings(access.name, await tool_images(conn, access.project_id))


async def put_settings(request: Request, user: Principal, project: str, body: OutputSettingsIn) -> OutputSettings:
    """Switch the images taken out of tools' results on or off for the project; an admin of it only."""
    async with UnitOfWork(request.app.state.engine) as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "admin"):
            raise Forbidden(f"switching the tool images of project {access.name} needs the admin role on it")
        before = await tool_images(conn, access.project_id)
        if before != body.tool_images:
            await store_repository.set_tool_images(conn, access.project_id, body.tool_images)
            target = f"{access.name}/outputs tool_images={'on' if body.tool_images else 'off'}"
            await audit_run(conn, user, access, audit.SETTINGS_OUTPUTS, target)
    log.info("output settings", extra={"project": project, "tool_images": body.tool_images, "login": user.login})
    return settings(access.name, body.tool_images)


# Readers


async def of_run(
    request: Request, user: Principal, project: str, run_id: int, sources: list[str], after: int, sink: str | None
) -> RunOutputs:
    """The run's outputs, oldest first, each with the URL of its bytes."""
    async with UnitOfWork(request.app.state.engine) as conn:
        access = await readable_run(conn, user, project, run_id, sink)
        found = await views.of_run(conn, access.name, run_id, list(dict.fromkeys(sources)), after)
        count, size = await usage(conn, run_id)
        on = await tool_images(conn, access.project_id)
    return RunOutputs(run_id=run_id, outputs=found, count=count, bytes=size, tool_images=on)


async def _one(conn: AsyncConnection, project: str, run_id: int, output_id: int) -> RunOutput:
    row = await views_repository.one(conn, output_id, views_repository.of_the_run(run_id))
    if row is None:
        raise NotFound(f"run {run_id} has no output {output_id}: see GET .../runs/{run_id}/outputs")
    return view(row, run_url(project, run_id, output_id))


async def one(
    request: Request, user: Principal, project: str, run_id: int, output_id: int, sink: str | None
) -> RunOutput:
    """Output ``output_id`` of the run, for its readers."""
    async with UnitOfWork(request.app.state.engine) as conn:
        access = await readable_run(conn, user, project, run_id, sink)
        return await _one(conn, access.name, run_id, output_id)


async def delete(
    request: Request, user: Principal, project: str, run_id: int, output_id: int, sink: str | None
) -> RunOutput:
    """Delete an output of the run (see the module); the run's dispatcher or an admin of the project only. The
    answer is what it was."""
    engine = request.app.state.engine
    async with UnitOfWork(engine) as conn:
        access = await readable_run(conn, user, project, run_id, sink)
        run = await store_repository.locked_run(conn, run_id)
        if run.dispatched_by != user.user_id and not has_role(access.role, "admin"):
            raise Forbidden(f"only who dispatched run {run_id}, or an admin of {access.name}, deletes its outputs")
        await blob_gc.lock_exclusive(conn)  # after the run's row, as recording takes them
        gone = await _one(conn, access.name, run_id, output_id)
        messages = await views_repository.carried_by(conn, output_id)
        await store_repository.drop_output(conn, output_id)
        said = f"output #{output_id} deleted by {user.login}"
        await write_event(conn, run_id, {"text": said, "outputs_deleted": [output_id]})
        for message_id, chat_id in messages:
            await write_chat_event(conn, chat_id, chats.MESSAGE_CHANGED, {"message_id": message_id})
        target = f"{run_target(access.name, run.plan_id, run.step_key, run_id)} output:{output_id}"
        await audit_run(conn, user, access, audit.RUN_OUTPUT_DELETE, target)
    await output_prune.delete_objects(engine, request.app.state.blobs)
    log.info("run output deleted", extra={"run_id": run_id, "output_id": output_id, "login": user.login})
    return gone


# The run outputs go to


def worker_reach(user: Principal, run_id: int) -> Reach:
    """The run ``run_id`` the worker ``user`` claimed, as it sends outputs; a question run sends none (404)."""

    async def reach(conn: AsyncConnection) -> Target:
        project = (await claimed_run(conn, user, run_id))[4]
        target = await target_of(conn, run_id, project)
        if target.kind == "question":  # nothing but its answer leaves a question run
            raise NotFound(NO_QUESTION_OUTPUTS.format(run=run_id))
        return target

    return reach


def local_reach(user: Principal, project: str, run_id: int) -> Reach:
    """The local run ``run_id`` of member ``user`` in ``project``, as their session sends outputs."""

    async def reach(conn: AsyncConnection) -> Target:
        _, row = await owned_local_run(conn, user, project, run_id)
        return Target(run_id, row.name, row.project_id, row.kind, row.state)

    return reach


async def reached_settings(request: Request, reach: Reach) -> RunOutputSettings:
    """``run_settings`` of the run ``reach`` reaches."""
    async with UnitOfWork(request.app.state.engine) as conn:
        return await run_settings(conn, await reach(conn))
