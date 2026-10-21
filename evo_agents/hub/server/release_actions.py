"""What the hub reads of GitHub Actions for a release tag, and the one thing it does there, with the workers' App
(evo-agents-workers): the run of the schedule's workflow for the tag, its jobs and the logs of those that failed, and,
for a failure of the infrastructure alone, the failed jobs run again (``evo_agents.hub.release_ci`` describes what it
makes of them).

Each read takes one token of the App's installation on the repo with actions read (``pulls.SERVER_PERMISSIONS``, use
``ci``), the run again one with actions write (use ``rerun``); each is revoked after it. An installation that does not
grant actions yet is ``NoActions``, before any token is asked: its text starts with ``actions_permission`` and says
that a member of the account accepts the App's new permissions on the installation's page. The run of a tag is the
latest run of the workflow for the tag's commit pushed as that tag (GET .../actions/workflows/{workflow}/runs with
head_sha and event push, its head_branch the tag), or, once known, that run by its id (GET .../actions/runs/{id}),
which follows a run again; its jobs are those of its latest attempt (GET .../actions/runs/{id}/jobs, filter latest). A
job's log is the redirect GET .../actions/jobs/{id}/logs answers, read without the token and only its last
LOG_TAIL_BYTES kept (``GitHubClient.tail_of``); a log GitHub no longer keeps, or that cannot be read, is left out and
the job's name and failed step stand for it. POST .../actions/runs/{id}/rerun-failed-jobs runs the failed jobs again.

GitHub failing is ``GitHubUnavailable``, refusing a ``GitHubRefused``, as ``evo_agents.hub.server.pulls`` raises them.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import quote, urlsplit

from evo_agents.hub import release_ci, release_runs
from evo_agents.hub.releases import CONTROL
from evo_agents.hub.server import pulls
from evo_agents.hub.server.github import API_HEADERS, GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp, not_installed

log = logging.getLogger(__name__)

T = TypeVar("T")

RUNS_PAGE = 20  # runs of the workflow for one commit the hub lists
JOBS_PAGE = 100
MAX_JOB_PAGES = 3
INSTALLATIONS_PAGE = "https://github.com/settings/installations"


class NoActions(GitHubRefused):
    """The App's installation does not grant actions at the level a call needs."""


@dataclass(frozen=True)
class WorkflowRun:
    """The run of a tag's workflow, as the hub keeps it."""

    id: int
    status: str | None
    conclusion: str | None
    url: str | None
    attempt: int | None
    started_at: str | None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


def actions_problem(owner: str, repo: str, level: str) -> str:
    return (
        f"{release_ci.ACTIONS_PERMISSION}: the App's installation on {owner}/{repo} does not grant actions {level}, "
        f"so the hub cannot read the CI of a release tag or run its failed jobs again: a member of {owner} accepts "
        f"the App's new permissions on the installation's page ({INSTALLATIONS_PAGE})"
    )


async def with_actions(app: GitHubApp, owner: str, repo: str, use: str, call: Callable[[str], Awaitable[T]]) -> T:
    """``pulls.with_token`` for a use of SERVER_PERMISSIONS that needs actions: ``NoActions`` when the installation
    does not grant it at that level."""
    installation = await app.installation(owner, repo)
    if installation is None:
        raise GitHubRefused(not_installed(owner, repo))
    level = pulls.SERVER_PERMISSIONS[use]["actions"]
    if not installation.holds({"actions": level}):
        raise NoActions(actions_problem(owner, repo, level))
    return await pulls.with_token(app, owner, repo, use, call)


def _path(*parts: str | int) -> str:
    return "/".join(quote(str(part), safe="") for part in parts)


async def _call(app: GitHubApp, token: str, method: str, path: str, doing: str, **kwargs):
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
    return await app._call(method, f"{app.config.github_api_url}/{path}", doing, headers=headers, **kwargs)


def _refused(response, doing: str, owner: str, repo: str) -> GitHubRefused:
    if response.status_code == 403:
        return NoActions(actions_problem(owner, repo, "read"))
    return GitHubRefused(f"GitHub answered {response.status_code} while {doing}")


def _run(data: dict) -> WorkflowRun | None:
    run_id = data.get("id")
    if type(run_id) is not int or run_id <= 0:
        return None
    attempt = data.get("run_attempt")

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    url = text("html_url")
    return WorkflowRun(
        run_id,
        text("status"),
        text("conclusion"),
        url if url and len(url) <= release_runs.MAX_URL_CHARS and not CONTROL.search(url) else None,
        attempt if type(attempt) is int and attempt > 0 else None,
        text("run_started_at") or text("created_at"),
    )


async def tag_run(
    app: GitHubApp, token: str, owner: str, repo: str, *, workflow: str, tag: str, commit: str, run_id: int | None
) -> WorkflowRun | None:
    """The run of ``workflow`` for ``tag`` at ``commit`` (see the module), or None before there is one."""
    if run_id is not None:
        doing = f"reading run {run_id} of {workflow} on {owner}/{repo}"
        response = await _call(app, token, "GET", _path("repos", owner, repo, "actions", "runs", run_id), doing)
        if response.status_code == 200:
            return _run(app._json(response, doing))
        if response.status_code != 404:
            raise _refused(response, doing, owner, repo)
    doing = f"listing the runs of {workflow} for {tag} on {owner}/{repo}"
    path = _path("repos", owner, repo, "actions", "workflows", workflow, "runs")
    params = {"head_sha": commit, "event": "push", "per_page": RUNS_PAGE}
    response = await _call(app, token, "GET", path, doing, params=params)
    if response.status_code == 404:  # GitHub knows no such workflow yet
        return None
    if response.status_code != 200:
        raise _refused(response, doing, owner, repo)
    listed = app._json(response, doing).get("workflow_runs")
    items = [item for item in listed if isinstance(item, dict)] if isinstance(listed, list) else []
    ours = [found for item in items if item.get("head_branch") == tag and (found := _run(item)) is not None]
    return max(ours, key=lambda found: found.id, default=None)


async def run_jobs(app: GitHubApp, token: str, owner: str, repo: str, run_id: int) -> list[dict]:
    """The jobs of the latest attempt of run ``run_id``, as GitHub lists them."""
    doing = f"listing the jobs of run {run_id} on {owner}/{repo}"
    path = _path("repos", owner, repo, "actions", "runs", run_id, "jobs")
    found: list[dict] = []
    for page in range(1, MAX_JOB_PAGES + 1):
        params = {"filter": "latest", "per_page": JOBS_PAGE, "page": page}
        response = await _call(app, token, "GET", path, doing, params=params)
        if response.status_code != 200:
            raise _refused(response, doing, owner, repo)
        listed = app._json(response, doing).get("jobs")
        items = [item for item in listed if isinstance(item, dict)] if isinstance(listed, list) else []
        found += items
        if len(items) < JOBS_PAGE:
            break
    return found


def _trusted(app: GitHubApp, location: str) -> bool:
    """Whether a log's link may be followed: https, or the host of the API the hub is set to (a GitHub of tests)."""
    found, api = urlsplit(location), urlsplit(app.config.github_api_url)
    return found.scheme == "https" or (found.scheme, found.netloc) == (api.scheme, api.netloc)


async def job_log(app: GitHubApp, token: str, owner: str, repo: str, job_id: int) -> str | None:
    """The end of the log of job ``job_id``, or None when GitHub keeps none or it cannot be read."""
    doing = f"reading the log of job {job_id} on {owner}/{repo}"
    path = _path("repos", owner, repo, "actions", "jobs", job_id, "logs")
    response = await _call(app, token, "GET", path, doing, redirect=True)
    location = response.headers.get("location")
    if response.status_code == 403:
        raise _refused(response, doing, owner, repo)
    if not (300 <= response.status_code < 400) or not location or not _trusted(app, location):
        log.info("job log not read", extra={"job_id": job_id, "status": response.status_code})
        return None
    try:
        data = await app.tail_of(location, doing, keep=release_ci.LOG_TAIL_BYTES, most=release_ci.MAX_LOG_BYTES)
    except GitHubUnavailable as exc:
        log.info("job log not read", extra={"job_id": job_id, "why": str(exc)})
        return None
    return data.decode("utf-8", "replace")


async def failures(app: GitHubApp, token: str, owner: str, repo: str, jobs: list[dict]) -> dict:
    """``release_ci.summary`` of the failed jobs among ``jobs``, each with the end of its log."""
    found = []
    for job in release_ci.failed_jobs(jobs):
        job_id = job.get("id")
        text = await job_log(app, token, owner, repo, job_id) if type(job_id) is int else None
        found.append(release_ci.job_failure(job, text))
    return release_ci.summary(found)


async def rerun_failed(app: GitHubApp, owner: str, repo: str, run_id: int) -> None:
    """Run the failed jobs of run ``run_id`` again."""

    async def rerun(token: str) -> None:
        doing = f"running the failed jobs of run {run_id} on {owner}/{repo} again"
        path = _path("repos", owner, repo, "actions", "runs", run_id, "rerun-failed-jobs")
        response = await _call(app, token, "POST", path, doing, json={})
        if response.status_code not in (201, 204):
            raise _refused(response, doing, owner, repo)

    await with_actions(app, owner, repo, "rerun", rerun)
