"""The hub's calls to GitLab, made with a project's forge secret (``evo_agents.hub.server.forge``): read a project's
merge settings and protected branches, the head of a branch, the merge base of two commits, a merge request, its diff
and commits, who labeled it and the role of a member, and the pipelines and jobs of a commit, open and rebase a merge
request, merge it with the sha the hub checked, comment on it, play or retry a job, retry a pipeline, and revert commits
onto a branch of their own with a merge request of them (plan gitlab-merge-queue, criteria a3 to a5).

GitLab's REST API v4 is at ``https://<host>[:port]/api/v4`` of the origin's host (``project_of``), and a project is
named by its path, URL-encoded (``group%2Frepo``). The token travels in the PRIVATE-TOKEN header of each request alone:
never in a URL, a log line, an error or a repr. A ``GitLab`` holds it for as long as its ``async with`` block, which
the hub opens right after it opened the secret and leaves once its calls are made.

Every call has a timeout (GITLAB_TIMEOUT_SECONDS), never follows a redirect, and makes one log line with the method,
the path and the status. GitLab not answering, answering 5xx or 429, redirecting, or answering JSON the hub cannot use
raises ``GitLabUnavailable``; any other 4xx raises ``GitLabRefused`` with its status. Merging raises ``ShaMismatch``
(409: the head moved since the hub checked it) or ``NotMergeable`` (405, 406, 422), and a revert GitLab cannot make
(nothing to revert, a conflict) ``RevertRefused`` with GitLab's error code. Their messages are safe to show and to log.
Lists are read page by page (PER_PAGE items each) up to MAX_PAGES pages; a longer list is ``GitLabUnavailable``,
never a part of it.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

from evo_agents import __version__
from evo_agents.hub.credentials import normalize_origin

log = logging.getLogger(__name__)

GITLAB_TIMEOUT_SECONDS = 30.0
API_PATH = "/api/v4"
PER_PAGE = 100  # the most GitLab gives in one page
MAX_PAGES = 100  # 10 000 files of a diff, jobs of a pipeline or merge requests of a list
MAX_MESSAGE_CHARS = 200  # of GitLab's own message, repeated in an error
TOKEN_HEADER = "PRIVATE-TOKEN"
MERGE_REFUSALS = (405, 406, 422)  # GitLab will not merge the merge request as it is
# What a GitLab message repeated in an error may hold: printable text on one line.
_SAFE_MESSAGE = re.compile(rf"[^\x00-\x1f\x7f]{{1,{MAX_MESSAGE_CHARS}}}")
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")


class GitLabUnavailable(Exception):
    """GitLab did not answer in time, failed, rate limited, redirected, or answered in a way the hub cannot use."""


class GitLabRefused(Exception):
    """GitLab turned a call down with a 4xx: the token lacks a right, the object is not there, or its state forbids
    the call. ``status`` is GitLab's answer and ``code`` its error_code when it gave a short one; the message is safe
    to show."""

    def __init__(self, message: str, status: int, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class ShaMismatch(GitLabRefused):
    """A merge GitLab refused because the merge request's head is no longer the sha the hub checked (409)."""


class NotMergeable(GitLabRefused):
    """A merge GitLab refused because the merge request cannot be merged as it is (405, 406 or 422)."""


class RevertRefused(GitLabRefused):
    """A revert GitLab could not make (400); ``code`` is GitLab's error_code, such as ``conflict``, or None."""


# What the hub reads of GitLab


@dataclass(frozen=True)
class GitLabProject:
    """Where GitLab serves a repo's project: its API's URL and the project's path in it."""

    api_url: str  # https://host[:port]/api/v4
    path: str  # group/subgroup/repo


def project_of(origin: str) -> GitLabProject | None:
    """The GitLab project of ``origin`` (https or SSH), or None when it names no host and namespaced path."""
    parts = urlsplit(normalize_origin(origin))
    path = parts.path.strip("/")
    if parts.scheme != "https" or not parts.hostname or "/" not in path:
        return None
    port = f":{parts.port}" if parts.port else ""
    return GitLabProject(f"https://{parts.hostname}{port}{API_PATH}", path)


class _Fields:
    """The fields of one object of GitLab's JSON; a required one missing or of another type is GitLabUnavailable."""

    def __init__(self, data, doing: str):
        if not isinstance(data, dict):
            raise GitLabUnavailable(f"GitLab answered without a JSON object while {doing}")
        self.data, self.doing = data, doing

    def _missing(self, key: str) -> GitLabUnavailable:
        return GitLabUnavailable(f"GitLab answered without a usable {key} while {self.doing}")

    def number(self, key: str) -> int:
        value = self.data.get(key)
        if type(value) is not int:
            raise self._missing(key)
        return value

    def word(self, key: str) -> str:
        value = self.data.get(key)
        if not isinstance(value, str):
            raise self._missing(key)
        return value

    def text(self, key: str) -> str | None:
        value = self.data.get(key)
        return value if isinstance(value, str) else None

    def flag(self, key: str) -> bool:
        return self.data.get(key) is True

    def words(self, key: str) -> tuple[str, ...]:
        value = self.data.get(key)
        return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()

    def child(self, key: str) -> _Fields | None:
        value = self.data.get(key)
        return _Fields(value, self.doing) if isinstance(value, dict) else None


@dataclass(frozen=True)
class ProjectSettings:
    """What gate 0 reads of a GitLab project: how it merges and whether a pipeline must pass first."""

    id: int
    path: str
    default_branch: str | None
    merge_method: str | None  # merge, rebase_merge or ff
    only_allow_merge_if_pipeline_succeeds: bool
    allow_merge_on_skipped_pipeline: bool

    @classmethod
    def read(cls, f: _Fields) -> ProjectSettings:
        return cls(
            id=f.number("id"),
            path=f.word("path_with_namespace"),
            default_branch=f.text("default_branch"),
            merge_method=f.text("merge_method"),
            only_allow_merge_if_pipeline_succeeds=f.flag("only_allow_merge_if_pipeline_succeeds"),
            allow_merge_on_skipped_pipeline=f.flag("allow_merge_on_skipped_pipeline"),
        )


def _levels(f: _Fields, key: str) -> tuple[int, ...]:
    value = f.data.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(
        item["access_level"] for item in value if isinstance(item, dict) and type(item.get("access_level")) is int
    )


@dataclass(frozen=True)
class ProtectedBranch:
    """Who may push to and merge into a protected branch, as GitLab's access levels (0 no one, 30 Developers, 40
    Maintainers)."""

    name: str
    push_access_levels: tuple[int, ...]
    merge_access_levels: tuple[int, ...]

    @classmethod
    def read(cls, f: _Fields) -> ProtectedBranch:
        return cls(f.word("name"), _levels(f, "push_access_levels"), _levels(f, "merge_access_levels"))


@dataclass(frozen=True)
class Pipeline:
    id: int
    sha: str
    ref: str | None
    status: str  # created, pending, running, success, failed, canceled, skipped, manual, scheduled ...
    source: str | None  # push, merge_request_event, web, api ...
    web_url: str | None

    @classmethod
    def read(cls, f: _Fields) -> Pipeline:
        return cls(f.number("id"), f.word("sha"), f.text("ref"), f.word("status"), f.text("source"), f.text("web_url"))


@dataclass(frozen=True)
class Job:
    id: int
    name: str
    stage: str | None
    status: str  # manual while it waits to be played; success, failed, running ...
    pipeline_id: int | None
    allow_failure: bool
    web_url: str | None

    @classmethod
    def read(cls, f: _Fields) -> Job:
        pipeline = f.child("pipeline")
        pipeline_id = pipeline.data.get("id") if pipeline is not None else None
        return cls(
            id=f.number("id"),
            name=f.word("name"),
            stage=f.text("stage"),
            status=f.word("status"),
            pipeline_id=pipeline_id if type(pipeline_id) is int else None,
            allow_failure=f.flag("allow_failure"),
            web_url=f.text("web_url"),
        )


@dataclass(frozen=True)
class MergeRequest:
    iid: int
    project_id: int
    title: str
    description: str
    state: str  # opened, closed, locked, merged
    draft: bool
    source_branch: str
    target_branch: str
    source_project_id: int | None
    target_project_id: int | None
    sha: str | None  # the head of its source branch
    merge_commit_sha: str | None
    labels: tuple[str, ...]
    author: str | None  # the author's username
    web_url: str | None
    head_pipeline: Pipeline | None
    detailed_merge_status: str | None
    rebase_in_progress: bool
    merge_error: str | None

    @classmethod
    def read(cls, f: _Fields) -> MergeRequest:
        author, pipeline = f.child("author"), f.child("head_pipeline")
        source, target = f.data.get("source_project_id"), f.data.get("target_project_id")
        return cls(
            iid=f.number("iid"),
            project_id=f.number("project_id"),
            title=f.word("title"),
            description=f.text("description") or "",
            state=f.word("state"),
            draft=f.flag("draft") or f.flag("work_in_progress"),
            source_branch=f.word("source_branch"),
            target_branch=f.word("target_branch"),
            source_project_id=source if type(source) is int else None,
            target_project_id=target if type(target) is int else None,
            sha=f.text("sha"),
            merge_commit_sha=f.text("merge_commit_sha"),
            labels=f.words("labels"),
            author=author.text("username") if author is not None else None,
            web_url=f.text("web_url"),
            head_pipeline=Pipeline.read(pipeline) if pipeline is not None else None,
            detailed_merge_status=f.text("detailed_merge_status"),
            rebase_in_progress=f.flag("rebase_in_progress"),
            merge_error=f.text("merge_error"),
        )


@dataclass(frozen=True)
class FileDiff:
    """One file a merge request changes, as GET .../merge_requests/:iid/diffs lists it."""

    old_path: str
    new_path: str
    diff: str
    new_file: bool
    renamed_file: bool
    deleted_file: bool
    cut: bool = False  # GitLab left the patch out (too_large or collapsed): what changed cannot be read

    @classmethod
    def read(cls, f: _Fields) -> FileDiff:
        return cls(
            old_path=f.word("old_path"),
            new_path=f.word("new_path"),
            diff=f.text("diff") or "",
            new_file=f.flag("new_file"),
            renamed_file=f.flag("renamed_file"),
            deleted_file=f.flag("deleted_file"),
            cut=f.flag("too_large") or f.flag("collapsed"),
        )


@dataclass(frozen=True)
class Commit:
    id: str
    title: str
    parent_ids: tuple[str, ...]
    message: str = ""  # the whole message, the title its first line

    @classmethod
    def read(cls, f: _Fields) -> Commit:
        return cls(f.word("id"), f.text("title") or "", f.words("parent_ids"), f.text("message") or "")


@dataclass(frozen=True)
class LabelEvent:
    """Who added or removed a label of a merge request, as GET .../resource_label_events lists it."""

    user_id: int | None
    username: str | None
    label: str | None
    action: str  # add, remove

    @classmethod
    def read(cls, f: _Fields) -> LabelEvent:
        user, label = f.child("user"), f.child("label")
        user_id = user.data.get("id") if user is not None else None
        return cls(
            user_id=user_id if type(user_id) is int else None,
            username=user.text("username") if user is not None else None,
            label=label.text("name") if label is not None else None,
            action=f.word("action"),
        )


# The client


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

    async def _object(self, method: str, path: str, doing: str, **kwargs) -> _Fields:
        response = await self._call(method, path, doing, **kwargs)
        return _Fields(self._json(response, doing), doing)

    async def _pages(self, path: str, doing: str, params: dict | None = None) -> list[_Fields]:
        """Every item of the list at ``path``, page by page as GitLab's x-next-page header leads."""
        items: list[_Fields] = []
        page = "1"
        for _ in range(MAX_PAGES):
            response = await self._call(
                "GET", path, doing, params={**(params or {}), "per_page": PER_PAGE, "page": page}
            )
            data = self._json(response, doing)
            if not isinstance(data, list):
                raise GitLabUnavailable(f"GitLab answered without a JSON list while {doing}")
            items.extend(_Fields(item, doing) for item in data)
            page = response.headers.get("x-next-page", "").strip()
            if not page:
                return items
        raise GitLabUnavailable(f"GitLab lists more than {MAX_PAGES * PER_PAGE} items while {doing}")

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
        return [Pipeline.read(_Fields(item, doing)) for item in data]

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
