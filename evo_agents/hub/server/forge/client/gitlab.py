"""The hub's calls to GitLab, made with a project's forge secret (``evo_agents.hub.server.forge.service.secrets``):
read a project's merge settings and protected branches, the head of a branch, the merge base of two commits, a merge
request, its diff and commits, who labeled it and the role of a member, and the pipelines and jobs of a commit, open and
rebase a merge request, merge it with the sha the hub checked, comment on it, play or retry a job, retry a pipeline, and
revert commits onto a branch of their own with a merge request of them (plan gitlab-merge-queue, criteria a3 to a5);
and list the merge requests updated since a time, which the Loop reads for what a plan run merged
(``loop.github_merges``).

GitLab's REST API v4 is at ``https://<host>[:port]/api/v4`` of the origin's host (``models.gitlab.project_of``), and a
project is named by its path, URL-encoded (``group%2Frepo``). The token travels in the PRIVATE-TOKEN header of each
request alone: never in a URL, a log line, an error or a repr. A ``GitLab`` holds it for as long as its ``async with``
block, which the hub opens right after it opened the secret and leaves once its calls are made.

Every call has a timeout (GITLAB_TIMEOUT_SECONDS), never follows a redirect, and makes one log line with the method,
the path and the status. GitLab not answering, answering 5xx or 429, redirecting, or answering JSON the hub cannot use
raises ``GitLabUnavailable``; any other 4xx raises ``GitLabRefused`` with its status. Merging raises ``ShaMismatch``
(409: the head moved since the hub checked it) or ``NotMergeable`` (405, 406, 422), and a revert GitLab cannot make
(nothing to revert, a conflict) ``RevertRefused`` with GitLab's error code. Their messages are safe to show and to log
(``evo_agents.hub.server.forge.models.gitlab`` holds them and the shapes read). Lists are read page by page (PER_PAGE
items each) up to MAX_PAGES pages; a longer list is ``GitLabUnavailable``, never a part of it.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from evo_agents import __version__
from evo_agents.hub.server.forge.models.gitlab import (
    Commit,
    Fields,
    FileDiff,
    GitLabRefused,
    GitLabUnavailable,
    Job,
    LabelEvent,
    MergeRequest,
    NotMergeable,
    Pipeline,
    ProjectSettings,
    ProtectedBranch,
    RevertRefused,
    ShaMismatch,
)

log = logging.getLogger(__name__)

GITLAB_TIMEOUT_SECONDS = 30.0
PER_PAGE = 100  # the most GitLab gives in one page
MAX_PAGES = 100  # 10 000 files of a diff, jobs of a pipeline or merge requests of a list
MAX_MESSAGE_CHARS = 200  # of GitLab's own message, repeated in an error
TOKEN_HEADER = "PRIVATE-TOKEN"
MERGE_REFUSALS = (405, 406, 422)  # GitLab will not merge the merge request as it is
# What a GitLab message repeated in an error may hold: printable text on one line.
_SAFE_MESSAGE = re.compile(rf"[^\x00-\x1f\x7f]{{1,{MAX_MESSAGE_CHARS}}}")
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")


def _said(response: httpx.Response) -> tuple[str | None, str | None]:
    """GitLab's own message and error_code of a refusal, each when it is short printable text."""
    try:
        data = response.json()
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    message, code = data.get("message", data.get("error")), data.get("error_code")
    return (
        message if isinstance(message, str) and _SAFE_MESSAGE.fullmatch(message) else None,
        code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else None,
    )


def _refusal(status: int, doing: str, message: str | None) -> str:
    said = f": {message}" if message else ""
    if status == 401:
        return (
            f"GitLab did not take the forge secret (401) while {doing}: the token expired or was revoked; an admin of "
            "the project sets the secret again"
        )
    if status == 403:
        return f"GitLab refused (403) while {doing}{said}: the forge secret's token needs scope api and Maintainer"
    if status == 404:
        return f"GitLab found nothing (404) while {doing}, or the forge secret's token cannot see it"
    return f"GitLab answered {status} while {doing}{said}"


class GitLab:
    """GitLab's API at ``api_url`` with one forge token, for the length of an ``async with`` block."""

    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = GITLAB_TIMEOUT_SECONDS,
    ):
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            headers={"User-Agent": f"evo-agents-hub/{__version__}", "Accept": "application/json"},
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"GitLab(api_url={self.api_url!r})"

    async def __aenter__(self) -> GitLab:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._token = ""
        await self._http.aclose()

    async def _call(self, method: str, path: str, doing: str, *, ok=(200,), **kwargs) -> httpx.Response:
        """``method`` on ``path`` of the API; the answer when its status is one of ``ok``."""
        url = f"{self.api_url}{path}"
        logged = urlsplit(url).path
        started = time.perf_counter()
        try:
            response = await self._http.request(method, url, headers={TOKEN_HEADER: self._token}, **kwargs)
        except httpx.TimeoutException:
            log.warning("gitlab call timed out", extra={"method": method, "path": logged})
            raise GitLabUnavailable(f"GitLab did not answer within {self._timeout:g}s while {doing}") from None
        except httpx.HTTPError as exc:
            log.warning("gitlab call failed", extra={"method": method, "path": logged, "error": type(exc).__name__})
            raise GitLabUnavailable(f"cannot reach GitLab while {doing} ({type(exc).__name__})") from None
        status = response.status_code
        duration = round((time.perf_counter() - started) * 1000, 1)
        log.info("gitlab call", extra={"method": method, "path": logged, "status": status, "duration_ms": duration})
        if status in ok:
            return response
        if status >= 500 or status == 429:
            raise GitLabUnavailable(f"GitLab answered {status} while {doing}; the hub tries again later")
        if 300 <= status < 400:
            raise GitLabUnavailable(f"GitLab answered {status} with a redirect while {doing}: check the origin's host")
        if 400 <= status < 500:
            message, code = _said(response)
            raise GitLabRefused(_refusal(status, doing, message), status, code)
        raise GitLabUnavailable(f"GitLab answered {status} while {doing}")

    @staticmethod
    def _json(response: httpx.Response, doing: str):
        try:
            return response.json()
        except ValueError:
            raise GitLabUnavailable(f"GitLab answered {response.status_code} without JSON while {doing}") from None

    async def _object(self, method: str, path: str, doing: str, **kwargs) -> Fields:
        response = await self._call(method, path, doing, **kwargs)
        return Fields(self._json(response, doing), doing)

    async def _pages(
        self, path: str, doing: str, params: dict | None = None, *, pages: int | None = None
    ) -> list[Fields]:
        """Every item of the list at ``path``, page by page as GitLab's x-next-page header leads, over at most
        ``pages`` pages (MAX_PAGES when None)."""
        pages = MAX_PAGES if pages is None else pages
        items: list[Fields] = []
        page = "1"
        for _ in range(pages):
            response = await self._call(
                "GET", path, doing, params={**(params or {}), "per_page": PER_PAGE, "page": page}
            )
            data = self._json(response, doing)
            if not isinstance(data, list):
                raise GitLabUnavailable(f"GitLab answered without a JSON list while {doing}")
            items.extend(Fields(item, doing) for item in data)
            page = response.headers.get("x-next-page", "").strip()
            if not page:
                return items
        raise GitLabUnavailable(f"GitLab lists more than {pages * PER_PAGE} items while {doing}")

    @staticmethod
    def _project(project: str) -> str:
        return f"/projects/{quote(project, safe='')}"

    # Reading

    async def project(self, project: str) -> ProjectSettings:
        """How ``project`` merges (GET /projects/:id)."""
        return ProjectSettings.read(await self._object("GET", self._project(project), f"reading project {project}"))

    async def protected_branch(self, project: str, branch: str) -> ProtectedBranch | None:
        """Who may push to and merge into ``branch``; None when it is not protected."""
        doing = f"reading the protection of branch {branch} of {project}"
        path = f"{self._project(project)}/protected_branches/{quote(branch, safe='')}"
        try:
            return ProtectedBranch.read(await self._object("GET", path, doing))
        except GitLabRefused as exc:
            if exc.status == 404:
                return None
            raise

    async def merge_request(self, project: str, iid: int) -> MergeRequest:
        """Merge request ``iid`` with its head, head pipeline and whether a rebase of it is running."""
        doing = f"reading merge request !{iid} of {project}"
        path = f"{self._project(project)}/merge_requests/{iid}"
        return MergeRequest.read(await self._object("GET", path, doing, params={"include_rebase_in_progress": "true"}))

    async def merge_requests(
        self, project: str, *, target_branch: str | None = None, labels: tuple[str, ...] = (), state: str = "opened"
    ) -> list[MergeRequest]:
        """The merge requests of ``project`` in ``state``, into ``target_branch`` and with every one of ``labels``."""
        params = {"state": state, "order_by": "created_at", "sort": "asc"}
        if target_branch is not None:
            params["target_branch"] = target_branch
        if labels:
            params["labels"] = ",".join(labels)
        found = await self._pages(
            f"{self._project(project)}/merge_requests", f"listing merge requests of {project}", params
        )
        return [MergeRequest.read(item) for item in found]

    async def updated_merge_requests(self, project: str, since: datetime, *, pages: int) -> list[MergeRequest]:
        """The merge requests of ``project`` in any state updated at or after ``since``, the newest update first; a
        list longer than ``pages`` pages is GitLabUnavailable."""
        after = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"state": "all", "updated_after": after, "order_by": "updated_at", "sort": "desc"}
        doing = f"listing the merge requests of {project} updated since {after}"
        found = await self._pages(f"{self._project(project)}/merge_requests", doing, params, pages=pages)
        return [MergeRequest.read(item) for item in found]

    async def merge_request_diffs(self, project: str, iid: int) -> list[FileDiff]:
        """Every file merge request ``iid`` changes, with its patch."""
        path = f"{self._project(project)}/merge_requests/{iid}/diffs"
        return [FileDiff.read(item) for item in await self._pages(path, f"reading the diff of !{iid} of {project}")]

    async def label_events(self, project: str, iid: int) -> list[LabelEvent]:
        """Who added and removed each label of merge request ``iid``, the oldest first."""
        path = f"{self._project(project)}/merge_requests/{iid}/resource_label_events"
        items = await self._pages(path, f"reading the label events of !{iid} of {project}")
        return [LabelEvent.read(item) for item in items]

    async def access_level(self, project: str, user_id: int) -> int | None:
        """The access level user ``user_id`` holds on ``project``, inherited ones included (10 Guest, 20 Reporter, 30
        Developer, 40 Maintainer, 50 Owner); None when the user is not a member."""
        doing = f"reading the role of user {user_id} on {project}"
        path = f"{self._project(project)}/members/all/{int(user_id)}"
        try:
            return (await self._object("GET", path, doing)).number("access_level")
        except GitLabRefused as exc:
            if exc.status == 404:
                return None
            raise

    async def merge_request_commits(self, project: str, iid: int) -> list[Commit]:
        """The commits of merge request ``iid``, in the order GitLab lists them."""
        path = f"{self._project(project)}/merge_requests/{iid}/commits"
        return [Commit.read(item) for item in await self._pages(path, f"reading the commits of !{iid} of {project}")]

    async def pipelines(self, project: str, sha: str, *, ref: str | None = None) -> list[Pipeline]:
        """The pipelines of commit ``sha`` (on ``ref`` alone when given), the newest first."""
        params = {"sha": sha, "order_by": "id", "sort": "desc"}
        if ref is not None:
            params["ref"] = ref
        doing = f"listing the pipelines of {sha[:12]} of {project}"
        response = await self._call(
            "GET", f"{self._project(project)}/pipelines", doing, params={**params, "per_page": PER_PAGE}
        )
        data = self._json(response, doing)
        if not isinstance(data, list):
            raise GitLabUnavailable(f"GitLab answered without a JSON list while {doing}")
        return [Pipeline.read(Fields(item, doing)) for item in data]

    async def pipeline(self, project: str, pipeline_id: int) -> Pipeline:
        doing = f"reading pipeline {pipeline_id} of {project}"
        return Pipeline.read(await self._object("GET", f"{self._project(project)}/pipelines/{pipeline_id}", doing))

    async def jobs(self, project: str, pipeline_id: int) -> list[Job]:
        """The jobs of pipeline ``pipeline_id``, the last try of each."""
        path = f"{self._project(project)}/pipelines/{pipeline_id}/jobs"
        return [Job.read(item) for item in await self._pages(path, f"listing the jobs of pipeline {pipeline_id}")]

    async def branch_head(self, project: str, branch: str) -> str | None:
        """The commit ``branch`` of ``project`` is at (GET .../repository/branches/:branch); None when it has none."""
        doing = f"reading branch {branch} of {project}"
        path = f"{self._project(project)}/repository/branches/{quote(branch, safe='')}"
        try:
            found = await self._object("GET", path, doing)
        except GitLabRefused as exc:
            if exc.status == 404:
                return None
            raise
        commit = found.child("commit")
        if commit is None:
            raise GitLabUnavailable(f"GitLab answered without a usable commit while {doing}")
        return commit.word("id")

    async def merge_base(self, project: str, first: str, second: str) -> str | None:
        """The newest commit both ``first`` and ``second`` of ``project`` stem from (GET .../repository/merge_base):
        ``second`` itself when ``first`` already holds it. None when GitLab does not know one of them, or they share
        none."""
        doing = f"reading the merge base of {first[:12]} and {second[:12]} of {project}"
        path = f"{self._project(project)}/repository/merge_base"
        try:
            found = await self._object("GET", path, doing, params=[("refs[]", first), ("refs[]", second)])
        except GitLabRefused as exc:
            if exc.status in (400, 404):
                return None
            raise
        return found.word("id")

    # Acting

    async def rebase(self, project: str, iid: int) -> None:
        """Ask GitLab to rebase merge request ``iid`` onto its target branch; it does so in the background, and
        ``merge_request`` says when it is done (``rebase_in_progress``) and whether it failed (``merge_error``)."""
        path = f"{self._project(project)}/merge_requests/{iid}/rebase"
        await self._call("PUT", path, f"rebasing !{iid} of {project}", ok=(202,))

    async def merge(self, project: str, iid: int, sha: str, *, remove_source_branch: bool = True) -> MergeRequest:
        """Merge ``iid`` if its head is still ``sha``, without squashing, so the commit merged is the one checked."""
        doing = f"merging !{iid} of {project} at {sha[:12]}"
        body = {"sha": sha, "squash": False, "should_remove_source_branch": remove_source_branch}
        path = f"{self._project(project)}/merge_requests/{iid}/merge"
        try:
            return MergeRequest.read(await self._object("PUT", path, doing, json=body))
        except GitLabRefused as exc:
            if exc.status == 409:
                raise ShaMismatch(f"the head of !{iid} of {project} is no longer {sha[:12]} (409)", 409) from None
            if exc.status in MERGE_REFUSALS:
                raise NotMergeable(str(exc), exc.status, exc.code) from None
            raise

    async def comment(self, project: str, iid: int, body: str) -> int:
        """Comment ``body`` on merge request ``iid``; the note's id."""
        path = f"{self._project(project)}/merge_requests/{iid}/notes"
        note = await self._object("POST", path, f"commenting on !{iid} of {project}", ok=(201,), json={"body": body})
        return note.number("id")

    async def play_job(self, project: str, job_id: int, variables: dict[str, str] | None = None) -> Job:
        """Run manual job ``job_id``, with ``variables`` set for it."""
        body = {"job_variables_attributes": [{"key": key, "value": value} for key, value in (variables or {}).items()]}
        path = f"{self._project(project)}/jobs/{job_id}/play"
        return Job.read(await self._object("POST", path, f"playing job {job_id} of {project}", json=body))

    async def retry_job(self, project: str, job_id: int) -> Job:
        """Run job ``job_id`` again; the new job."""
        path = f"{self._project(project)}/jobs/{job_id}/retry"
        return Job.read(await self._object("POST", path, f"retrying job {job_id} of {project}", ok=(200, 201)))

    async def retry_pipeline(self, project: str, pipeline_id: int) -> Pipeline:
        """Run the failed and cancelled jobs of pipeline ``pipeline_id`` again; the pipeline."""
        path = f"{self._project(project)}/pipelines/{pipeline_id}/retry"
        doing = f"retrying pipeline {pipeline_id} of {project}"
        return Pipeline.read(await self._object("POST", path, doing, ok=(200, 201)))

    async def create_branch(self, project: str, branch: str, ref: str) -> None:
        """Branch ``branch`` of ``project`` at ``ref``."""
        path = f"{self._project(project)}/repository/branches"
        doing = f"creating branch {branch} of {project}"
        await self._call("POST", path, doing, ok=(201,), json={"branch": branch, "ref": ref})

    async def revert_commit(self, project: str, sha: str, branch: str) -> Commit:
        """A commit on ``branch`` that reverts ``sha``; RevertRefused when GitLab cannot make it."""
        doing = f"reverting {sha[:12]} onto {branch} of {project}"
        path = f"{self._project(project)}/repository/commits/{quote(sha, safe='')}/revert"
        try:
            return Commit.read(await self._object("POST", path, doing, ok=(201,), json={"branch": branch}))
        except GitLabRefused as exc:
            if exc.status != 400:
                raise
            raise RevertRefused(str(exc), exc.status, exc.code) from None

    async def create_merge_request(
        self,
        project: str,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: tuple[str, ...] = (),
        remove_source_branch: bool = True,
    ) -> MergeRequest:
        body = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": remove_source_branch,
        }
        if labels:
            body["labels"] = ",".join(labels)
        doing = f"opening a merge request of {source_branch} into {target_branch} of {project}"
        return MergeRequest.read(
            await self._object("POST", f"{self._project(project)}/merge_requests", doing, ok=(201,), json=body)
        )

    async def open_revert(
        self,
        project: str,
        *,
        shas: list[str],
        target_branch: str,
        branch: str,
        title: str,
        description: str,
        labels: tuple[str, ...] = (),
    ) -> MergeRequest:
        """A merge request into ``target_branch`` that reverts ``shas`` one commit each, in the order given (the newest
        first), on ``branch`` made from ``target_branch``."""
        await self.create_branch(project, branch, target_branch)
        for sha in shas:
            await self.revert_commit(project, sha, branch)
        return await self.create_merge_request(
            project,
            source_branch=branch,
            target_branch=target_branch,
            title=title,
            description=description,
            labels=labels,
        )
