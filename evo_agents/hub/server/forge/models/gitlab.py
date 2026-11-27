"""What the hub reads of GitLab and the errors its client raises (``evo_agents.hub.server.forge.client.gitlab``).

``project_of`` names where GitLab serves a repo's project: its REST API v4 at ``https://<host>[:port]/api/v4`` of the
origin's host, and the project's path. ``Fields`` reads one object of GitLab's JSON, a required field missing or of
another type being ``GitLabUnavailable``; each dataclass reads itself from it. GitLab not answering, answering 5xx or
429, redirecting, or answering JSON the hub cannot use is ``GitLabUnavailable``; any other 4xx is ``GitLabRefused``
with its status. Merging raises ``ShaMismatch`` (409) or ``NotMergeable`` (405, 406, 422), and a revert GitLab cannot
make ``RevertRefused``. Their messages are safe to show and to log.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from evo_agents.hub.credentials import normalize_origin

API_PATH = "/api/v4"


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


class Fields:
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

    def child(self, key: str) -> Fields | None:
        value = self.data.get(key)
        return Fields(value, self.doing) if isinstance(value, dict) else None


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
    def read(cls, f: Fields) -> ProjectSettings:
        return cls(
            id=f.number("id"),
            path=f.word("path_with_namespace"),
            default_branch=f.text("default_branch"),
            merge_method=f.text("merge_method"),
            only_allow_merge_if_pipeline_succeeds=f.flag("only_allow_merge_if_pipeline_succeeds"),
            allow_merge_on_skipped_pipeline=f.flag("allow_merge_on_skipped_pipeline"),
        )


def _levels(f: Fields, key: str) -> tuple[int, ...]:
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
    def read(cls, f: Fields) -> ProtectedBranch:
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
    def read(cls, f: Fields) -> Pipeline:
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
    def read(cls, f: Fields) -> Job:
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
    created_at: str | None = None  # ISO 8601, as GitLab writes it
    merged_at: str | None = None

    @classmethod
    def read(cls, f: Fields) -> MergeRequest:
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
            created_at=f.text("created_at"),
            merged_at=f.text("merged_at"),
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
    def read(cls, f: Fields) -> FileDiff:
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
    def read(cls, f: Fields) -> Commit:
        return cls(f.word("id"), f.text("title") or "", f.words("parent_ids"), f.text("message") or "")


@dataclass(frozen=True)
class LabelEvent:
    """Who added or removed a label of a merge request, as GET .../resource_label_events lists it."""

    user_id: int | None
    username: str | None
    label: str | None
    action: str  # add, remove

    @classmethod
    def read(cls, f: Fields) -> LabelEvent:
        user, label = f.child("user"), f.child("label")
        user_id = user.data.get("id") if user is not None else None
        return cls(
            user_id=user_id if type(user_id) is int else None,
            username=user.text("username") if user is not None else None,
            label=label.text("name") if label is not None else None,
            action=f.word("action"),
        )
