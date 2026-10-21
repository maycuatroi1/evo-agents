"""A fake GitLab for the hub tests: the API v4 of one host held in memory and served through ``httpx.MockTransport``, so
the real GitLab is never called (``evo_agents.hub.server.gitlab``).

A test adds projects (``add_project``: merge settings, branch heads, protected branches), their merge requests
(``add_merge_request``: head, labels, files, commits, whether GitLab would merge it, the head a rebase gives it, the
error a rebase of it ends with, how many reads a rebase of it lasts) and the pipelines of a commit with their jobs
(``add_pipeline``), and reads back every request (``requests``), the comments (``notes``), the jobs played with their
variables (``played``), the pipelines retried (``retried``), the merge requests rebased (``rebased``) and the reverts
made (``reverted``). A merge request remembers the head of its target branch it sits on (``base``): in a project that
merges fast-forward, one whose target branch moved since needs a rebase, as GitLab's detailed_merge_status says
(``need_rebase``), and GitLab will not merge it. ``label`` records
who added or removed a label of a merge request, and a project's ``members`` the role of each user. Every request needs
the PRIVATE-TOKEN header ``token`` names, or the answer is 401, as GitLab answers. ``queue`` scripts the answers of the
next requests, whatever they ask, one a request: a status with a body or headers, or an exception the transport raises
(a timeout).

It answers what the hub's client calls, as GitLab does: GET /projects/:id and .../protected_branches/:name; GET and POST
.../merge_requests, GET .../merge_requests/:iid with .../diffs, .../commits and .../resource_label_events (paged by
x-next-page), GET .../members/all/:user_id, PUT .../rebase (202: the head becomes ``rebase_to``, or a commit of its own
when the target branch moved, the merge request then sitting on the target branch's head; with ``rebase_error`` the head
stays and merge_error says why; ``rebase_reads`` reads of it answer rebase_in_progress first), PUT .../merge (409 for a
sha that is not the head, 405 when ``mergeable`` is false, 406 when it needs a rebase, else merged fast-forward: the
target branch's head becomes the merge request's), POST .../notes; GET .../pipelines (by sha and ref, the newest first),
.../pipelines/:id and .../pipelines/:id/jobs; POST .../pipelines/:id/retry (the pipeline runs again); POST
.../jobs/:id/play (a manual job only, else 400) and .../jobs/:id/retry; GET .../repository/branches/:name (404 for a
branch that is not there), POST .../repository/branches (400 for a branch that exists) and POST
.../repository/commits/:sha/revert (400 with the error_code ``revert_refusals`` names for that sha).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

HOST = "gitlab.example.org"
API = f"https://{HOST}/api/v4"
ROUTE = re.compile(r"^/api/v4/projects/(?P<project>[^/]+)(?P<rest>/.*)?$")


def sha_of(text: str) -> str:
    """A commit id made of ``text``, so a test names commits by what they are."""
    return hashlib.sha1(text.encode()).hexdigest()


@dataclass
class FakeMergeRequest:
    iid: int
    project_id: int
    source_branch: str
    target_branch: str
    sha: str
    title: str = "Add the lumen report"
    description: str = ""
    state: str = "opened"
    draft: bool = False
    labels: list[str] = field(default_factory=list)
    author: str = "northwind-dev"
    files: list[dict] = field(default_factory=list)  # as .../diffs lists them
    commits: list[dict] = field(default_factory=list)  # as .../commits lists them
    mergeable: bool = True  # False: PUT .../merge answers 405
    rebase_to: str | None = None  # the head PUT .../rebase gives it
    merge_error: str | None = None
    merge_commit_sha: str | None = None
    pipeline: dict | None = None  # its head pipeline
    label_events: list[dict] = field(default_factory=list)  # as .../resource_label_events lists them
    base: str | None = None  # the head of the target branch it sits on
    rebase_error: str | None = None  # a rebase of it fails with this merge_error
    rebase_reads: int = 0  # how many reads after a rebase still answer rebase_in_progress
    rebasing: int = 0  # reads left that answer rebase_in_progress

    def as_json(self, *, behind: bool = False) -> dict:
        return {
            "id": 9000 + self.iid,
            "iid": self.iid,
            "project_id": self.project_id,
            "title": self.title,
            "description": self.description,
            "state": self.state,
            "draft": self.draft,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "source_project_id": self.project_id,
            "target_project_id": self.project_id,
            "sha": self.sha,
            "merge_commit_sha": self.merge_commit_sha,
            "labels": list(self.labels),
            "author": {"id": 77, "username": self.author},
            "web_url": f"https://{HOST}/merge_requests/{self.iid}",
            "head_pipeline": self.pipeline,
            "detailed_merge_status": "not_approved" if not self.mergeable else "need_rebase" if behind else "mergeable",
            "rebase_in_progress": self.rebasing > 0,
            "merge_error": self.merge_error,
        }


@dataclass
class FakeProject:
    id: int
    path: str
    default_branch: str = "main"
    merge_method: str = "ff"
    only_allow_merge_if_pipeline_succeeds: bool = True
    allow_merge_on_skipped_pipeline: bool = False
    branches: dict[str, str] = field(default_factory=dict)  # name: head
    protected: dict[str, dict] = field(default_factory=dict)  # name: {"push": [levels], "merge": [levels]}
    merge_requests: dict[int, FakeMergeRequest] = field(default_factory=dict)
    pipelines: list[dict] = field(default_factory=list)
    jobs: dict[int, list[dict]] = field(default_factory=dict)  # pipeline id: its jobs
    revert_refusals: dict[str, str] = field(default_factory=dict)  # sha: the error_code a revert of it gets
    notes: list[tuple[int, str]] = field(default_factory=list)
    played: list[tuple[int, dict]] = field(default_factory=list)  # (job id, variables)
    reverted: list[tuple[str, str]] = field(default_factory=list)  # (sha, branch)
    retried: list[int] = field(default_factory=list)  # the pipelines retried
    rebased: list[int] = field(default_factory=list)  # the merge requests rebased, by iid
    members: dict[int, int] = field(default_factory=dict)  # user id: access level, inherited ones included

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "path_with_namespace": self.path,
            "default_branch": self.default_branch,
            "merge_method": self.merge_method,
            "only_allow_merge_if_pipeline_succeeds": self.only_allow_merge_if_pipeline_succeeds,
            "allow_merge_on_skipped_pipeline": self.allow_merge_on_skipped_pipeline,
        }


def _answer(status: int, body=None, headers: dict | None = None) -> httpx.Response:
    content = b"" if body is None else json.dumps(body).encode()
    kind = {"content-type": "application/json"} if body is not None else {}
    return httpx.Response(status, content=content, headers={**kind, **(headers or {})})


def _refused(status: int, message: str, **extra) -> httpx.Response:
    return _answer(status, {"message": message, **extra})


class FakeGitLab:
    """GitLab's API v4 on ``host``, for one token."""

    def __init__(self, token: str, host: str = HOST):
        self.token, self.host = token, host
        self.projects: dict[str, FakeProject] = {}
        self.requests: list[httpx.Request] = []
        self.queued: list[httpx.Response | Exception] = []
        self._ids = itertools.count(101)

    # Setting it up

    def add_project(self, path: str, **settings) -> FakeProject:
        project = FakeProject(next(self._ids), path, **settings)
        project.branches.setdefault(project.default_branch, sha_of(f"{path}@{project.default_branch}"))
        self.projects[path] = project
        return project

    def add_merge_request(self, project: FakeProject, source_branch: str, **fields) -> FakeMergeRequest:
        iid = max(project.merge_requests, default=0) + 1
        sha = fields.pop("sha", sha_of(f"{project.path}!{iid}"))
        target = fields.pop("target_branch", project.default_branch)
        fields.setdefault("base", project.branches.get(target))
        merge_request = FakeMergeRequest(iid, project.id, source_branch, target, sha, **fields)
        project.branches[source_branch] = sha
        project.merge_requests[iid] = merge_request
        return merge_request

    def add_pipeline(self, project: FakeProject, sha: str, ref: str, status: str = "success", jobs=()) -> dict:
        """A pipeline of ``sha`` on ``ref`` with ``jobs`` (name, status), each in stage test."""
        pipeline = {
            "id": next(self._ids),
            "sha": sha,
            "ref": ref,
            "status": status,
            "source": "push",
            "web_url": f"https://{self.host}/{project.path}/-/pipelines",
        }
        project.pipelines.append(pipeline)
        project.jobs[pipeline["id"]] = [self._job(pipeline, name, job_status) for name, job_status in jobs]
        return pipeline

    def _job(self, pipeline: dict, name: str, status: str) -> dict:
        return {
            "id": next(self._ids),
            "name": name,
            "stage": "test",
            "status": status,
            "allow_failure": False,
            "pipeline": {"id": pipeline["id"], "sha": pipeline["sha"], "ref": pipeline["ref"]},
            "web_url": f"https://{self.host}/jobs",
        }

    def queue(self, status: int | Exception, body=None, headers: dict | None = None) -> None:
        """The answer of the next request, whatever it asks: ``status`` with ``body``, or the exception raised."""
        self.queued.append(status if isinstance(status, Exception) else _answer(status, body, headers))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        """The method and raw path of every request, query left out."""
        return [f"{r.method} {r.url.raw_path.decode().partition('?')[0]}" for r in self.requests]

    # Answering

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.queued:
            answer = self.queued.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if request.headers.get("private-token") != self.token:
            return _refused(401, "401 Unauthorized")
        path = request.url.raw_path.decode().partition("?")[0]
        found = ROUTE.match(path)
        project = self.projects.get(unquote(found["project"])) if found else None
        if project is None:
            return _refused(404, "404 Project Not Found")
        query = {key: values[-1] for key, values in parse_qs(urlsplit(str(request.url)).query).items()}
        body = json.loads(request.content) if request.content else {}
        for pattern, method, answer in self._routes():
            matched = re.fullmatch(pattern, found["rest"] or "")
            if matched and request.method == method:
                return answer(project, query, body, *matched.groups())
        return _refused(404, "404 Not Found")

    def _routes(self):
        return (
            (r"", "GET", lambda p, q, b: _answer(200, p.as_json())),
            (r"/protected_branches/([^/]+)", "GET", self._protected),
            (r"/merge_requests", "GET", self._list_merge_requests),
            (r"/merge_requests", "POST", self._open_merge_request),
            (r"/merge_requests/(\d+)", "GET", self._merge_request),
            (r"/merge_requests/(\d+)/diffs", "GET", lambda p, q, b, iid: self._page(q, self._mr(p, iid).files)),
            (r"/merge_requests/(\d+)/commits", "GET", lambda p, q, b, iid: self._page(q, self._mr(p, iid).commits)),
            (r"/merge_requests/(\d+)/rebase", "PUT", self._rebase),
            (r"/merge_requests/(\d+)/merge", "PUT", self._merge),
            (r"/merge_requests/(\d+)/notes", "POST", self._note),
            (
                r"/merge_requests/(\d+)/resource_label_events",
                "GET",
                lambda p, q, b, iid: self._page(q, self._mr(p, iid).label_events),
            ),
            (r"/members/all/(\d+)", "GET", self._member),
            (r"/pipelines", "GET", self._pipelines),
            (r"/pipelines/(\d+)", "GET", self._pipeline),
            (r"/pipelines/(\d+)/jobs", "GET", lambda p, q, b, pid: self._page(q, p.jobs.get(int(pid), []))),
            (r"/pipelines/(\d+)/retry", "POST", self._retry_pipeline),
            (r"/jobs/(\d+)/play", "POST", self._play),
            (r"/jobs/(\d+)/retry", "POST", self._retry),
            (r"/repository/branches", "POST", self._branch),
            (r"/repository/branches/([^/]+)", "GET", self._branch_head),
            (r"/repository/commits/([0-9a-f]+)/revert", "POST", self._revert),
        )

    @staticmethod
    def _page(query: dict, items: list) -> httpx.Response:
        page, per_page = int(query.get("page", 1)), int(query.get("per_page", 20))
        start = (page - 1) * per_page
        more = start + per_page < len(items)
        return _answer(200, items[start : start + per_page], {"x-next-page": str(page + 1) if more else ""})

    @staticmethod
    def _mr(project: FakeProject, iid: str) -> FakeMergeRequest:
        return project.merge_requests[int(iid)]

    def _protected(self, project, query, body, name):
        rule = project.protected.get(unquote(name))
        if rule is None:
            return _refused(404, "404 Not found")
        levels = {key: [{"access_level": level} for level in rule[key]] for key in ("push", "merge")}
        body = {"name": unquote(name), "push_access_levels": levels["push"], "merge_access_levels": levels["merge"]}
        return _answer(200, body)

    @staticmethod
    def behind(project: FakeProject, mr: FakeMergeRequest) -> bool:
        """Whether ``mr`` needs a rebase before GitLab merges it: its project merges fast-forward and its target branch
        moved since the head it sits on."""
        head = project.branches.get(mr.target_branch)
        return project.merge_method == "ff" and mr.state == "opened" and mr.base is not None and mr.base != head

    def _list_merge_requests(self, project, query, body):
        wanted = set(filter(None, query.get("labels", "").split(",")))
        found = [
            mr.as_json(behind=self.behind(project, mr))
            for mr in project.merge_requests.values()
            if mr.state == query.get("state", mr.state)
            and mr.target_branch == query.get("target_branch", mr.target_branch)
            and wanted <= set(mr.labels)
        ]
        return self._page(query, found)

    def _merge_request(self, project, query, body, iid):
        if int(iid) not in project.merge_requests:
            return _refused(404, "404 Not found")
        mr = self._mr(project, iid)
        answer = mr.as_json(behind=self.behind(project, mr))
        mr.rebasing = max(mr.rebasing - 1, 0)
        return _answer(200, answer)

    def _open_merge_request(self, project, query, body):
        if body["source_branch"] not in project.branches:
            return _refused(400, "Source branch does not exist")
        labels = [label for label in body.get("labels", "").split(",") if label]
        fields = {"title": body["title"], "description": body.get("description", ""), "labels": labels}
        iid = max(project.merge_requests, default=0) + 1
        sha = project.branches[body["source_branch"]]
        fields["base"] = project.branches.get(body["target_branch"])
        mr = FakeMergeRequest(iid, project.id, body["source_branch"], body["target_branch"], sha, **fields)
        project.merge_requests[iid] = mr
        return _answer(201, mr.as_json())

    def _rebase(self, project, query, body, iid):
        mr = self._mr(project, iid)
        project.rebased.append(mr.iid)
        mr.rebasing = mr.rebase_reads
        if mr.rebase_error is not None:
            mr.merge_error = mr.rebase_error
            return _answer(202, {"rebase_in_progress": True})
        head = project.branches.get(mr.target_branch)
        new = mr.rebase_to or (sha_of(f"{mr.sha} onto {head}") if mr.base != head else mr.sha)
        mr.rebase_to, mr.base = None, head
        if new != mr.sha:
            mr.commits = [{**commit, "id": new} if commit.get("id") == mr.sha else commit for commit in mr.commits]
            mr.sha, mr.pipeline = new, None
            project.branches[mr.source_branch] = new
        return _answer(202, {"rebase_in_progress": True})

    def _merge(self, project, query, body, iid):
        mr = self._mr(project, iid)
        if body.get("sha") is not None and body["sha"] != mr.sha:
            return _refused(409, "SHA does not match HEAD of source branch")
        if not mr.mergeable or mr.state != "opened":
            return _refused(405, "405 Method Not Allowed")
        if self.behind(project, mr):
            return _refused(406, "Branch cannot be merged")
        mr.state = "merged"
        project.branches[mr.target_branch] = mr.sha  # fast-forward
        if body.get("should_remove_source_branch"):
            project.branches.pop(mr.source_branch, None)
        return _answer(200, mr.as_json())

    def _member(self, project, query, body, user_id):
        level = project.members.get(int(user_id))
        if level is None:
            return _refused(404, "404 Not found")
        return _answer(200, {"id": int(user_id), "username": f"user-{user_id}", "access_level": level})

    def label(self, merge_request: FakeMergeRequest, label: str, user_id: int, action: str = "add") -> None:
        """``user_id`` adds (or removes) ``label`` on ``merge_request``, as a resource label event says."""
        if action == "add" and label not in merge_request.labels:
            merge_request.labels.append(label)
        if action == "remove" and label in merge_request.labels:
            merge_request.labels.remove(label)
        merge_request.label_events.append(
            {
                "id": next(self._ids),
                "user": {"id": user_id, "username": f"user-{user_id}"},
                "label": {"id": 1, "name": label},
                "action": action,
            }
        )

    def _note(self, project, query, body, iid):
        project.notes.append((int(iid), body["body"]))
        return _answer(201, {"id": next(self._ids), "body": body["body"]})

    def _pipelines(self, project, query, body):
        found = [
            pipeline
            for pipeline in project.pipelines
            if pipeline["sha"] == query.get("sha", pipeline["sha"])
            and pipeline["ref"] == query.get("ref", pipeline["ref"])
        ]
        return _answer(200, sorted(found, key=lambda pipeline: -pipeline["id"]))

    def _pipeline(self, project, query, body, pipeline_id):
        found = [pipeline for pipeline in project.pipelines if pipeline["id"] == int(pipeline_id)]
        return _answer(200, found[0]) if found else _refused(404, "404 Not found")

    def _find_job(self, project, job_id) -> tuple[int, dict] | None:
        for pipeline_id, jobs in project.jobs.items():
            for job in jobs:
                if job["id"] == int(job_id):
                    return pipeline_id, job
        return None

    def _play(self, project, query, body, job_id):
        found = self._find_job(project, job_id)
        if found is None:
            return _refused(404, "404 Not found")
        _, job = found
        if job["status"] != "manual":
            return _refused(400, "Unplayable Job")
        variables = {item["key"]: item["value"] for item in body.get("job_variables_attributes", [])}
        project.played.append((job["id"], variables))
        job["status"] = "pending"
        return _answer(200, job)

    def _retry(self, project, query, body, job_id):
        found = self._find_job(project, job_id)
        if found is None:
            return _refused(404, "404 Not found")
        pipeline_id, job = found
        again = {**job, "id": next(self._ids), "status": "pending"}
        project.jobs[pipeline_id].append(again)
        return _answer(201, again)

    def _branch_head(self, project, query, body, name):
        head = project.branches.get(unquote(name))
        if head is None:
            return _refused(404, "404 Branch Not Found")
        return _answer(
            200, {"name": unquote(name), "commit": {"id": head}, "protected": unquote(name) in project.protected}
        )

    def _retry_pipeline(self, project, query, body, pipeline_id):
        found = [pipeline for pipeline in project.pipelines if pipeline["id"] == int(pipeline_id)]
        if not found:
            return _refused(404, "404 Not found")
        pipeline = found[0]
        project.retried.append(pipeline["id"])
        pipeline["status"] = "running"
        jobs = project.jobs.get(pipeline["id"], [])
        jobs += [{**job, "id": next(self._ids), "status": "pending"} for job in jobs if job["status"] == "failed"]
        return _answer(201, pipeline)

    def _branch(self, project, query, body):
        name, ref = body["branch"], body["ref"]
        if name in project.branches:
            return _refused(400, "Branch already exists")
        project.branches[name] = project.branches.get(ref, ref)
        return _answer(201, {"name": name, "commit": {"id": project.branches[name]}})

    def _revert(self, project, query, body, sha):
        code = project.revert_refusals.get(sha)
        if code is not None:
            return _refused(400, "Sorry, we cannot revert this commit automatically.", error_code=code)
        branch = body["branch"]
        parent = project.branches[branch]
        commit = {"id": sha_of(f"revert {sha} on {parent}"), "title": f"Revert {sha[:8]}", "parent_ids": [parent]}
        project.branches[branch] = commit["id"]
        project.reverted.append((sha, branch))
        return _answer(201, commit)
