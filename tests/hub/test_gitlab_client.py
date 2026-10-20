"""The hub's GitLab client (``evo_agents.hub.server.gitlab``) against the fake GitLab of ``tests.hub.fake_gitlab``.

The checks step 3 of plan gitlab-merge-queue names: the client reads a merge request, its diff and the pipelines of a
commit, rebases a merge request, merges it with the sha the hub checked, plays a manual job, and makes revert commits
with a merge request of them. Around them: the token travels in the PRIVATE-TOKEN header alone and reaches no URL, log
record, error or repr; lists are read whole, page by page, never cut; GitLab failing, rate limiting, redirecting or
not answering is ``GitLabUnavailable``, and a refusal says why. No Postgres is needed, and the real GitLab is never
called.
"""

import asyncio
import json
import logging
import re
import secrets

import pytest

pytest.importorskip("httpx")

import httpx

from evo_agents.hub.server import gitlab
from evo_agents.hub.server.gitlab import (
    GitLab,
    GitLabProject,
    GitLabRefused,
    GitLabUnavailable,
    NotMergeable,
    RevertRefused,
    ShaMismatch,
    project_of,
)
from tests.hub.fake_gitlab import API, HOST, FakeGitLab, sha_of

PATH = "northwind/lumen-app"
ENCODED = "northwind%2Flumen-app"


def sample(prefix: str = "glpat-") -> str:
    """A token drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


@pytest.fixture
def fake() -> FakeGitLab:
    return FakeGitLab(sample())


def calls(fake: FakeGitLab, work, **client):
    """The result of ``work(gl)`` with a client of the fake's API holding its token."""

    async def main():
        async with GitLab(API, client.pop("token", fake.token), transport=fake.transport(), **client) as gl:
            return await work(gl)

    return asyncio.run(main())


def merge_request_of(fake: FakeGitLab, **fields):
    project = fake.add_project(PATH)
    return project, fake.add_merge_request(project, "plan/lumen-report", **fields)


# Where GitLab is


@pytest.mark.parametrize(
    "origin, expected",
    [
        ("https://gitlab.example.org/northwind/lumen-app.git", GitLabProject(API, PATH)),
        ("git@gitlab.example.org:northwind/lumen-app.git", GitLabProject(API, PATH)),
        (
            "ssh://git@gitlab.example.org:2222/northwind/infra/lumen-app",
            GitLabProject(API, "northwind/infra/lumen-app"),
        ),
        ("https://GitLab.Example.org:8443/northwind/lumen-app/", GitLabProject(f"https://{HOST}:8443/api/v4", PATH)),
        ("https://gitlab.example.org/lumen-app", None),  # no namespace: not a project
        ("/srv/git/lumen-app", None),
    ],
)
def test_project_of_maps_an_origin_onto_its_api_and_project_path(origin, expected):
    assert project_of(origin) == expected


# Reading


def test_a_merge_request_is_read_with_its_head_pipeline_and_the_token_in_its_header_alone(fake):
    project, mr = merge_request_of(fake, labels=["evo-merge"], description="Adds the report.")
    mr.pipeline = fake.add_pipeline(project, mr.sha, mr.source_branch)
    found = calls(fake, lambda gl: gl.merge_request(PATH, mr.iid))
    assert (found.iid, found.sha, found.source_branch, found.target_branch) == (1, mr.sha, "plan/lumen-report", "main")
    assert (found.state, found.draft, found.labels, found.author) == ("opened", False, ("evo-merge",), "northwind-dev")
    assert found.description == "Adds the report." and found.rebase_in_progress is False
    assert found.head_pipeline is not None
    assert (found.head_pipeline.sha, found.head_pipeline.status) == (mr.sha, "success")
    (request,) = fake.requests
    assert (
        request.url.raw_path.decode() == f"/api/v4/projects/{ENCODED}/merge_requests/1?include_rebase_in_progress=true"
    )
    assert request.headers["private-token"] == fake.token
    assert fake.token not in str(request.url) and "authorization" not in request.headers


def test_the_diff_and_the_commits_are_read_whole_page_by_page(fake):
    files = [
        {"old_path": f"f{n}.py", "new_path": f"f{n}.py", "diff": f"@@ -1 +1 @@\n-{n}\n+{n + 1}\n"} for n in range(250)
    ]
    commits = [{"id": sha_of(f"c{n}"), "title": f"Step {n}", "parent_ids": []} for n in range(3)]
    _, mr = merge_request_of(fake, files=files, commits=commits)
    diffs = calls(fake, lambda gl: gl.merge_request_diffs(PATH, mr.iid))
    assert [d.new_path for d in diffs] == [f"f{n}.py" for n in range(250)]
    assert diffs[7].diff == "@@ -1 +1 @@\n-7\n+8\n" and not diffs[7].new_file
    assert [p for p in fake.paths() if p.endswith("/diffs")] == [
        f"GET /api/v4/projects/{ENCODED}/merge_requests/1/diffs"
    ] * 3
    found = calls(fake, lambda gl: gl.merge_request_commits(PATH, mr.iid))
    assert [(c.id, c.title) for c in found] == [(c["id"], c["title"]) for c in commits]


def test_a_list_longer_than_the_pages_the_client_reads_is_unavailable_never_cut(fake, monkeypatch):
    monkeypatch.setattr(gitlab, "MAX_PAGES", 2)
    files = [{"old_path": f"f{n}", "new_path": f"f{n}", "diff": ""} for n in range(201)]
    _, mr = merge_request_of(fake, files=files)
    with pytest.raises(GitLabUnavailable, match="more than 200 items while reading the diff of !1"):
        calls(fake, lambda gl: gl.merge_request_diffs(PATH, mr.iid))


def test_the_pipelines_of_a_commit_come_newest_first_with_their_jobs(fake):
    project, mr = merge_request_of(fake)
    head = project.branches["main"]
    first = fake.add_pipeline(project, head, "main", status="failed")
    second = fake.add_pipeline(project, head, "main", jobs=[("uat_check", "success"), ("prod_deploy", "manual")])
    fake.add_pipeline(project, mr.sha, mr.source_branch)  # another commit's

    found = calls(fake, lambda gl: gl.pipelines(PATH, head, ref="main"))
    assert [(p.id, p.status) for p in found] == [(second["id"], "success"), (first["id"], "failed")]
    jobs = calls(fake, lambda gl: gl.jobs(PATH, second["id"]))
    assert [(j.name, j.status, j.pipeline_id) for j in jobs] == [
        ("uat_check", "success", second["id"]),
        ("prod_deploy", "manual", second["id"]),
    ]
    again = calls(fake, lambda gl: gl.pipeline(PATH, first["id"]))
    assert (again.id, again.sha, again.ref, again.status) == (first["id"], head, "main", "failed")


def test_gate_zero_reads_how_the_project_merges_and_who_may_push_to_its_target_branch(fake):
    fake.add_project(PATH, protected={"main": {"push": [0], "merge": [40]}})
    settings = calls(fake, lambda gl: gl.project(PATH))
    assert (settings.path, settings.default_branch, settings.merge_method) == (PATH, "main", "ff")
    assert settings.only_allow_merge_if_pipeline_succeeds and not settings.allow_merge_on_skipped_pipeline
    protection = calls(fake, lambda gl: gl.protected_branch(PATH, "main"))
    assert (protection.push_access_levels, protection.merge_access_levels) == ((0,), (40,))
    assert calls(fake, lambda gl: gl.protected_branch(PATH, "release/1.0")) is None  # not protected
    assert fake.paths()[-1] == f"GET /api/v4/projects/{ENCODED}/protected_branches/release%2F1.0"


def test_merge_requests_are_listed_by_target_branch_and_labels(fake):
    project = fake.add_project(PATH)
    fake.add_merge_request(project, "a", labels=["evo-merge"])
    fake.add_merge_request(project, "b", labels=["evo-merge", "lumen"])
    fake.add_merge_request(project, "c")
    fake.add_merge_request(project, "d", labels=["evo-merge"], target_branch="release")
    found = calls(fake, lambda gl: gl.merge_requests(PATH, target_branch="main", labels=("evo-merge",)))
    assert [mr.source_branch for mr in found] == ["a", "b"]


# Acting


def test_a_rebase_then_a_merge_with_the_sha_checked_fast_forwards_the_target_branch(fake):
    project, mr = merge_request_of(fake, rebase_to=sha_of("rebased"))

    async def work(gl):
        await gl.rebase(PATH, mr.iid)
        rebased = await gl.merge_request(PATH, mr.iid)
        return rebased, await gl.merge(PATH, mr.iid, rebased.sha)

    rebased, merged = calls(fake, work)
    assert rebased.sha == sha_of("rebased") and not rebased.rebase_in_progress
    assert merged.state == "merged" and project.branches["main"] == sha_of("rebased")
    assert "plan/lumen-report" not in project.branches  # the source branch went with the merge
    merge = fake.requests[-1]
    assert (merge.method, merge.url.raw_path.decode()) == ("PUT", f"/api/v4/projects/{ENCODED}/merge_requests/1/merge")
    assert json.loads(merge.content) == {
        "sha": sha_of("rebased"),
        "squash": False,
        "should_remove_source_branch": True,
    }


def test_a_merge_whose_head_moved_is_a_sha_mismatch_and_one_gitlab_will_not_merge_is_not_mergeable(fake):
    project, mr = merge_request_of(fake)
    with pytest.raises(
        ShaMismatch, match=r"the head of !1 of northwind/lumen-app is no longer \w{12} \(409\)"
    ) as moved:
        calls(fake, lambda gl: gl.merge(PATH, mr.iid, sha_of("checked before a push")))
    assert moved.value.status == 409 and isinstance(moved.value, GitLabRefused)
    mr.mergeable = False
    with pytest.raises(NotMergeable, match="GitLab answered 405 while merging !1") as refused:
        calls(fake, lambda gl: gl.merge(PATH, mr.iid, mr.sha))
    assert refused.value.status == 405
    assert mr.state == "opened" and project.branches["main"] != mr.sha


def test_a_comment_is_posted_on_the_merge_request(fake):
    project, mr = merge_request_of(fake)
    note = calls(fake, lambda gl: gl.comment(PATH, mr.iid, "Blocked: the pipeline of the head failed."))
    assert isinstance(note, int) and project.notes == [(1, "Blocked: the pipeline of the head failed.")]


def test_a_manual_job_is_played_with_its_variables_and_a_failed_job_retried(fake):
    project, _ = merge_request_of(fake)
    pipeline = fake.add_pipeline(
        project, project.branches["main"], "main", jobs=[("rollback", "manual"), ("uat_check", "failed")]
    )
    rollback, check = project.jobs[pipeline["id"]]
    played = calls(fake, lambda gl: gl.play_job(PATH, rollback["id"], {"EVO_ROLLBACK_TO_SHA": sha_of("good")}))
    assert (played.id, played.name, played.status) == (rollback["id"], "rollback", "pending")
    assert project.played == [(rollback["id"], {"EVO_ROLLBACK_TO_SHA": sha_of("good")})]
    with pytest.raises(GitLabRefused, match="GitLab answered 400 while playing job .*: Unplayable Job"):
        calls(fake, lambda gl: gl.play_job(PATH, check["id"]))
    again = calls(fake, lambda gl: gl.retry_job(PATH, check["id"]))
    assert again.name == "uat_check" and again.id != check["id"] and again.status == "pending"


def test_a_revert_merge_request_reverts_each_commit_in_order_on_a_branch_of_its_own(fake):
    project, mr = merge_request_of(fake)
    main = project.branches["main"]
    shas = [sha_of("step 2"), sha_of("step 1")]  # the newest first

    revert = calls(
        fake,
        lambda gl: gl.open_revert(
            PATH,
            shas=shas,
            target_branch="main",
            branch="evo-revert/1",
            title="Revert !1: Add the lumen report",
            description="The UAT check failed twice after !1 merged.",
            labels=("evo-revert",),
        ),
    )
    assert project.reverted == [(shas[0], "evo-revert/1"), (shas[1], "evo-revert/1")]
    assert (revert.source_branch, revert.target_branch, revert.labels) == ("evo-revert/1", "main", ("evo-revert",))
    assert revert.title == "Revert !1: Add the lumen report" and revert.iid == 2
    assert project.branches["main"] == main  # nothing lands before the revert goes through the queue
    with pytest.raises(GitLabRefused, match="Branch already exists"):
        calls(fake, lambda gl: gl.create_branch(PATH, "evo-revert/1", "main"))


def test_a_revert_gitlab_cannot_make_is_refused_with_its_error_code(fake):
    project, _ = merge_request_of(fake)
    calls(fake, lambda gl: gl.create_branch(PATH, "evo-revert/1", "main"))
    project.revert_refusals[sha_of("step 1")] = "conflict"
    with pytest.raises(RevertRefused, match="cannot revert this commit automatically") as refused:
        calls(fake, lambda gl: gl.revert_commit(PATH, sha_of("step 1"), "evo-revert/1"))
    assert (refused.value.status, refused.value.code) == (400, "conflict")


# GitLab failing


@pytest.mark.parametrize(
    "answer, problem",
    [
        ((500, {"message": "500 Internal Server Error"}), "GitLab answered 500 while reading merge request !1"),
        ((503, None), "GitLab answered 503 while reading merge request !1"),
        ((429, {"message": "Retry later"}), "GitLab answered 429"),
        ((302, None, {"location": "https://sso.example.org/login"}), "answered 302 with a redirect"),
        ((200, None), "answered 200 without JSON"),
        ((200, ["not", "an", "object"]), "without a JSON object"),
        ((200, {"iid": 1}), "without a usable project_id"),
        ((204, None), "GitLab answered 204 while reading merge request !1"),
        ((httpx.ReadTimeout("slow"),), "GitLab did not answer within 30s while reading merge request !1"),
        (
            (httpx.ConnectError("refused"),),
            f"cannot reach GitLab while reading merge request !1 of {PATH} (ConnectError)",
        ),
    ],
)
def test_gitlab_failing_rate_limiting_redirecting_or_not_answering_is_unavailable(fake, answer, problem):
    merge_request_of(fake)
    fake.queue(*answer)
    with pytest.raises(GitLabUnavailable, match=re.escape(problem)):
        calls(fake, lambda gl: gl.merge_request(PATH, 1))


def test_a_token_gitlab_does_not_take_and_an_object_it_lacks_are_refusals_that_say_why(fake):
    merge_request_of(fake)
    with pytest.raises(GitLabRefused, match="did not take the forge secret \\(401\\)") as refused:
        calls(fake, lambda gl: gl.merge_request(PATH, 1), token=sample())
    assert refused.value.status == 401
    with pytest.raises(GitLabRefused, match="found nothing \\(404\\) while reading merge request !7"):
        calls(fake, lambda gl: gl.merge_request(PATH, 7))
    fake.queue(403, {"message": "403 Forbidden"})
    with pytest.raises(GitLabRefused, match="refused \\(403\\) while rebasing !1 of .*: 403 Forbidden: .*scope api"):
        calls(fake, lambda gl: gl.rebase(PATH, 1))


def test_the_token_reaches_no_url_log_record_error_or_repr(fake, caplog):
    caplog.set_level(logging.DEBUG)
    project, mr = merge_request_of(fake)
    errors = []

    async def work(gl):
        assert fake.token not in repr(gl) and fake.token not in str(gl)
        await gl.merge_request(PATH, mr.iid)
        for failure in ((500, None), (httpx.ReadTimeout("slow"),), (401, {"message": "401 Unauthorized"})):
            fake.queue(*failure)
            try:
                await gl.merge_request(PATH, mr.iid)
            except (GitLabUnavailable, GitLabRefused) as exc:
                errors.append(exc)
        try:
            await gl.merge(PATH, mr.iid, sha_of("stale"))
        except ShaMismatch as exc:
            errors.append(exc)

    calls(fake, work)
    assert {"gitlab call", "gitlab call timed out"} <= {record.getMessage() for record in caplog.records}
    raw = caplog.text + "\n".join(f"{r.getMessage()} {r.args!r} {vars(r)!r}" for r in caplog.records)
    shown = " ".join(f"{e!s} {e!r} {e.args!r}" for e in errors)
    assert len(errors) == 4
    assert fake.token not in raw and fake.token not in shown
    assert all(fake.token not in str(request.url) for request in fake.requests)
