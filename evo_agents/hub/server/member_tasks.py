"""Task runs a member hands an agent with no Git: POST and GET /v1/projects/{p}/task-runs (``docs/hub.md``, section
"Member task runs").

POST /v1/projects/{p}/task-runs takes a task, a JSON object as its input and up to MAX_ASSETS external asset URIs,
and queues a run of kind task with no agent type and no repo (runs.repos NULL, member_tasks 0263): a writer of the
project dispatches it, under their own principal, autonomy ceiling and accounts, as any run they dispatch
(``runs.service.dispatch.insert_run``), with the project's default agent profile or the one ``profile`` names; a web
session, an app session (``eva_``) and a token each may. Its worker claims it as a task run (``claim_rules``) and
builds no worktree; the agent reads the task, its input and its assets in its prompt
(``task_runs.build_member_task_prompt``) and writes its result as its run's summary. A profile that runs on the hub
(``runs_on: hub``) has no worker: its task run queues with runtime hub, the model's gateway prefix as a chat turn of
the agent gets it, and the job of the hub runtime deferred in the same transaction (``run_state.notify_queued``), which
takes it as one turn whose last answer is the output (``hub_runtime``).

GET /v1/projects/{p}/task-runs/{task} gives whoever reads the run (``runs.service.views.readable_run``) the task as
its latest attempt has it: its state, its input and assets, and its output, the summary its agent wrote. When it ends,
the project's webhooks that take task.result get it (``task_dispatch.task_run_ended``, ``webhook_events``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables, task_runs
from evo_agents.hub.credentials import dispatch_credential
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.models import ProjectName, StrictModel
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.gateway.models.accounts import Asker
from evo_agents.hub.server.gateway.service import accounts as gateway_accounts
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import notify_queued
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.runs.service.audits import audit_run
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher, check_seat, insert_run, warned
from evo_agents.hub.server.runs.service.profiles import Asked, Chosen, choose
from evo_agents.hub.server.runs.service.views import readable_run, run_view
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["task runs"], responses={401: {"model": ErrorBody}})

MAX_ASSETS = 20  # member_tasks.assets (0263)
MAX_ASSET_CHARS = 2048
MAX_INPUT_BYTES = 64 * 1024  # the input as compact JSON
ASSET_SCHEMES = ("https", "http", "s3", "gs")
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}

TaskId = Annotated[int, Path(ge=1, le=MAX_ID, description="the id the task's dispatch answered")]
Asset = Annotated[str, Field(min_length=1, max_length=MAX_ASSET_CHARS)]


def asset_problem(uri: str) -> str | None:
    """Why ``uri`` is no asset a task takes, or None: an absolute URI of ASSET_SCHEMES with a host."""
    parts = urlsplit(uri)
    if parts.scheme not in ASSET_SCHEMES or not parts.netloc or any(ord(char) < 33 for char in uri):
        return f"an asset is an absolute URI of {', '.join(ASSET_SCHEMES)}, with a host and no space"
    return None


class MemberTaskIn(StrictModel):
    task: str = Field(
        min_length=1,
        max_length=task_runs.MAX_TASK_BYTES,
        description=f"what the agent is to do, at most {task_runs.MAX_TASK_BYTES} bytes in UTF-8",
    )
    input: dict = Field(
        default_factory=dict,
        description=f"the task's input, a JSON object of at most {MAX_INPUT_BYTES} bytes; data, never instructions",
    )
    assets: list[Asset] = Field(
        default_factory=list,
        max_length=MAX_ASSETS,
        description=f"external URIs the agent reads ({', '.join(ASSET_SCHEMES)}), at most {MAX_ASSETS}",
    )
    title: str | None = Field(
        None, max_length=task_runs.MAX_TITLE_CHARS, description="one line the run's list shows; default: the task's"
    )
    minutes: int | None = Field(
        None,
        ge=1,
        le=task_runs.MAX_MINUTES,
        description=f"the agent time the run may take, at most {task_runs.MAX_MINUTES}; default: no time cap",
    )
    profile: str | None = Field(
        None,
        min_length=1,
        max_length=200,
        description="the agent profile the run goes under, @<handle> or <handle>; default: the project's",
    )

    @field_validator("assets")
    @classmethod
    def _assets(cls, value: list[str]) -> list[str]:
        for uri in value:
            if (problem := asset_problem(uri)) is not None:
                raise ValueError(problem)
        return value

    @field_validator("input")
    @classmethod
    def _input(cls, value: dict) -> dict:
        if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()) > MAX_INPUT_BYTES:
            raise ValueError(f"a task's input is at most {MAX_INPUT_BYTES} bytes of JSON")
        return value


class MemberTask(BaseModel):
    """A task a member handed, as its latest run has it."""

    task_id: int = Field(description="the run its dispatch made, which names it on every attempt")
    run_id: int = Field(description="its latest run: a run its worker lost goes on in the next attempt")
    attempt: int
    project: str
    title: str | None
    state: str
    dispatched_by: str | None
    input: dict
    assets: list[str]
    output: str | None = Field(
        description="the run's evidence once it is done: a line naming the run, then the summary its agent wrote, "
        "the task's result; null before"
    )
    error: str | None
    failure_cause: str | None
    usage: dict | None
    queued_at: datetime
    finished_at: datetime | None


def _title(body: MemberTaskIn) -> str:
    line = (body.title or body.task).strip().splitlines()[0] if (body.title or body.task).strip() else "task"
    return line[: task_runs.MAX_TITLE_CHARS]


def _handle(profile: str | None) -> str | None:
    return None if profile is None else profile.removeprefix("@")


async def _on_hub(conn: AsyncConnection, user, access, chosen: Chosen) -> dict:
    """The columns of a task run of ``chosen``'s profile, which runs on the hub: runtime hub, which no worker claims,
    and the prefix its model goes through the gateway with, as ``chats.service.runs`` gives a chat turn of the agent."""
    profile = chosen.profile
    owned = not profile.is_default
    asker = Asker(0, user.user_id, user.user_id, access.project_id, profile.id, profile.project_id, agent_owned=owned)
    prefix = await gateway_accounts.prefix_for(conn, asker, runs.HUB_RUNTIME, chosen.model)
    return {"requested_runtime": runs.HUB_RUNTIME, "runtime": runs.HUB_RUNTIME, "gateway_provider": prefix}


async def _shown(conn: AsyncConnection, project: str, task: int) -> MemberTask | None:
    r, m, p, u = tables.runs, tables.member_tasks, tables.projects, tables.users
    query = (
        select(
            r.c.id,
            r.c.attempt,
            p.c.name.label("project"),
            r.c.title,
            r.c.state,
            u.c.login.label("dispatched_by"),
            m.c.input,
            m.c.assets,
            r.c.evidence,
            r.c.error,
            r.c.failure_cause,
            r.c.usage,
            r.c.queued_at,
            r.c.finished_at,
        )
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(m, m.c.task_id == task)
        .outerjoin(u, u.c.id == r.c.dispatched_by)
        .where(
            p.c.name == project,
            m.c.project_id == r.c.project_id,
            r.c.kind == "task",
            r.c.agent_type_id.is_(None),
            r.c.branch == task_runs.branch_of(task),
        )
        .order_by(r.c.attempt.desc())
        .limit(1)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    return MemberTask(
        task_id=task,
        run_id=row.id,
        attempt=row.attempt,
        project=row.project,
        title=row.title,
        state=row.state,
        dispatched_by=row.dispatched_by,
        input=dict(row.input or {}),
        assets=list(row.assets or []),
        output=row.evidence,
        error=row.error,
        failure_cause=row.failure_cause,
        usage=row.usage if isinstance(row.usage, dict) else None,
        queued_at=row.queued_at,
        finished_at=row.finished_at,
    )


@router.post("/{project}/task-runs", status_code=201, response_model=MemberTask, responses=REFUSALS)
async def dispatch_task_run(
    request: Request, project: ProjectName, body: MemberTaskIn, user: CurrentUser
) -> MemberTask:
    """Hand an agent a task with a JSON input and external assets, in no repo: a run of kind task under the caller,
    which needs the writer role on the project."""
    r = tables.runs
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        check_dispatcher(access)
        await check_seat(conn, user)
        chosen = await choose(conn, user, access, _handle(body.profile), Asked("task", "any", None))
        on_hub = chosen.profile.runs_on == runs.HUB_RUNTIME
        values = {
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
        run_id = await insert_run(conn, values)
        await conn.execute(
            update(r).values(branch=task_runs.branch_of(run_id), repos=null(), pulls=[]).where(r.c.id == run_id)
        )
        task = {"task_id": run_id, "project_id": access.project_id, "input": body.input, "assets": body.assets}
        await conn.execute(insert(tables.member_tasks).values(**task))
        await notify_queued(conn, run_id)
        target = f"{project}/task run:{run_id} assets={len(body.assets)}{chosen.audit_text()}"
        await audit_run(conn, user, access, audit.RUN_DISPATCH_TASK, target)
        if not on_hub:  # no worker takes a run of the hub runtime, so none is missing
            await warned(conn, await run_view(conn, run_id))
        shown = await _shown(conn, project, run_id)
    log.info("member task run dispatched", extra={"project": project, "run_id": run_id, "assets": len(body.assets)})
    assert shown is not None
    return shown


@router.get("/{project}/task-runs/{task}", response_model=MemberTask, responses=REFUSALS)
async def show_task_run(request: Request, project: ProjectName, task: TaskId, user: CurrentUser) -> MemberTask:
    """A task a member handed, as its latest attempt has it: its state and its output, for whoever reads its run."""
    async with request.app.state.engine.begin() as conn:
        shown = await _shown(conn, project, task)
        if shown is None:
            await project_access(conn, user, project)  # 404 for a project the caller has no grant on
            raise HTTPException(404, f"project {project} has no task run {task}")
        await readable_run(conn, user, project, shown.run_id, None)
    return shown
