"""Which ended runs of a worker had their work merged: POST /v1/worker/merged (plan super-board-parity-1, step 1).

A daemon keeps the directory and worktrees of each run it ran for 7 days after the run ended (``worker.daemon``,
``KEEP_WORKTREES``). On each turn of its cleanup it names the ended runs whose worktrees it still keeps, and the hub
answers with those whose work it knows merged; the daemon removes them in that turn instead of waiting out the 7 days.
A run counts as merged here when:

- it is a plan run or a run of one step whose plan marks every repo of the run released (``status`` merged or
  not-needed, ``loop.view.RELEASED``): a person marking it, or the Loop once the pull request of a deliver's plan run
  merged on GitHub (``loop.github_merges``);
- it built or judged a change of the Curator the hub merged (``curator_changes.state`` merged);
- it made, or judged, a merge request the merge queue on GitLab merged (``merge_queue.merged_at``);
- it is a task run whose every pull request (``runs.pulls``) GitHub says merged, read with the workers' App and a token
  of pull_requests read alone (``pulls.SERVER_PERMISSIONS["pulls"]``), outside any transaction.

Only runs of the worker whose token asks, in a terminal state, and that no run still going on resumes, are answered;
any other id is left out, as is a run whose pull request GitHub cannot read now (the next turn asks again). The
answer is a hint: the daemon keeps a run it holds, a parked one, one another run adopted and a directory a later run
uses, whatever the hub says. A daemon of a release before this route never asks, and one that gets 404 from an older
hub keeps waiting out the 7 days.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import pulls
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.loop.view import RELEASED
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

MAX_ASKED = 200  # run ids in one question
PLAN_KINDS = ("plan", "step")
PULL_URL = re.compile(r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)$")

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


class MergedQuestion(BaseModel):
    runs: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        max_length=MAX_ASKED, description="ended runs of this worker whose directory and worktrees it still keeps"
    )


class MergedAnswer(BaseModel):
    merged: list[int] = Field(
        description="those of runs whose work the hub knows merged: the daemon may remove their worktrees now"
    )


def _released(body) -> dict[str, str]:
    entries = body.get("repos") if isinstance(body, dict) and isinstance(body.get("repos"), list) else []
    return {entry["repo"]: entry.get("status") for entry in entries if isinstance(entry, dict) and entry.get("repo")}


def _run_repos(row) -> set[str]:
    found = {entry["repo"] for entry in row.repos or [] if isinstance(entry, dict) and entry.get("repo")}
    return found or ({row.repo} if row.repo else set())


async def _ended(conn: AsyncConnection, worker_id: int, ids: list[int]) -> list:
    """The runs of ``ids`` worker ``worker_id`` ran that ended and that no run going on resumes."""
    r = tables.runs
    later = r.alias("later")
    going_on = (
        select(later.c.id)
        .where(later.c.resume_of_run_id == r.c.id, later.c.state.not_in(runs.TERMINAL_STATES))
        .exists()
    )
    query = select(r.c.id, r.c.project_id, r.c.kind, r.c.plan_id, r.c.repo, r.c.repos, r.c.pulls).where(
        r.c.id.in_(ids), r.c.worker_id == worker_id, r.c.state.in_(runs.TERMINAL_STATES), ~going_on
    )
    return list(await conn.execute(query))


async def _plans_released(conn: AsyncConnection, rows: list) -> set[int]:
    p = tables.plans
    wanted = {(row.project_id, row.plan_id) for row in rows if row.kind in PLAN_KINDS and row.plan_id}
    if not wanted:
        return set()
    query = select(p.c.project_id, p.c.plan_id, p.c.body).where(
        p.c.project_id.in_({project for project, _ in wanted}), p.c.plan_id.in_({plan for _, plan in wanted})
    )
    statuses = {(row.project_id, row.plan_id): _released(row.body) for row in await conn.execute(query)}
    found = set()
    for row in rows:
        repos, status = _run_repos(row), statuses.get((row.project_id, row.plan_id))
        if row.kind in PLAN_KINDS and status is not None and repos and all(status.get(n) in RELEASED for n in repos):
            found.add(row.id)
    return found


async def _merged_elsewhere(conn: AsyncConnection, ids: list[int]) -> set[int]:
    """The runs of ``ids`` that built or judged a change of the Curator the hub merged, or made or judged a merge
    request the queue merged."""
    c, q = tables.curator_changes, tables.merge_queue
    found: set[int] = set()
    for column in (c.c.builder_run_id, c.c.judge_run_id):
        found |= set((await conn.execute(select(column).where(column.in_(ids), c.c.state == "merged"))).scalars())
    for column in (q.c.run_id, q.c.judge_run_id):
        query = select(column).where(and_(column.in_(ids), q.c.merged_at.is_not(None)))
        found |= set((await conn.execute(query)).scalars())
    return found


async def _pull_merged(app: GitHubApp, url: str) -> bool | None:
    """Whether GitHub says the pull request at ``url`` merged; None when it cannot tell now."""
    match = PULL_URL.match(url or "")
    if match is None:
        return None
    owner, repo, number = match["owner"], match["repo"], int(match["number"])

    async def read(token: str) -> dict:
        return await pulls.pull(app, token, owner, repo, number)

    try:
        found = await pulls.with_token(app, owner, repo, "pulls", read)
    except (GitHubRefused, GitHubUnavailable) as exc:
        log.info("a task run's pull request could not be read", extra={"pull": url, "why": str(exc)})
        return None
    return found.get("merged") is True


async def _tasks_merged(app: GitHubApp | None, rows: list) -> set[int]:
    found = set()
    for row in rows:
        listed = [item for item in row.pulls or [] if isinstance(item, dict) and item.get("url")]
        if app is None or row.kind != "task" or not listed:
            continue
        if all([await _pull_merged(app, item["url"]) for item in listed]):
            found.add(row.id)
    return found


@worker_router.post("/merged", response_model=MergedAnswer, responses={403: {"model": ErrorBody}})
async def merged(request: Request, question: MergedQuestion, user: CurrentUser) -> MergedAnswer:
    """Of the ended runs the worker whose token asks still keeps, those whose work the hub knows merged."""
    w = tables.workers
    ids = sorted(set(question.runs))
    async with request.app.state.engine.connect() as conn:
        query = select(w.c.id, w.c.revoked_at).where(w.c.token_id == user.token_id)
        worker = (await conn.execute(query)).one_or_none()
        if worker is None or worker.revoked_at is not None:
            raise HTTPException(403, "this worker token belongs to no live worker: join the machine again")
        rows = await _ended(conn, worker.id, ids) if ids else []
        ended = [row.id for row in rows]
        found = await _plans_released(conn, rows) | (await _merged_elsewhere(conn, ended) if ended else set())
    left = [row for row in rows if row.id not in found]
    found |= await _tasks_merged(getattr(request.app.state, "github_app", None), left)
    if found:
        log.info("merged runs a worker keeps", extra={"worker_id": worker.id, "runs": sorted(found)})
    return MergedAnswer(merged=sorted(found))


__all__ = ["MAX_ASKED", "MergedAnswer", "MergedQuestion", "merged", "worker_router"]
