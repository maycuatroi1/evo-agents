"""The Curator's replays on the hub: the night shift's replay runs, what their workers report, and reading them.
``evo_agents.hub.replay`` holds the model and ``evo_agents.hub.replay_suite`` the job that builds each project's suite
of replay tasks.

The night shift (``curator._fire``) queues, after a judge run that waits, the replay run of the oldest change of the
Curator that waits for one (``queue_replay``): a change built (its Builder ran, its branch is on origin) and not merged
or closed (``replay.REPLAY_STATES``), whose proposal is of kind skill or names a skill, an AGENTS.md or a CLAUDE.md
(``replay.harness_path``), with no run of its plan active. It takes the tasks of the holdout split of the project's
suite in the order they came in, those not found unusable whose repos the worker on duty has, up to the charter's
``replay.tasks`` per change, one replay run per task: a run of kind replay on the change's plan, pinned to the worker on
duty, as the schedule's owner, with the runtime and model of the charter's Builder, and caps that keep the night's
replay runs within ``replay.budget_usd`` (a charter without it, or with ``replay.tasks`` 0, replays nothing). A replay
run that ends without both arms is replayed again, REPLAY_ATTEMPTS runs in all, then the replay fails with why
(``replay_ended``); one whose worker refused it (no base commit for the task, no harness file in the change) fails at
once, and a task whose commits are nowhere on origin is unusable from then on.

The claim of a replay run tells its worker the replay, the task and the change's branch and head, and hands it the
run's own key (``replay_claim``), which the hub keeps as a SHA-256: the worker posts each arm's result with POST
/v1/worker/runs/{id}/replay with that key alone (``replay.REPLAY_KEY_HEADER``), so code on the worker's machine that
reads the worker token writes no result. The first arm that names the task's base commit sets it on the task, once.
The Judge reads none of this: its prompt and its tools hold no replay.

GET /v1/projects/{p}/curator/replay/tasks (reader) lists the project's suite, of ``split`` when given, the tasks of
plans the caller may read through the project's hub sink; GET .../curator/changes/{id}/replay (reader) a change's
replays with what each arm found and ``replay.compare`` of them. The MCP tool curator_replay answers both
(``mcp_answer``), the suite's select split alone to the agent of a run.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, insert, null, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, replay, runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.night_cost import kind_cost
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.run_state import RunStep, notify_queued, write_event
from evo_agents.hub.server.runs.models import MAX_ID, NOT_HELD, OBJECT_NAME, RunId
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run, unfit
from evo_agents.hub.server.runs.service.views import lock_plan, plan_activity
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

REPLAY = "curator.replay"  # the night shift queued a replay run: "<project>/<plan> run:<id> change:<id> task:<id>"
MAX_TASKS_LISTED = 200
HARNESS_KINDS = ("skill",)  # the kinds of proposal replayed whatever the paths they name

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}

ReplayKey = Annotated[
    str | None,
    Header(
        alias=replay.REPLAY_KEY_HEADER,
        max_length=200,
        description="the replay run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]
Split = Literal[replay.SPLITS]


# Models


class VerifyRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exit_code: int
    duration_ms: int | None = Field(None, ge=0)


class ArmResult(BaseModel):
    """What one arm of a replay found: whether the step's verify passed on the agent's work, and what the agent took."""

    model_config = ConfigDict(extra="forbid")

    passed: bool = Field(description="every verify command exited 0")
    harness_sha: str = Field(pattern=OBJECT_NAME, description="the commit of the change's repo the arm's harness is at")
    harness_paths: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=replay.MAX_PATHS, description="the files of the change the arm ran with"
    )
    verify: list[VerifyRun] = Field(default_factory=list, max_length=10)
    cost_usd: float | None = Field(None, ge=0, description="the agent's session; null when its runtime names none")
    turns: int | None = Field(None, ge=0)
    tool_calls: int = Field(0, ge=0)
    tool_errors: int = Field(0, ge=0, description="the tool calls that failed")
    peak_context_tokens: int | None = Field(None, ge=0)
    duration_ms: int = Field(ge=0, description="the agent's time")
    agent_error: str | None = Field(None, max_length=replay.MAX_REASON_CHARS, description="why its turn did not end")
    session_id: str | None = Field(None, max_length=200)


class ReplayReport(BaseModel):
    """One arm's result with the task's base commit; or why the worker cannot replay the task at all."""

    model_config = ConfigDict(extra="forbid")

    arm: Literal[replay.ARMS] | None = None
    base_sha: str | None = Field(None, pattern=OBJECT_NAME, description="the task's base commit the arm started at")
    result: ArmResult | None = None
    refused: str | None = Field(None, min_length=1, max_length=replay.MAX_REASON_CHARS)
    task_unusable: bool = Field(False, description="with refused: no replay can run the task, whatever the change")

    @model_validator(mode="after")
    def _one(self):
        if self.refused is None and (self.arm is None or self.result is None or self.base_sha is None):
            raise ValueError("a replay report is an arm with its result and base_sha, or refused with why")
        if self.refused is not None and (self.arm is not None or self.result is not None):
            raise ValueError("a refused replay reports no arm")
        return self


class ReplayTask(BaseModel):
    id: int
    plan_id: str
    step_key: str
    repo: str
    title: str | None
    what: str
    verify: str
    commits: list[str]
    source: Literal[replay.SOURCES]
    split: Split
    base_sha: str | None = Field(description="the first parent of the oldest of its commits, once a replay read it")
    unusable: str | None = Field(description="why no replay can run it")
    created_at: datetime


class ReplayTaskList(BaseModel):
    project: str
    split: Split | None
    tasks: list[ReplayTask] = Field(description="in the order they came into the suite")
    total: int
    counts: dict[str, int] = Field(description="the tasks of each split the caller may read")


class Replay(BaseModel):
    id: int
    change_id: int
    task_id: int
    plan_id: str
    step_key: str
    repo: str
    title: str | None
    split: Split
    run_id: int | None
    state: Literal[replay.STATES]
    attempts: int
    base_sha: str | None
    a: dict | None = Field(description="arm a, the harness of the default branch: ArmResult")
    b: dict | None = Field(description="arm b, the harness of the change's branch: ArmResult")
    reason: str | None
    created_at: datetime
    updated_at: datetime


class ChangeReplay(BaseModel):
    project: str
    change_id: int
    tasks: int = Field(description="the tasks each change is replayed on, as the charter says now")
    replays: list[Replay]
    arms: dict = Field(description="replay.compare: per arm the replays, passed, cost, turns, tool errors, context")


# Queueing


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _harness_change(row) -> bool:
    paths = [item.get("path") for item in row.paths or [] if isinstance(item, dict) and item.get("repo") == row.repo]
    return row.kind in HARNESS_KINDS or bool(replay.harness_paths(paths))


async def _replayable(conn: AsyncConnection, project_id: int) -> list:
    """The project's changes a replay may measure, oldest first (see the module's docstring)."""
    c, pr = tables.curator_changes, tables.proposals
    query = (
        select(c, pr.c.paths, pr.c.kind.label("kind"), pr.c.title.label("proposal_title"))
        .join_from(c, pr, pr.c.id == c.c.proposal_id)
        .where(
            c.c.project_id == project_id,
            c.c.state.in_(replay.REPLAY_STATES),
            c.c.builder_run_id.is_not(None),
            c.c.branch.is_not(None),
            c.c.plan_id.is_not(None),
        )
        .order_by(c.c.id)
    )
    return [row for row in (await conn.execute(query)).all() if _harness_change(row)]


async def _next_task(conn: AsyncConnection, due, gate, change, wanted: int, runtime: str):
    """(the task, the replay to run again or None) the night shift replays ``change`` on next; None when it has as
    many replays as the charter wants, or no task fits the worker on duty."""
    rp, t = tables.curator_replays, tables.replay_tasks
    query = select(rp, t.c.unusable).join(t, t.c.id == rp.c.task_id).where(rp.c.change_id == change.id)
    done = (await conn.execute(query.order_by(rp.c.id))).all()
    for row in done:
        if row.state == "queued" and row.unusable is not None:  # another replay found its task unusable since
            why = f"the task is unusable: {row.unusable}"[: replay.MAX_REASON_CHARS]
            await conn.execute(
                update(rp).values(state="failed", reason=why, updated_at=func.now()).where(rp.c.id == row.id)
            )
    again = next((row for row in done if row.state == "queued" and row.unusable is None), None)
    if again is not None:
        task = (await conn.execute(select(t).where(t.c.id == again.task_id))).one()
        return (
            (task, again) if not unfit(gate.worker, due.project, "replay", [task.repo, change.repo], runtime) else None
        )
    counted = [row for row in done if row.unusable is None]  # a task found unusable gives its place to the next
    if any(row.state == "running" for row in done) or len(counted) >= wanted:
        return None
    used = [row.task_id for row in done]
    candidates = await conn.execute(
        select(t)
        .where(t.c.project_id == due.project_id, t.c.split == "holdout", t.c.unusable.is_(None), t.c.id.not_in(used))
        .order_by(t.c.id)
    )
    for task in candidates:
        if not unfit(gate.worker, due.project, "replay", [task.repo, change.repo], runtime):
            return task, None
    return None


def _repos(task, change) -> list[dict]:
    repos = [{"repo": task.repo, "branch": None}] if task.repo != change.repo else []
    return [*repos, {"repo": change.repo, "branch": change.branch}]


async def _queued(conn: AsyncConnection, due, change, task, again, run_id: int, budget: dict) -> None:
    rp = tables.curator_replays
    if again is None:
        await conn.execute(
            insert(rp).values(
                project_id=due.project_id, change_id=change.id, task_id=task.id, run_id=run_id, state="running"
            )
        )
    else:
        await conn.execute(
            update(rp)
            .values(
                state="running", run_id=run_id, result_a=null(), result_b=null(), reason=None, updated_at=func.now()
            )
            .where(rp.c.id == again.id)
        )
    caps = f"cost cap {curator.money(budget['max_usd'])}, {budget['max_seconds'] // 60} minutes of agent time"
    text = (
        f"Queued by the night shift of project {due.project} as the replay of step {task.step_key} of plan "
        f"{task.plan_id} for the Curator's change #{change.id}: arm a with the harness of the default branch of "
        f"{change.repo}, arm b with that of {change.branch}, each in a new directory at the step's base commit: {caps}."
    )
    await write_event(conn, run_id, {"text": text, "change_id": change.id, "task_id": task.id, **budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=REPLAY,
        target=f"{due.project}/{change.plan_id} run:{run_id} change:{change.id} task:{task.id}",
        project_id=due.project_id,
    )
    log.info("curator replay queued", extra={"project": due.project, "run_id": run_id, "change_id": change.id})


def _values(due, gate, change, held, task, runtime: str, budget: dict) -> dict:
    builder = due.body.get("builder") or {}
    return {
        "kind": "replay",
        "project_id": due.project_id,
        "plan_id": change.plan_id,
        "title": f"Replay of {task.plan_id}#{task.step_key} for the Curator's change #{change.id}"[:200],
        "plan_revision": held.revision,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        "pinned_worker_id": gate.worker.id,
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": builder.get("model"),
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "max_attempts": 1,  # a replay run that ends without both arms is replayed again by its replay, not the reaper
        "repos": _repos(task, change),
        "schedule_id": due.id,
        "schedule_night": gate.night,
        "budget": budget,
    }


async def queue_replay(conn: AsyncConnection, due, gate) -> int | None:
    """Queue the next replay run of the project's changes (see the module's docstring); its id, or None. Called under
    the schedule's row lock."""
    settings = due.body.get("replay") or {}
    wanted, cap = int(settings.get("tasks", replay.DEFAULT_TASKS) or 0), settings.get("budget_usd")
    if wanted <= 0 or cap is None:
        return None
    left = min(gate.budget["max_usd"], cap - await kind_cost(conn, due.id, gate.night, "replay"))
    if left < curator.MIN_RUN_USD:
        return None
    runtime = (due.body.get("builder") or {}).get("runtime") or curator.DEFAULT_RUNTIME
    for change in await _replayable(conn, due.project_id):
        held = await plan_routes._held(conn, gate.access, change.plan_id)
        if held is None:
            continue
        await lock_plan(conn, due.project_id, change.plan_id)
        activity = await plan_activity(conn, due.project_id, change.plan_id)
        if activity.plan_run is not None or activity.steps:
            continue  # its Builder runs: the branch may still move
        found = await _next_task(conn, due, gate, change, wanted, runtime)
        if found is None:
            continue
        task, again = found
        budget = {**gate.budget, "max_usd": round(left, 4)}
        try:
            run_id = await insert_run(conn, _values(due, gate, change, held, task, runtime, budget))
        except IntegrityError:  # another replay run of the project is active
            return None
        await _queued(conn, due, change, task, again, run_id, budget)
        return run_id
    return None


# The claim of a replay run


def _task_spec(task) -> dict:
    return {
        "id": task.id,
        "plan_id": task.plan_id,
        "step_key": task.step_key,
        "repo": task.repo,
        "title": task.title,
        "what": task.what,
        "verify": [task.verify],
        "commits": list(task.commits),
        "base_sha": task.base_sha,
    }


async def replay_claim(conn: AsyncConnection, change, run_id: int) -> dict:
    """What the claim of replay run ``run_id`` of ``change`` adds for its worker: the replay, its task and a new key of
    the run (``replay_key``), whose SHA-256 alone the hub keeps; nothing when the run replays nothing now."""
    rp, t = tables.curator_replays, tables.replay_tasks
    row = (
        await conn.execute(
            select(rp.c.id, rp.c.task_id).where(rp.c.run_id == run_id, rp.c.state == "running").with_for_update()
        )
    ).one_or_none()
    if row is None:
        return {}
    task = (await conn.execute(select(t).where(t.c.id == row.task_id))).one()
    key = secrets.token_urlsafe(32)
    await conn.execute(update(rp).values(replay_key=_key_digest(key), updated_at=func.now()).where(rp.c.id == row.id))
    return {"replay": {"replay_id": row.id, "task": _task_spec(task), "repo": change.repo}, "replay_key": key}


async def run_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of replay run ``view``, as its worker builds it for each arm (``replay.replay_prompt``)."""
    rp, t = tables.curator_replays, tables.replay_tasks
    task = (await conn.execute(select(t).join(rp, rp.c.task_id == t.c.id).where(rp.c.run_id == view.id))).one_or_none()
    if task is None:
        return "This replay run replays nothing: end it."
    return replay.replay_prompt(view.project, _task_spec(task), folder=task.repo)


# The replay run's side


async def _held_replay(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """The row of curator_replays of replay run ``run_id``, which the worker of ``user`` holds, locked; 404 for any
    other run, 409 for a replay that waits for nothing of it, 403 without the run's own key."""
    worker_id = (await worker_of(conn, user))[0]
    r, rp = tables.runs, tables.curator_replays
    run = (
        await conn.execute(select(r.c.state, r.c.worker_id, r.c.kind).where(r.c.id == run_id).with_for_update())
    ).one_or_none()
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if run.kind != "replay":
        raise HTTPException(404, f"run {run_id} is not a replay run: only a replay run reports a replay")
    row = (await conn.execute(select(rp).where(rp.c.run_id == run_id).with_for_update())).one_or_none()
    if row is None or row.state != "running":
        raise HTTPException(409, f"run {run_id} replays nothing that waits for it")
    if not key or row.replay_key is None or not hmac.compare_digest(_key_digest(key), row.replay_key):
        raise HTTPException(
            403,
            f"run {run_id} is a replay run: its results take the run's own key ({replay.REPLAY_KEY_HEADER}), which "
            "its claim handed the daemon; the worker token alone writes no result",
        )
    return row


async def _refused(conn: AsyncConnection, row, body: ReplayReport, run_id: int) -> None:
    rp, t = tables.curator_replays, tables.replay_tasks
    await conn.execute(
        update(rp).values(state="failed", reason=body.refused, updated_at=func.now()).where(rp.c.id == row.id)
    )
    if body.task_unusable:
        await conn.execute(update(t).values(unusable=body.refused).where(t.c.id == row.task_id, t.c.unusable.is_(None)))
    what = "the task is unusable from now on" if body.task_unusable else "the replay fails"
    await write_event(conn, run_id, {"text": f"The worker cannot replay the task: {body.refused}; {what}."})


async def _arm(conn: AsyncConnection, row, body: ReplayReport, run_id: int) -> None:
    rp, t = tables.curator_replays, tables.replay_tasks
    if getattr(row, f"result_{body.arm}") is not None:
        raise HTTPException(409, f"run {run_id} reported arm {body.arm} already")
    other = row.result_b if body.arm == "a" else row.result_a
    values = {f"result_{body.arm}": body.result.model_dump(), "base_sha": row.base_sha or body.base_sha}
    if other is not None:
        values["state"] = "done"
    await conn.execute(update(rp).values(**values, updated_at=func.now()).where(rp.c.id == row.id))
    await conn.execute(update(t).values(base_sha=body.base_sha).where(t.c.id == row.task_id, t.c.base_sha.is_(None)))
    word = "passed" if body.result.passed else "failed"
    await write_event(
        conn, run_id, {"text": f"Arm {body.arm} of the replay {word} the step's verify.", "arm": body.arm}
    )


@worker_router.post("/runs/{run_id}/replay", response_model=Replay, responses=REFUSALS)
async def report_replay(
    request: Request, run_id: RunId, body: ReplayReport, user: CurrentUser, replay_key: ReplayKey = None
) -> Replay:
    """One arm's result of the replay run this worker holds, or why it cannot replay its task; with the run's key."""
    async with request.app.state.engine.begin() as conn:
        row = await _held_replay(conn, user, run_id, replay_key)
        if body.refused is not None:
            await _refused(conn, row, body, run_id)
        else:
            await _arm(conn, row, body, run_id)
        (found,) = await _replays(conn, [row.id])
    log.info("replay reported", extra={"run_id": run_id, "replay_id": row.id, "arm": body.arm})
    return found


async def replay_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> None:
    """What the end of replay run ``found`` does to its replay: one that ended without both arms is replayed again,
    REPLAY_ATTEMPTS runs in all, then fails with why; a failure sends the owner the notice run_failed."""
    if new not in runs.TERMINAL_STATES or found.project_id is None:
        return
    from evo_agents.hub.server.notifications import notify, run_link

    why = found.error or reason
    if new == "failed":
        await notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=found.project_id,
            run_id=found.run_id,
            title=f"Replay run #{found.run_id} of {found.plan_id} failed",
            body=why,
            details={"run_kind": "replay", "plan_id": found.plan_id, "error": why},
            link=run_link(found.project, found.run_id),
        )
    rp = tables.curator_replays
    row = (await conn.execute(select(rp).where(rp.c.run_id == found.run_id).with_for_update())).one_or_none()
    if row is None or row.state != "running":
        return
    attempts = row.attempts + 1
    last = attempts >= replay.REPLAY_ATTEMPTS
    text = f"replay run #{found.run_id} ended {new} without both arms ({why})"
    values = {"state": "failed" if last else "queued", "attempts": attempts}
    values["reason"] = (f"{attempts} replay runs ended without both arms, the last {new}: {why}" if last else text)[
        : replay.MAX_REASON_CHARS
    ]
    await conn.execute(update(rp).values(**values, updated_at=func.now()).where(rp.c.id == row.id))


# Reading


def _task_view(row) -> ReplayTask:
    return ReplayTask(**{key: getattr(row, key) for key in ReplayTask.model_fields})


async def _replays(conn: AsyncConnection, ids: list[int]) -> list[Replay]:
    rp, t = tables.curator_replays, tables.replay_tasks
    query = (
        select(rp, t.c.plan_id, t.c.step_key, t.c.repo, t.c.title, t.c.split)
        .join(t, t.c.id == rp.c.task_id)
        .where(rp.c.id.in_(ids))
        .order_by(rp.c.id)
    )
    return [
        Replay(
            **{key: value for key, value in row._mapping.items() if key in Replay.model_fields},
            a=row.result_a,
            b=row.result_b,
        )
        for row in await conn.execute(query)
    ]


async def _reader_access(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    need_reader(access)
    return access


async def tasks_of(conn: AsyncConnection, access: ProjectAccess, *, split: str | None, limit: int, sink: str | None):
    """The project's suite as the caller may read it through ``sink``: (the tasks of ``split``, at most ``limit``,
    the count of each split)."""
    t = tables.replay_tasks
    rows = (await conn.execute(select(t).where(t.c.project_id == access.project_id).order_by(t.c.id))).all()
    through = plan_routes._sink(access, sink)
    readable = [row for row in rows if access.visible(row.label, through)]
    counts = {name: sum(row.split == name for row in readable) for name in replay.SPLITS}
    shown = [_task_view(row) for row in readable if split is None or row.split == split]
    return shown[:limit], counts, len(shown)


@router.get("/{project}/curator/replay/tasks", response_model=ReplayTaskList, responses=REFUSALS)
async def list_tasks(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    split: Annotated[Split | None, Query(description="the tasks of this split alone")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_TASKS_LISTED)] = 100,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> ReplayTaskList:
    """The project's suite of replay tasks, in the order they came in, of the plans the caller may read."""
    async with request.app.state.engine.begin() as conn:
        access = await _reader_access(conn, user, project)
        tasks, counts, total = await tasks_of(conn, access, split=split, limit=limit, sink=sink)
    return ReplayTaskList(project=access.name, split=split, tasks=tasks, total=total, counts=counts)


async def change_replay(conn: AsyncConnection, access: ProjectAccess, change_id: int, sink: str | None) -> ChangeReplay:
    """The replays of change ``change_id`` of the project the caller may read; 404 when it may not read the change."""
    c, pr, rp, t = tables.curator_changes, tables.proposals, tables.curator_replays, tables.replay_tasks
    change = (
        await conn.execute(
            select(c.c.id, pr.c.label)
            .join_from(c, pr, pr.c.id == c.c.proposal_id)
            .where(c.c.id == change_id, c.c.project_id == access.project_id)
        )
    ).one_or_none()
    through = plan_routes._sink(access, sink)
    if change is None or not access.visible(change.label, through):
        raise HTTPException(404, f"project {access.name} has no change #{change_id} of the Curator you may read")
    rows = await conn.execute(
        select(rp.c.id, t.c.label).join(t, t.c.id == rp.c.task_id).where(rp.c.change_id == change_id).order_by(rp.c.id)
    )
    shown = await _replays(conn, [row.id for row in rows if access.visible(row.label, through)])
    ch = tables.charters
    charter = (
        await conn.execute(
            select(ch.c.body).where(ch.c.project_id == access.project_id).order_by(ch.c.revision.desc()).limit(1)
        )
    ).scalar_one_or_none() or {}
    wanted = (charter.get("replay") or {}).get("tasks", replay.DEFAULT_TASKS)
    arms = replay.compare([{"result_a": item.a, "result_b": item.b} for item in shown])
    return ChangeReplay(project=access.name, change_id=change_id, tasks=wanted, replays=shown, arms=arms)


@router.get("/{project}/curator/changes/{change_id}/replay", response_model=ChangeReplay, responses=REFUSALS)
async def show_change_replay(
    request: Request,
    project: ProjectName,
    change_id: Annotated[int, Path(ge=1, le=MAX_ID)],
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> ChangeReplay:
    """A change's replays: each task, what arm a (the default branch's harness) and arm b (the change's) found."""
    async with request.app.state.engine.begin() as conn:
        access = await _reader_access(conn, user, project)
        return await change_replay(conn, access, change_id, sink)


# The MCP tool


def _arm_text(item: dict | None) -> str:
    if not item:
        return "-"
    cost = curator.money(item["cost_usd"]) if item.get("cost_usd") is not None else "cost -"
    word = "passed" if item.get("passed") else "failed"
    return f"{word}, {cost}, {item.get('turns') or 0} turns, {item.get('tool_errors') or 0} tool errors"


async def mcp_answer(request: Request, project: str, user: Principal, arguments: dict, *, sink: str | None):
    """(text, data) of the MCP tool curator_replay. The agent of a run (the Reviewer, a Builder) lists the select split
    of the suite alone: the holdout split, which changes are measured on, stays out of what shapes them."""
    split = arguments.get("split")
    if user.scope is not None and "change_id" not in arguments:
        if split == "holdout":
            raise HTTPException(403, "the agent of a run lists the select split of the replay suite alone")
        split = "select"
    async with request.app.state.engine.begin() as conn:
        access = await _reader_access(conn, user, project)
        if "change_id" in arguments:
            found = await change_replay(conn, access, arguments["change_id"], sink)
        else:
            tasks, counts, total = await tasks_of(
                conn, access, split=split, limit=arguments.get("limit", 20), sink=sink
            )
    if "change_id" in arguments:
        arms = found.arms
        lines = [
            f"Change #{found.change_id} of project {project}: {len(found.replays)} of {found.tasks} replays; arm a "
            f"passed {arms['a']['passed']} of {arms['a']['replays']}, arm b {arms['b']['passed']} of "
            f"{arms['b']['replays']}. Arm a has the harness of the default branch, arm b the change's."
        ]
        for item in found.replays:
            lines.append(
                f"#{item.id} {item.plan_id}#{item.step_key} ({item.state}): a {_arm_text(item.a)}; b {_arm_text(item.b)}"
            )
        data = {**found.model_dump(mode="json"), "summary": f"{len(found.replays)} replays"}
        return "\n".join(lines), data
    lines = [f"The replay suite of project {project}: {counts['select']} select, {counts['holdout']} holdout."]
    lines += [
        f"#{task.id} {task.split} {task.plan_id}#{task.step_key} in {task.repo}: {task.title or ''}" for task in tasks
    ]
    data = {"project": project, "tasks": [task.model_dump(mode="json") for task in tasks], "total": total}
    return "\n".join(lines), {**data, "counts": counts, "summary": f"{total} tasks"}
