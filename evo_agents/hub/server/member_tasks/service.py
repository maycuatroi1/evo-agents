"""What a member's task run does: its dispatch under the caller (``dispatch``), with the runtime of a profile that
runs on the hub, and the task as its latest attempt has it to whoever reads its run (``shown``). Every query is the
repository's (``TaskStore``)."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs, task_runs
from evo_agents.hub.credentials import dispatch_credential
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.gateway.models.accounts import Asker
from evo_agents.hub.server.gateway.service import accounts as gateway_accounts
from evo_agents.hub.server.member_tasks.models import NO_TASK, MemberTask, MemberTaskIn
from evo_agents.hub.server.member_tasks.repository import STORE
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import notify_queued
from evo_agents.hub.server.runs.service.audits import audit_run
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher, check_seat, insert_run, warned
from evo_agents.hub.server.runs.service.profiles import Asked, Chosen, choose
from evo_agents.hub.server.runs.service.views import readable_run, run_view
from evo_agents.hub.server.security import Principal

log = logging.getLogger("evo_agents.hub.server.member_tasks")  # the name the module had before it was a package


def _title(body: MemberTaskIn) -> str:
    line = (body.title or body.task).strip().splitlines()[0] if (body.title or body.task).strip() else "task"
    return line[: task_runs.MAX_TITLE_CHARS]


def _handle(profile: str | None) -> str | None:
    return None if profile is None else profile.removeprefix("@")


async def _on_hub(conn: AsyncConnection, user: Principal, access: ProjectAccess, chosen: Chosen) -> dict:
    """The columns of a task run of ``chosen``'s profile, which runs on the hub: runtime hub, which no worker claims,
    and the prefix its model goes through the gateway with, as ``chats.service.runs`` gives a chat turn of the agent."""
    profile = chosen.profile
    owned = not profile.is_default
    asker = Asker(0, user.user_id, user.user_id, access.project_id, profile.id, profile.project_id, agent_owned=owned)
    prefix = await gateway_accounts.prefix_for(conn, asker, runs.HUB_RUNTIME, chosen.model)
    return {"requested_runtime": runs.HUB_RUNTIME, "runtime": runs.HUB_RUNTIME, "gateway_provider": prefix}


async def _columns(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, body: MemberTaskIn, chosen: Chosen
) -> dict:
    """The columns of the run of the task ``body`` hands, under ``user`` and ``chosen``'s profile."""
    on_hub = chosen.profile.runs_on == runs.HUB_RUNTIME
    return {
        "kind": "task",
        "project_id": access.project_id,
        "title": _title(body),
        "dispatched_by": user.user_id,
        "dispatched_via": dispatch_credential(user.kind),
        "requested_runtime": "any",
        "runtime": "any",
        "model": chosen.model,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": task_runs.run_timeout(body.minutes),
        "request": body.task,
        **chosen.columns(),
        **(await _on_hub(conn, user, access, chosen) if on_hub else {}),
    }


async def dispatch(engine: AsyncEngine, user: Principal, project: str, body: MemberTaskIn) -> MemberTask:
    """Queue the task ``body`` hands as a run of kind task in no repo under ``user``, a writer of ``project``; the task
    as it then is."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        await check_seat(conn, user)
        chosen = await choose(conn, user, access, _handle(body.profile), Asked("task", "any", None))
        run_id = await insert_run(conn, await _columns(conn, user, access, body, chosen))
        await STORE.add(conn, run_id, access.project_id, body.input, body.assets)
        await notify_queued(conn, run_id)
        target = f"{project}/task run:{run_id} assets={len(body.assets)}{chosen.audit_text()}"
        await audit_run(conn, user, access, audit.RUN_DISPATCH_TASK, target)
        if chosen.profile.runs_on != runs.HUB_RUNTIME:  # no worker takes a run of the hub runtime, so none is missing
            await warned(conn, await run_view(conn, run_id))
        shown = await STORE.shown(conn, project, run_id)
    log.info("member task run dispatched", extra={"project": project, "run_id": run_id, "assets": len(body.assets)})
    assert shown is not None
    return shown


async def shown(engine: AsyncEngine, user: Principal, project: str, task: int) -> MemberTask:
    """Task ``task`` of ``project`` as its latest attempt has it, to ``user`` when they read its run."""
    async with UnitOfWork(engine) as conn:
        found = await STORE.shown(conn, project, task)
        if found is None:
            await project_access(conn, user, project)  # 404 for a project the caller has no grant on
            raise HTTPException(404, NO_TASK.format(project=project, task=task))
        await readable_run(conn, user, project, found.run_id, None)
    return found
