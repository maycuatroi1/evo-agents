"""The Curator's replays on the hub: the night shift's replay runs, what their workers report, and reading them.
``evo_agents.hub.replay`` holds the model and ``evo_agents.hub.replay_suite`` the job that builds each project's suite
of replay tasks.

The night shift (``curator.service.shift``) queues, after a judge run that waits and the Builder of a planned change,
the replay run of the oldest change of the Curator that waits for one (``queue_replay``): a change built (its Builder
ran, its branch is on origin), not merged or closed (``replay.REPLAY_STATES``) and not failed by the Judge (``passed``
false), whose proposal is of kind skill or names a skill, an AGENTS.md or a CLAUDE.md (``replay.harness_path``), with no
run of its plan active. It takes the tasks of the holdout split of the project's suite in the order they came in, those
not found unusable whose repos the worker on duty has, up to the charter's ``replay.tasks`` per change, one replay run
per task: a run of kind replay on the change's plan, pinned to the worker on duty, as the schedule's owner, with the
runtime and model of the charter's Builder, and caps that keep the night's replay runs within ``replay.budget_usd`` (a
charter without it, or with ``replay.tasks`` 0, replays nothing). A replay run that ends without both arms is replayed
again, REPLAY_ATTEMPTS runs in all, then the replay fails with why (``replay_ended``); one whose worker refused it (no
base commit for the task, no harness file in the change) fails at once, and a task whose commits are nowhere on origin
is unusable from then on.

The claim of a replay run tells its worker the replay, the task and the change's branch and head, and hands it the
run's own key (``replay_claim``), which the hub keeps as a SHA-256: the worker posts each arm's result with POST
/v1/worker/runs/{id}/replay with that key alone (``replay.REPLAY_KEY_HEADER``), so code on the worker's machine that
reads the worker token writes no result. The first arm that names the task's base commit sets it on the task, once.
The Judge reads none of this: its prompt and its tools hold no replay. A replay done gives a change the Judge passed
and the hub left open its merge again (``_won_again``): once its replays win (``change_tiers.replay_verdict``), the
change is ``judged`` again and the job reads its pull request once more, which merges it as tier 0 when its diff edits a
harness alone (``evo_agents.hub.server.change_tiers``).

GET /v1/projects/{p}/curator/replay/tasks (reader) lists the project's suite, of ``split`` when given, the tasks of
plans the caller may read through the project's hub sink; GET .../curator/changes/{id}/replay (reader) a change's
replays with what each arm found, ``replay.compare`` of them and ``replay.verdict`` at the head of its pull request,
whether they won. The MCP tool curator_replay answers both (``mcp.mcp_answer``); to the agent of a run, the suite's
select split alone and no change's replays, and no replay run reads for it either (``views.agent_reads``).

The package has four layers, which import-linter keeps in order (contract ``hub-replays-layers``): ``routes`` and
``mcp`` (the HTTP routes and the MCP tool), then ``service`` (this module: queueing, the claim, the worker's report
and the reads), ``repository`` (every SQL statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic
bodies, the audit action and the refusals).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, replay, runs
from evo_agents.hub.server import audit, change_tiers
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound
from evo_agents.hub.server.night_cost import kind_cost
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.replays import repository as store
from evo_agents.hub.server.replays.models import (
    HARNESS_KINDS,
    REPLAY,
    ChangeReplay,
    Replay,
    ReplayReport,
    ReplayTask,
)
from evo_agents.hub.server.run_state import RunStep, notify_queued, write_event
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.runs.service.views import lock_plan, plan_activity
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


# Queueing


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _harness_change(row) -> bool:
    paths = [item.get("path") for item in row.paths or [] if isinstance(item, dict) and item.get("repo") == row.repo]
    return row.kind in HARNESS_KINDS or bool(replay.harness_paths(paths))


async def _replayable(conn: AsyncConnection, project_id: int) -> list:
    """The project's changes a replay may measure, oldest first (see the module's docstring)."""
    return [row for row in await store.replayable(conn, project_id) if _harness_change(row)]


async def _next_task(conn: AsyncConnection, due, gate, change, wanted: int, runtime: str):
    """(the task, the replay to run again or None) the night shift replays ``change`` on next; None when it has as
    many replays as the charter wants, or no task fits the worker on duty."""
    done = await store.replays_of_change(conn, change.id)
    for row in done:
        if row.state == "queued" and row.unusable is not None:  # another replay found its task unusable since
            why = f"the task is unusable: {row.unusable}"[: replay.MAX_REASON_CHARS]
            await store.fail_replay(conn, row.id, why)
    again = next((row for row in done if row.state == "queued" and row.unusable is None), None)
    if again is not None:
        task = await store.task(conn, again.task_id)
        return (task, again) if not gate.duty.unfit(due.project, "replay", [task.repo, change.repo], runtime) else None
    counted = [row for row in done if row.unusable is None]  # a task found unusable gives its place to the next
    if any(row.state == "running" for row in done) or len(counted) >= wanted:
        return None
    used = [row.task_id for row in done]
    for task in await store.holdout_tasks(conn, due.project_id, used):
        if not gate.duty.unfit(due.project, "replay", [task.repo, change.repo], runtime):
            return task, None
    return None


def _repos(task, change) -> list[dict]:
    repos = [{"repo": task.repo, "branch": None}] if task.repo != change.repo else []
    return [*repos, {"repo": change.repo, "branch": change.branch}]


async def _queued(conn: AsyncConnection, due, change, task, again, run_id: int, budget: dict) -> None:
    if again is None:
        await store.start_replay(conn, due.project_id, change.id, task.id, run_id)
    else:
        await store.restart_replay(conn, again.id, run_id)
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
        **gate.duty.columns(),
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
        held = await plan_service.held(conn, gate.access, change.plan_id)
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
    row = await store.lock_running(conn, run_id)
    if row is None:
        return {}
    task = await store.task(conn, row.task_id)
    key = secrets.token_urlsafe(32)
    await store.set_key(conn, row.id, _key_digest(key))
    return {"replay": {"replay_id": row.id, "task": _task_spec(task), "repo": change.repo}, "replay_key": key}


async def run_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of replay run ``view``, as its worker builds it for each arm (``replay.replay_prompt``)."""
    task = await store.task_of_run(conn, view.id)
    if task is None:
        return "This replay run replays nothing: end it."
    return replay.replay_prompt(view.project, _task_spec(task), folder=task.repo)


# The replay run's side


async def _held_replay(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """The row of curator_replays of replay run ``run_id``, which the worker of ``user`` holds, locked; 404 for any
    other run, 409 for a replay that waits for nothing of it, 403 without the run's own key."""
    worker_id = (await worker_of(conn, user))[0]
    run = await store.lock_run(conn, run_id)
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise NotFound(NOT_HELD.format(id=run_id))
    if run.kind != "replay":
        raise NotFound(f"run {run_id} is not a replay run: only a replay run reports a replay")
    row = await store.lock_replay_of_run(conn, run_id)
    if row is None or row.state != "running":
        raise Conflict(f"run {run_id} replays nothing that waits for it")
    if not key or row.replay_key is None or not hmac.compare_digest(_key_digest(key), row.replay_key):
        raise Forbidden(
            f"run {run_id} is a replay run: its results take the run's own key ({replay.REPLAY_KEY_HEADER}), which "
            "its claim handed the daemon; the worker token alone writes no result"
        )
    return row


async def _refused(conn: AsyncConnection, row, body: ReplayReport, run_id: int) -> None:
    await store.fail_replay(conn, row.id, body.refused)
    if body.task_unusable:
        await store.task_unusable(conn, row.task_id, body.refused)
    what = "the task is unusable from now on" if body.task_unusable else "the replay fails"
    await write_event(conn, run_id, {"text": f"The worker cannot replay the task: {body.refused}; {what}."})


async def _arm(conn: AsyncConnection, row, body: ReplayReport, run_id: int) -> None:
    if getattr(row, f"result_{body.arm}") is not None:
        raise Conflict(f"run {run_id} reported arm {body.arm} already")
    result = body.result
    assert result is not None  # an arm's report has its result (ReplayReport)
    other = row.result_b if body.arm == "a" else row.result_a
    values = {f"result_{body.arm}": result.model_dump(), "base_sha": row.base_sha or body.base_sha}
    if other is not None:
        values["state"] = "done"
    await store.write_replay(conn, row.id, values)
    await store.task_base(conn, row.task_id, body.base_sha)
    word = "passed" if result.passed else "failed"
    await write_event(
        conn, run_id, {"text": f"Arm {body.arm} of the replay {word} the step's verify.", "arm": body.arm}
    )
    if other is not None:
        await _won_again(conn, row.change_id, run_id)


async def _won_again(conn: AsyncConnection, change_id: int, run_id: int) -> None:
    """A change left open after the Judge passed it goes back to ``judged`` once its replays win, so the job reads its
    pull request again and merges it as tier 0 when everything else still holds."""
    change = await store.lock_change(conn, change_id)
    if change.state != "open" or change.passed is not True or change.forge != "github":
        return
    found = await change_tiers.replay_verdict(conn, change.id, change.head_sha)
    if not found["won"]:
        return
    why = f"its replays won ({found['reason']}): the hub reads its pull request again to merge it"
    values = {"state": "judged", "reason": why[: replay.MAX_REASON_CHARS]}
    await store.judge_again(conn, change.id, values)
    await write_event(conn, run_id, {"text": f"The replays of the Curator's change #{change.id} won.", "won": True})
    log.info("curator change judged again by its replays", extra={"change_id": change.id, "run_id": run_id})


async def report(conn: AsyncConnection, user: Principal, run_id: int, body: ReplayReport, key: str | None):
    """One arm's result of the replay run the worker of ``user`` holds, or why it cannot replay its task: the replay,
    and its id."""
    row = await _held_replay(conn, user, run_id, key)
    if body.refused is not None:
        await _refused(conn, row, body, run_id)
    else:
        await _arm(conn, row, body, run_id)
    (found,) = await replays(conn, [row.id])
    return found, row.id


async def replay_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> None:
    """What the end of replay run ``found`` does to its replay: one that ended without both arms is replayed again,
    REPLAY_ATTEMPTS runs in all, then fails with why; a failure sends the owner the notice run_failed."""
    if new not in runs.TERMINAL_STATES or found.project_id is None:
        return
    from evo_agents.hub.server.notifications.service.notifier import NOTIFIER

    why = found.error or reason
    if new == "failed":
        await NOTIFIER.notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=found.project_id,
            run_id=found.run_id,
            title=f"Replay run #{found.run_id} of {found.plan_id} failed",
            body=why,
            details={"run_kind": "replay", "plan_id": found.plan_id, "error": why},
            link=NOTIFIER.run_link(found.project, found.run_id),
        )
    row = await store.lock_replay_of_run(conn, found.run_id)
    if row is None or row.state != "running":
        return
    attempts = row.attempts + 1
    last = attempts >= replay.REPLAY_ATTEMPTS
    text = f"replay run #{found.run_id} ended {new} without both arms ({why})"
    values = {"state": "failed" if last else "queued", "attempts": attempts}
    values["reason"] = (f"{attempts} replay runs ended without both arms, the last {new}: {why}" if last else text)[
        : replay.MAX_REASON_CHARS
    ]
    await store.write_replay(conn, row.id, values)


# Reading


def _task_view(row) -> ReplayTask:
    return ReplayTask(**{key: getattr(row, key) for key in ReplayTask.model_fields})


async def replays(conn: AsyncConnection, ids: list[int]) -> list[Replay]:
    return [
        Replay(
            **{key: value for key, value in row._mapping.items() if key in Replay.model_fields},
            a=row.result_a,
            b=row.result_b,
        )
        for row in await store.replays(conn, ids)
    ]


async def reader_access(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    need_reader(access)
    return access


async def tasks_of(conn: AsyncConnection, access: ProjectAccess, *, split: str | None, limit: int, sink: str | None):
    """The project's suite as the caller may read it through ``sink``: (the tasks of ``split``, at most ``limit``,
    the count of each split)."""
    rows = await store.tasks(conn, access.project_id)
    readable = [row for row in rows if plan_service.sees(access, row.label, sink)]
    counts = {name: sum(row.split == name for row in readable) for name in replay.SPLITS}
    shown = [_task_view(row) for row in readable if split is None or row.split == split]
    return shown[:limit], counts, len(shown)


async def change_replay(conn: AsyncConnection, access: ProjectAccess, change_id: int, sink: str | None) -> ChangeReplay:
    """The replays of change ``change_id`` of the project the caller may read; 404 when it may not read the change."""
    change = await store.change_of_project(conn, change_id, access.project_id)
    if change is None or not plan_service.sees(access, change.label, sink):
        raise NotFound(f"project {access.name} has no change #{change_id} of the Curator you may read")
    rows = await store.replay_labels(conn, change_id)
    shown = await replays(conn, [row.id for row in rows if plan_service.sees(access, row.label, sink)])
    charter = await store.newest_charter_body(conn, access.project_id) or {}
    wanted = (charter.get("replay") or {}).get("tasks", replay.DEFAULT_TASKS)
    arms = replay.compare([{"result_a": item.a, "result_b": item.b} for item in shown])
    rows = [{"split": item.split, "state": item.state, "result_a": item.a, "result_b": item.b} for item in shown]
    found = replay.verdict(rows, change.head_sha)
    return ChangeReplay(project=access.name, change_id=change_id, tasks=wanted, replays=shown, arms=arms, verdict=found)
