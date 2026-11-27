"""Which ended runs of a worker had their work merged: POST /v1/worker/merged (plan super-board-parity-1, step 1).

A daemon keeps the directory and worktrees of each run it ran for 7 days after the run ended (``worker.daemon``,
``KEEP_WORKTREES``). On each turn of its cleanup it names the ended runs whose worktrees it still keeps, and the hub
answers with those whose work it knows merged; the daemon removes them in that turn instead of waiting out the 7 days.
A run counts as merged here when:

- it is a plan run or a run of one step whose plan marks every repo of the run released (``status`` merged or
  not-needed, ``loop.view.RELEASED``): a person marking it, or the Loop once the pull requests of a deliver's plan run
  merged (``loop.github_merges``);
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

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server.forge.client.github_app import GitHubApp
from evo_agents.hub.server.forge.models.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.loop.view import RELEASED
from evo_agents.hub.server.merge.models.merged_work import MergedAnswer
from evo_agents.hub.server.merge.repository import merged_work as repository
from evo_agents.hub.server.merge.service import pulls

log = logging.getLogger(__name__)

PLAN_KINDS = ("plan", "step")
PULL_URL = re.compile(r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)$")


def _released(body) -> dict[str, str]:
    entries = body.get("repos") if isinstance(body, dict) and isinstance(body.get("repos"), list) else []
    return {entry["repo"]: entry.get("status") for entry in entries if isinstance(entry, dict) and entry.get("repo")}  # pyright: ignore[reportOptionalIterable, reportReturnType]


def _run_repos(row) -> set[str]:
    found = {entry["repo"] for entry in row.repos or [] if isinstance(entry, dict) and entry.get("repo")}
    return found or ({row.repo} if row.repo else set())


async def _plans_released(conn: AsyncConnection, rows: list) -> set[int]:
    wanted = {(row.project_id, row.plan_id) for row in rows if row.kind in PLAN_KINDS and row.plan_id}
    if not wanted:
        return set()
    statuses = {
        (row.project_id, row.plan_id): _released(row.body) for row in await repository.plan_bodies(conn, wanted)
    }
    found = set()
    for row in rows:
        repos, status = _run_repos(row), statuses.get((row.project_id, row.plan_id))
        if row.kind in PLAN_KINDS and status is not None and repos and all(status.get(n) in RELEASED for n in repos):
            found.add(row.id)
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


async def merged(engine: AsyncEngine, app: GitHubApp | None, token_id: int, ids: list[int]) -> MergedAnswer:
    """Of the ended runs ``ids`` the worker of token ``token_id`` still keeps, those whose work the hub knows merged;
    GitHub is read with the workers' App ``app``."""
    async with engine.connect() as conn:
        worker = await repository.worker_of_token(conn, token_id)
        if worker is None or worker.revoked_at is not None:
            raise HTTPException(403, "this worker token belongs to no live worker: join the machine again")
        rows = await repository.ended(conn, worker.id, ids) if ids else []
        ended = [row.id for row in rows]
        found = await _plans_released(conn, rows) | (await repository.merged_elsewhere(conn, ended) if ended else set())
    left = [row for row in rows if row.id not in found]
    found |= await _tasks_merged(app, left)
    if found:
        log.info("merged runs a worker keeps", extra={"worker_id": worker.id, "runs": sorted(found)})
    return MergedAnswer(merged=sorted(found))
