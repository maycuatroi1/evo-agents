"""A plan run waits on a run of a GitHub Actions workflow, such as the CI of a release tag or the deploy of a merge
(``evo-agents worker wait --on github.workflow_run --contains run_id=ID``), and wakes when that run completes or a
job of it fails, rather than only at ``--until``.

GitHub tells the hub nothing of Actions: the hub serves no webhook of GitHub (the one webhook it serves is Telegram's,
``security.PUBLIC_PATHS``) and its Apps subscribe to no event. What it knows of a workflow run it reads with the
workers' App, as the release watch does (``release_actions``: a token with actions read, revoked after). So the hub
polls, and only the runs an open wait names: ``poll``, on each pass of the reaper and before the waits wake
(``run_reaper.recover_runs``), reads each such run once, however many waits name it, with the jobs of its latest
attempt, and records event ``github.workflow_run`` (``hub_events.WORKFLOW_RUN``) of the wait's project when the run
completed, or when a job of it failed while others still run; ``run_waits.wake_waits`` then wakes, in the same pass,
each wait whose ``contains`` the event's payload holds. A webhook would wake it seconds sooner, but needs a public
route, its secret and the App's subscription to workflow_run on every installation, and still a poll for a delivery
GitHub dropped; a reaper's minute is little beside a CI of twenty.

An event is recorded once for each attempt of the run and each of its two moments (a job failed while the run goes on;
the run completed) after the latest wait on it was asked: a wait asked after the run completed still wakes at the next
pass, and one that waits for the end alone is not woken by a failure before it.

A wait that names the run alone wakes on its first failed job: a release whose CI job failed publishes nothing (its
build needs ci), and the agent fixes it forward at once rather than sleep while the other jobs finish (run #1641 slept
until 03:37 UTC with ten jobs failed by 03:12). One that waits for the end adds ``status=completed``; one that waits for
a later attempt, ``attempt=N``.

The payload (``VERSION`` 1): ``repo`` (owner/name), ``run_id``, ``attempt``, ``workflow`` (its name), ``head_branch``,
``head_sha``, ``status`` and ``conclusion`` as GitHub says them, ``failed`` (a job of the attempt failed, or the run
did), ``failed_jobs`` (their names, at most ``release_ci.MAX_FAILED_JOBS``) and ``url``.

Each read is kept too, whether or not it records an event (``store``, table workflow_run_reads, schema 0236): the run,
the jobs of its latest attempt (at most MAX_JOBS, those that run or failed first, with how many GitHub lists) and the
steps of each (at most MAX_STEPS), as ``snapshot`` keeps them, so the views of a wait (``waiting_on``) show where the run
stands as of the latest pass and nobody's look calls GitHub. The first read of a run also reads how long the latest
TYPICAL_RUNS runs of its workflow that succeeded took (``typical_seconds``, their median), once. A read nobody refreshed
for KEEP_READS goes.

``checked``, in the route of a wait, takes the repo of ``contains`` (owner/name, the project's name for it, or nothing
when the project has one GitHub repo) to be one of the project's GitHub repos, and the run to be there: a wait the hub
cannot keep is 422 at once, saying what to do instead, rather than a sleep until ``--until``.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

from fastapi import HTTPException
from sqlalchemy import delete, exists, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import hub_events, release_ci, runs, tables
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.release_actions import recent_successes, run_by_id, run_jobs, with_actions

log = logging.getLogger(__name__)

KIND = hub_events.WORKFLOW_RUN
VERSION = 1
TEXT_KEYS = (
    "repo",
    "workflow",
    "head_branch",
    "head_sha",
    "status",
    "conclusion",
)  # keys of the payload that hold text
NUMBER_KEYS = ("run_id", "attempt")
MAX_TEXT = 300  # characters of a text of the payload
WOKE_HINT = (
    "A wait on a workflow run wakes on its first failed job: read that job's log (`gh run view RUN_ID --repo "
    "OWNER/NAME --log-failed`) and fix it forward; to wait for the run's end instead, wait again with --contains "
    "status=completed."
)
WAIT_INSTEAD = "wait with --until alone and look at the run with gh when it wakes"
MAX_JOBS = 40  # jobs of a read kept: a matrix can list hundreds
MAX_STEPS = 60  # steps of a job kept
TYPICAL_RUNS = 5  # runs of the workflow that succeeded whose median time is its typical one
KEEP_READS = timedelta(days=30)  # a read nobody refreshed that long goes
RUN_FIELDS = {  # the key a snapshot keeps: GitHub's for it
    "workflow": "name",
    "title": "display_title",
    "branch": "head_branch",
    "sha": "head_sha",
    "event": "event",
    "status": "status",
    "conclusion": "conclusion",
    "url": "html_url",
    "created_at": "created_at",
    "started_at": "run_started_at",
    "updated_at": "updated_at",
}
TIMES = ("started_at", "completed_at", "status", "conclusion")


@dataclass(frozen=True)
class Watched:
    """A run of a workflow that open waits name: its project, its repo, and the latest of the waits' cursors."""

    project_id: int
    owner: str
    repo: str
    run_id: int
    after: int


# The route


async def github_repos(conn: AsyncConnection, project_id: int) -> dict[str, tuple[str, str]]:
    """The GitHub repos of project ``project_id``: (owner, name) by the project's name for each."""
    p = tables.project_repos
    query = select(p.c.name, p.c.origin).where(p.c.project_id == project_id, p.c.origin.is_not(None))
    return {name: found for name, origin in await conn.execute(query) if (found := github_repo(origin)) is not None}


def _number(value) -> int | None:
    """A positive integer given as a number or as its digits, else None."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if type(value) is int and value > 0 else None


def _repo_of(repos: dict[str, tuple[str, str]], named) -> tuple[str, str]:
    """(owner, name) of the project's GitHub repo ``named`` (owner/name or the project's name for it), or of its one
    GitHub repo when nothing is named; 422 otherwise."""
    pairs = sorted(set(repos.values()))
    listed = ", ".join(f"{owner}/{name}" for owner, name in pairs) or "none"
    if named is None and len(pairs) == 1:
        return pairs[0]
    if named is None:
        raise HTTPException(422, f"name the run's repo with --contains repo=OWNER/NAME: the project's are {listed}")
    if isinstance(named, str) and named in repos:
        return repos[named]
    for owner, name in pairs:
        if isinstance(named, str) and f"{owner}/{name}".lower() == named.lower():
            return owner, name
    raise HTTPException(422, f"repo {named!r} is not a GitHub repo of the project: its GitHub repos are {listed}")


async def checked(app: GitHubApp | None, repos: dict[str, tuple[str, str]], contains: dict) -> dict:
    """``contains`` of a wait on a workflow run, its repo as owner/name and its numbers as numbers, once the run is one
    GitHub knows on a repo of the project (see the module); 422 for a wait the hub cannot keep."""
    if app is None:
        raise HTTPException(422, f"this hub has no GitHub App, so it reads no workflow run: {WAIT_INSTEAD}")
    found = dict(contains)
    for key in NUMBER_KEYS:
        if key in found:
            found[key] = _number(found[key])
            if found[key] is None:
                raise HTTPException(422, f"{key} of a workflow run is a positive integer")
    if "run_id" not in found:
        raise HTTPException(
            422, "a wait on github.workflow_run names its run: --contains run_id=ID, the databaseId of `gh run list`"
        )
    owner, repo = _repo_of(repos, found.get("repo"))
    found["repo"] = f"{owner}/{repo}"
    try:
        data = await with_actions(app, owner, repo, "ci", partial(_run, app, owner, repo, found["run_id"]))
    except GitHubUnavailable as exc:  # the poll reads it later
        log.info("workflow run of a wait not read", extra={"run_id": found["run_id"], "why": str(exc)})
        return found
    except GitHubRefused as exc:
        raise HTTPException(422, f"{exc}: {WAIT_INSTEAD}") from exc
    if data is None:
        raise HTTPException(422, f"GitHub knows no workflow run {found['run_id']} on {owner}/{repo}")
    return found


async def _run(app: GitHubApp, owner: str, repo: str, run_id: int, token: str) -> dict | None:
    return await run_by_id(app, token, owner, repo, run_id)


# The poll


async def _watched(conn: AsyncConnection, batch: int) -> list[Watched]:
    """The runs of a workflow the open waits name, at most ``batch`` waits read."""
    d = tables.decisions
    query = (
        select(d.c.project_id, d.c.wake_on)
        .where(
            d.c.category == runs.WAIT_DECISION_CATEGORY,
            d.c.state == "open",
            d.c.wake_on["kind"].astext == KIND,
        )
        .order_by(d.c.id)
        .limit(batch)
    )
    found: dict[tuple[int, str, str, int], int] = {}
    for project_id, wake_on in await conn.execute(query):
        contains = wake_on.get("contains") or {}
        repo, run_id = contains.get("repo"), contains.get("run_id")
        if isinstance(repo, str) and repo.count("/") == 1 and type(run_id) is int:
            owner, name = repo.split("/")
            key = (project_id, owner, name, run_id)
            found[key] = max(found.get(key, 0), int(wake_on.get("after") or 0))
    return [Watched(*key, after=after) for key, after in found.items()]


def _text(value) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) else None


def payload_of(repo: str, run_id: int, data: dict, jobs: list[dict]) -> dict | None:
    """The payload of the event of run ``run_id`` of ``repo`` as GitHub answers it (``data``) with the jobs of its
    latest attempt, or None while it neither completed nor had a job fail."""
    failed_jobs = [name for job in release_ci.failed_jobs(jobs) if (name := _text(job.get("name")))]
    status, conclusion = _text(data.get("status")), _text(data.get("conclusion"))
    failed = bool(failed_jobs) or conclusion in release_ci.FAILED_CONCLUSIONS
    if status != "completed" and not failed:
        return None
    attempt = data.get("run_attempt")
    return {
        "v": VERSION,
        "repo": repo,
        "run_id": run_id,
        "attempt": attempt if type(attempt) is int and attempt > 0 else None,
        "workflow": _text(data.get("name")),
        "head_branch": _text(data.get("head_branch")),
        "head_sha": _text(data.get("head_sha")),
        "status": status,
        "conclusion": conclusion,
        "failed": failed,
        "failed_jobs": failed_jobs,
        "url": _text(data.get("html_url")),
    }


# What a read keeps


def _step(job_url: str | None, item: dict) -> dict | None:
    number = item.get("number")
    if type(number) is not int:
        return None
    url = f"{job_url}#step:{number}:1" if job_url else None
    return {
        "number": number,
        "name": _text(item.get("name")),
        **{key: _text(item.get(key)) for key in TIMES},
        "url": url,
    }


def _job(item: dict) -> dict | None:
    job_id = item.get("id")
    if type(job_id) is not int:
        return None
    url = _text(item.get("html_url"))
    listed = item.get("steps") if isinstance(item.get("steps"), list) else []
    steps = [found for step in listed if isinstance(step, dict) and (found := _step(url, step)) is not None]
    return {
        "id": job_id,
        "name": _text(item.get("name")),
        **{key: _text(item.get(key)) for key in TIMES},
        "url": url,
        "steps": steps[:MAX_STEPS],
        "steps_total": len(steps),
    }


def _first(job: dict) -> int:
    """0 for a job that failed, 1 for one that runs, 2 for any other: the order jobs are kept in past MAX_JOBS."""
    if job.get("conclusion") in release_ci.FAILED_CONCLUSIONS:
        return 0
    return 1 if job.get("status") in ("in_progress", "queued", "waiting", "pending") else 2


def snapshot(repo: str, run_id: int, data: dict, jobs: list[dict]) -> dict:
    """What a read of run ``run_id`` of ``repo`` keeps of GitHub's answer (``data``) and the jobs of its latest attempt:
    the run, and at most MAX_JOBS jobs in GitHub's order, those that failed or run kept first, with ``jobs_total``."""
    attempt = data.get("run_attempt")
    shown = [found for item in jobs if isinstance(item, dict) and (found := _job(item)) is not None]
    kept = sorted(sorted(range(len(shown)), key=lambda index: _first(shown[index]))[:MAX_JOBS])
    return {
        "repo": repo,
        "run_id": run_id,
        "attempt": attempt if type(attempt) is int and attempt > 0 else None,
        **{key: _text(data.get(field)) for key, field in RUN_FIELDS.items()},
        "jobs": [shown[index] for index in kept],
        "jobs_total": len(shown),
    }


def _at(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        return None


def took(item: dict) -> int | None:
    """The seconds a completed run of a workflow took, from its start to its last update, as GitHub lists it."""
    start, end = _at(item.get("run_started_at") or item.get("created_at")), _at(item.get("updated_at"))
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds())


async def _typical(app: GitHubApp, token: str, item: Watched, data: dict) -> int | None:
    """The median time of the latest TYPICAL_RUNS runs of the workflow of ``data`` that succeeded, but this one."""
    path = data.get("path")
    workflow = path.rsplit("/", 1)[-1] if isinstance(path, str) and path else data.get("workflow_id")
    if not isinstance(workflow, str | int) or not workflow:
        return None
    listed = await recent_successes(app, token, item.owner, item.repo, workflow, TYPICAL_RUNS + 1)
    times = [found for run in listed if run.get("id") != item.run_id and (found := took(run)) is not None]
    return int(statistics.median(times[:TYPICAL_RUNS])) if times else None


@dataclass(frozen=True)
class Read:
    """A read of a watched run: the event's payload when one is due, what the read keeps, and the typical time of its
    workflow when this read looked for it."""

    payload: dict | None
    data: dict
    typical: int | None
    looked: bool


async def _read(app: GitHubApp, item: Watched, typical: bool, token: str) -> Read | None:
    data = await run_by_id(app, token, item.owner, item.repo, item.run_id)
    if data is None:
        return None
    jobs = await run_jobs(app, token, item.owner, item.repo, item.run_id)
    repo = f"{item.owner}/{item.repo}"
    seconds = None
    if typical:
        try:
            seconds = await _typical(app, token, item, data)
        except (GitHubUnavailable, GitHubRefused) as exc:  # the run's read stands without it
            log.info("typical time of a workflow not read", extra={"run_id": item.run_id, "why": str(exc)})
    return Read(payload_of(repo, item.run_id, data, jobs), snapshot(repo, item.run_id, data, jobs), seconds, typical)


async def _looked(conn: AsyncConnection, watched: list[Watched]) -> set[tuple[int, str, int]]:
    """The watched runs whose workflow's typical time the hub looked for already."""
    w = tables.workflow_run_reads
    keys = [(item.project_id, f"{item.owner}/{item.repo}", item.run_id) for item in watched]
    if not keys:
        return set()
    query = select(w.c.project_id, w.c.repo, w.c.run_id).where(
        tuple_(w.c.project_id, w.c.repo, w.c.run_id).in_(keys), w.c.typical_read_at.is_not(None)
    )
    return {tuple(row) for row in await conn.execute(query)}


async def store(conn: AsyncConnection, item: Watched, read: Read) -> None:
    """Keep ``read`` as the latest of its run, and the typical time of its workflow when it looked for one."""
    w = tables.workflow_run_reads
    values = {"project_id": item.project_id, "repo": f"{item.owner}/{item.repo}", "run_id": item.run_id}
    fresh = {"data": read.data, "read_at": func.now()}
    if read.looked:
        fresh |= {"typical_seconds": read.typical, "typical_read_at": func.now()}
    statement = insert(w).values(**values, **fresh)
    await conn.execute(
        statement.on_conflict_do_update(index_elements=[w.c.project_id, w.c.repo, w.c.run_id], set_=fresh)
    )


async def _record(conn: AsyncConnection, item: Watched, payload: dict) -> bool:
    """Record ``payload`` unless an event of the same attempt at the same moment came after the latest wait on the
    run; whether it was recorded."""
    e = tables.hub_events
    moment = {"repo": payload["repo"], "run_id": item.run_id, "attempt": payload["attempt"]}
    moment |= {"status": "completed"} if payload["status"] == "completed" else {"failed": True}
    seen = exists().where(
        e.c.project_id == item.project_id, e.c.kind == KIND, e.c.id > item.after, e.c.payload.contains(moment)
    )
    if await conn.scalar(select(seen)):
        return False
    await hub_events.record(conn, KIND, item.project_id, payload)
    return True


async def poll(engine: AsyncEngine, app: GitHubApp | None, batch: int) -> int:
    """Read each run of a workflow an open wait names, of at most ``batch`` waits, keep the read, and record its event
    when it completed or a job of it failed (see the module), each in a transaction of its own; how many were
    recorded."""
    if app is None:
        return 0
    async with engine.begin() as conn:
        w = tables.workflow_run_reads
        await conn.execute(delete(w).where(w.c.read_at < func.now() - KEEP_READS))
        watched = await _watched(conn, batch)
        looked = await _looked(conn, watched)
    recorded = 0
    for item in watched:
        typical = (item.project_id, f"{item.owner}/{item.repo}", item.run_id) not in looked
        try:
            read = await with_actions(app, item.owner, item.repo, "ci", partial(_read, app, item, typical))
        except (GitHubUnavailable, GitHubRefused) as exc:
            log.info("workflow run of a wait not read", extra={"run_id": item.run_id, "why": str(exc)})
            continue
        if read is None:
            continue
        async with engine.begin() as conn:
            await store(conn, item, read)
            if read.payload is not None and await _record(conn, item, read.payload):
                recorded += 1
                log.info("workflow run event recorded", extra={"run_id": item.run_id, "status": read.payload["status"]})
    return recorded
