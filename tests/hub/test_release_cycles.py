"""A release cycle after its tag: the hub follows the CI of the tag's workflow, fixes a red tag forward and stops with a
triage thread and a decision (``evo_agents.hub.release_ci``, ``evo_agents.hub.server.release_watch``,
``evo_agents.hub.server.release_cycles``, ``evo_agents.hub.server.release_answer``).

The checks step 3 of the daily-release plan names. a3: release.yml of the tag passes with every job, publish included,
and the cycle is published, its owner told on the web and Telegram with the version, the workflow run and the page on
PyPI; no run of the workflow 30 minutes after the tag, or one going over 3 hours, stops the cycle. a4: a red tag's
failed jobs and the end of their logs give the failed test ids (pytest's FAILED and ERROR lines, Playwright's failed
tests, else the job and its failed step) and a fix run in mode fix with them; a failure of the infrastructure alone
runs the failed jobs again once instead; the fix lands as the next patch, the red tag staying; a fix with signs of
test hacking is never merged and stops the cycle. a5: the cycle stops on its cap, on one test red at two tags in a
row, on a timeout, on a failed run or on hacking, with a thread in the project's triage chat and a decision of three
options for its owner, and goes on with nothing until the answer. a6: a red newest tag opens, at the daily check, a
cycle that starts with its fix, even with no packaging change; a tag a person pushes while a cycle is open becomes a
tag it follows. GitHub is the fake of ``tests.hub.fake_github``, with GitHub Actions; the workers' App holds actions
write there unless a test says otherwise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from evo_agents.hub import judge, release_ci, release_runs, runs, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.release_check import release_pass
from evo_agents.hub.server.release_watch import watch_releases
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, OWNER, app_key, install  # noqa: F401 (app_key is a fixture)
from tests.hub.test_credentials_api import hub_config as credentials_config
from tests.hub.test_credentials_api import members as credential_members
from tests.hub.test_credentials_api import worker_of as credential_worker
from tests.hub.test_curator import PROJECT, grant
from tests.hub.test_release_runs import (
    VERSION_FILE,
    at_head,
    cut,
    done,
    land,
    landed_row,
    opened,
    pushed,
    says_release,
    tag_of,
    version_line,
)
from tests.hub.test_release_schedule import BASE, DOCS, HEAD, NEXT, REPO, SIX, fire, repo_at, set_schedule, status_of
from tests.hub.test_runs import claim, moved, report

ACTIONS = {"contents": "write", "pull_requests": "write", "metadata": "read", "actions": "write"}
NO_ACTIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
WORKFLOW = "release.yml"
FIXED, FIXED_AGAIN, PERSON = "2" * 40, "3" * 40, "4" * 40
TEST_A = "tests/hub/test_paths.py::test_a_written_file_ends_lines_in_lf[windows]"
TEST_B = "tests/hub/test_store.py::test_the_store_closes_its_database"
E2E = "[chromium] e2e/releases.spec.ts › Releases › shows the cycle"


def logged(*lines: str) -> str:
    """A job's log as GitHub writes it: each line after its time."""
    return "\n".join(f"2026-10-21T23:{n % 60:02d}:00.1234567Z {line}" for n, line in enumerate(lines))


PYTEST_LOG = logged(
    "============================= test session starts ==============================",
    "tests/hub/test_paths.py F.                                              [ 50%]",
    "=========================== short test summary info ============================",
    f"FAILED {TEST_A} - AssertionError: assert 'a\\r\\n' == 'a\\n'",
    f"ERROR {TEST_B} - PermissionError: [WinError 32] The process cannot access the file",
    "==================== 1 failed, 40 passed, 1 error in 9.81s =====================",
    "##[error]Process completed with exit code 1.",
)
PLAYWRIGHT_LOG = logged(
    "Running 12 tests using 2 workers",
    "  1) [chromium] › e2e/releases.spec.ts:14:3 › Releases › shows the cycle ─────────────────────",
    "    Error: expect(locator).toBeVisible() failed",
    "  1 failed",
    "    [chromium] › e2e/releases.spec.ts:14:3 › Releases › shows the cycle ─────────────────────",
    "  1 flaky",
    "    [chromium] › e2e/curator.spec.ts:9:3 › Curator › shows the briefs ─────────────────────",
    "  10 passed (41.2s)",
)
INFRA_LOG = logged(
    "Collecting httpx>=0.27",
    "##[error]The runner has received a shutdown signal. This can happen when the runner service is stopped.",
    "##[error]The operation was canceled.",
)
GREEN = [{"name": "version"}, {"name": "ci / test (ubuntu-latest, 3.12)"}, {"name": "build"}, {"name": "publish"}]


def red_jobs(log: str = PYTEST_LOG, job: str = "ci / test-windows (3.12)") -> list[dict]:
    return [
        {"name": "version"},
        {"name": job, "conclusion": "failure", "step": "Run pytest", "log": log},
        {"name": "build", "conclusion": "skipped"},
        {"name": "publish", "conclusion": "skipped"},
    ]


@pytest.fixture
def world(hub_db, tmp_path, github, app_key):  # noqa: F811
    """test_release_runs' world, the workers' App holding actions write on evo-agents."""
    yield from _world(hub_db, tmp_path, github, app_key, ACTIONS)


@pytest.fixture
def blind(hub_db, tmp_path, github, app_key):  # noqa: F811
    """The same world, the App's installation granting no actions."""
    yield from _world(hub_db, tmp_path, github, app_key, NO_ACTIONS)


def _world(hub_db, tmp_path, github, app_key, permissions):  # noqa: F811
    config = credentials_config(hub_db, tmp_path, github, app_key)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = credential_members(client, github)
        grant(client, headers, OWNER, "admin")
        install(github, REPO, permissions=permissions)
        repo_at(github, HEAD)
        at_head(github)
        worker = credential_worker(client, headers["owner"], "mac")
        yield SimpleNamespace(
            client=client, headers=headers, github=github, db=hub_db, app=client.app, config=config, worker=worker
        )


def watch(world) -> dict:
    state = world.app.state
    return world.client.portal.call(partial(watch_releases, state.engine, state.github_app))


def tagged(world) -> tuple[int, str]:
    """The cut run landed: v0.25.0 on its merge commit, which main's tip names 0.25.0; (run id, merge commit)."""
    run_id = cut(world)
    assert land(world) == {"tagged": 1}
    merge = tag_of(world.github, "v0.25.0")[0]
    world.github.put_file(MINE, REPO, merge, VERSION_FILE, version_line("0.25.0"))
    return run_id, merge


def ci(world, tag: str, sha: str, conclusion: str | None = "success", jobs=GREEN, **more):
    return world.github.workflow_run(
        MINE, REPO, workflow=WORKFLOW, sha=sha, tag=tag, conclusion=conclusion, jobs=jobs, **more
    )


def tags(db) -> list[dict]:
    t = tables.release_tags
    names = ("tag", "commit", "run_id", "ci_state", "workflow_run_id", "reruns", "failures", "problem")
    rows = sql(db, select(*(t.c[name] for name in names)).order_by(t.c.id))
    return [dict(zip(names, row, strict=True)) for row in rows]


def cycle(db) -> dict:
    c = tables.release_cycles
    names = ("id", "state", "stop_cause", "stop_reason", "budget_usd", "decision_id", "triage_chat_id", "ended_at")
    row = sql(db, select(*(c.c[name] for name in names)).order_by(c.c.id.desc()).limit(1))[0]
    return dict(zip(names, row, strict=True))


def release_rows(db) -> list[tuple]:
    rr = tables.release_runs
    return sql(db, select(rr.c.run_id, rr.c.mode, rr.c.tag).order_by(rr.c.run_id))


def claimed(world, mode: str = "fix") -> dict:
    says_release(world)
    spec = claim(world.client, world.worker)
    assert spec is not None and spec["release"]["mode"] == mode, spec
    moved(world.client, world.worker, spec["id"], "running")
    return spec


def fixed(world, run_id: int, version: str = "0.25.1", *, head: str = FIXED, files: list | None = None) -> dict:
    """The fix run pushed release/<version> at ``head`` (a pull request of it listing ``files``) and ended done; the
    landing pass."""
    if files is not None:
        world.github.push(MINE, REPO, release_runs.branch_of(version), head, files=files)
    answer = pushed(world, run_id, version, head=head, decision=f"Decision: {version}, the next patch of the red tag.")
    assert answer.status_code == 201, answer.text
    done(world, run_id)
    return land(world)


def red_tag_fixing(world) -> tuple[str, int]:
    """v0.25.0 tagged, its CI red with TEST_A and TEST_B, and its fix run queued and claimed; (merge, fix run id)."""
    _, merge = tagged(world)
    ci(world, "v0.25.0", merge, "failure", red_jobs())
    assert watch(world) == {"fix": 1}
    return merge, claimed(world)["id"]


def notifications(db, kind: str, notice_kind: str | None = None) -> list[tuple]:
    n = tables.notifications
    query = select(n.c.id, n.c.title, n.c.body, n.c.details, n.c.link, n.c.decision_id).where(n.c.kind == kind)
    if notice_kind is not None:
        query = query.where(n.c.notice_kind == notice_kind)
    return sql(db, query.order_by(n.c.id))


def answer(world, decision_id: int, option: str | None, who: str = "owner", **body):
    path = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
    return world.client.post(path, json={"option": option, **body}, headers=world.headers[who])


def thread_messages(world, chat_id: int) -> list[str]:
    m = tables.chat_messages
    return [row[0] for row in sql(world.db, select(m.c.body).where(m.c.chat_id == chat_id).order_by(m.c.id))]


def stopped_on_timeout(world) -> dict:
    """v0.25.0 tagged and no run of its workflow for 31 minutes: the cycle stopped; the cycle's row."""
    tagged(world)
    t = tables.release_tags
    sql(world.db, update(t).values(watched_since=func.now() - timedelta(minutes=31)))
    assert watch(world) == {"stopped": 1}
    return cycle(world.db)


# The model


def test_the_failed_test_ids_of_a_fix_come_from_pytest_and_playwright_else_the_job_and_its_step():
    assert release_ci.failed_tests(PYTEST_LOG) == [TEST_A, TEST_B]
    assert release_ci.failed_tests(PLAYWRIGHT_LOG) == [E2E], "a flaky test did not fail"
    assert release_ci.failed_tests(logged("ERROR: Could not find a version that satisfies httpx")) == []
    assert release_ci.failed_tests(logged("ERROR tests/hub/test_store.py - ImportError: sqlalchemy")) == [
        "tests/hub/test_store.py"
    ]
    job = {"id": 9, "name": "ci / web-check", "conclusion": "failure", "html_url": "https://github.com/x"}
    job["steps"] = [{"name": "Set up job", "conclusion": "success"}, {"name": "npm run lint", "conclusion": "failure"}]
    found = release_ci.job_failure(job, logged("x.ts:3:1 error  no-unused-vars"))
    assert found["ids"] == ["ci / web-check: npm run lint"] and found["tests"] is False and found["infra"] is None
    assert "no-unused-vars" in found["tail"] and "2026-10-21T" not in found["tail"]
    assert release_ci.summary([found])["infra"] is False
    text = release_ci.failures_text("v0.25.0", "https://github.com/run/1", release_ci.summary([found]))
    assert (
        "ci / web-check, step npm run lint" in text and "no-unused-vars" in text and "https://github.com/run/1" in text
    )
    assert release_ci.pypi_url("0.25.0") == "https://pypi.org/project/evo-ak/0.25.0/"


def test_the_infra_rules_name_a_lost_runner_the_network_while_installing_and_pypi_answering_5xx():
    assert release_ci.infra_rule(INFRA_LOG) == "runner_lost"
    assert release_ci.infra_rule(logged("pip._vendor.urllib3: ReadTimeoutError: Read timed out.")) == "network"
    assert release_ci.infra_rule(logged("ERROR: HTTP error 503 while getting https://files.pythonhosted.org/x")) == (
        "pypi_5xx"
    )
    assert release_ci.infra_rule(PYTEST_LOG) is None
    infra = release_ci.job_failure({"name": "build", "conclusion": "failure"}, INFRA_LOG)
    tested = release_ci.job_failure({"name": "ci / test", "conclusion": "failure"}, PYTEST_LOG + "\n" + INFRA_LOG)
    assert infra["infra"] == "runner_lost" and tested["infra"] is None, "a job that names a failed test is no infra"
    assert release_ci.summary([infra])["infra"] is True and release_ci.summary([infra, tested])["infra"] is False
    assert release_ci.same_tests([TEST_A, TEST_B], [TEST_B, "x"]) == [TEST_B]


def patch(path: str, *lines: str, status: str = "modified") -> dict:
    return {"filename": path, "status": status, "patch": "@@ -1,3 +1,3 @@\n" + "\n".join(lines)}


def test_the_hacking_signs_of_a_fix_spare_importorskip_and_a_platform_skip_with_its_reason():
    def signs(*files: dict) -> list[str]:
        return [item["kind"] for item in release_ci.release_signs(judge.from_github_files(list(files)), REPO)]

    assert signs(patch("tests/test_x.py", " def test_x():", "-    assert f() == 1", "+    assert True")) == [
        "assert_removed"
    ]
    assert signs(patch("tests/test_x.py", "+@pytest.mark.skip(reason='flaky')", " def test_x():")) == ["skip_added"]
    assert signs({"filename": "tests/test_y.py", "status": "removed", "changes": 0, "patch": ""}) == ["test_removed"]
    assert signs(patch("tests/test_x.py", '+sqlalchemy = pytest.importorskip("sqlalchemy")', " import x")) == []
    platform = '+@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")'
    assert signs(patch("tests/test_x.py", platform, " def test_mode():")) == []
    assert signs(patch("tests/test_x.py", '+@pytest.mark.skipif(sys.platform == "win32")', " def test_m():")) == [
        "skip_added"
    ], "a platform skip without its reason"
    assert signs(patch("evo_agents/x.py", "-    return a", "+    return b")) == [], "code is no test"
    assert signs(patch(".github/workflows/ci.yml", "-  timeout: 10", "+  timeout: 20")) == []


# a3: published, or a timeout


def test_a_tag_whose_workflow_passes_with_publish_is_published_and_its_owner_told_on_the_web_and_telegram(world):
    owner = sql(world.db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0][0]
    ch = tables.notification_channels
    [(telegram,)] = sql(world.db, insert(ch).values(user_id=owner, kind="telegram", config={}).returning(ch.c.id))
    run_id, merge = tagged(world)
    assert tags(world.db)[0] | {"failures": None} == {
        "tag": "v0.25.0",
        "commit": merge,
        "run_id": run_id,
        "ci_state": "pending",
        "workflow_run_id": None,
        "reruns": 0,
        "failures": None,
        "problem": None,
    }
    assert watch(world) == {}, "no run of the workflow yet: it waits"
    jobs = [{"name": "version", "status": "in_progress", "conclusion": None}]
    run = ci(world, "v0.25.0", merge, None, jobs, status="in_progress")
    assert watch(world) == {}
    assert (tags(world.db)[0]["ci_state"], tags(world.db)[0]["workflow_run_id"]) == ("running", run.id)
    world.github.attempt(run.id, conclusion="success", jobs=GREEN)
    assert watch(world) == {"published": 1}
    found = cycle(world.db)
    assert found["state"] == "published" and found["ended_at"] is not None and found["stop_cause"] is None
    assert tags(world.db)[0]["ci_state"] == "passed"
    [(notice_id, title, body, details, link, _)] = notifications(world.db, "notice", "release")
    url = f"https://github.com/{MINE}/{REPO}/actions/runs/{run.id}"
    assert "0.25.0" in title and url in body and "https://pypi.org/project/evo-ak/0.25.0/" in body
    assert details == {
        "event": "published",
        "cycle_id": found["id"],
        "tag": "v0.25.0",
        "version": "0.25.0",
        "workflow_url": url,
        "pypi_url": "https://pypi.org/project/evo-ak/0.25.0/",
    }
    assert link == f"/p/{PROJECT}/releases"
    d = tables.notification_deliveries
    channels = sql(world.db, select(d.c.channel_id).where(d.c.notification_id == notice_id).order_by(d.c.id))
    assert channels == [(None,), (telegram,)], "the web and the owner's Telegram"
    assert watch(world) == {}, "a published cycle is not watched"
    assert status_of(world)["cycle"] is None


def test_a_green_workflow_whose_publish_did_not_succeed_is_no_publication(world):
    _, merge = tagged(world)
    ci(world, "v0.25.0", merge, "success", [{"name": "version"}, {"name": "publish", "conclusion": "skipped"}])
    assert watch(world) == {"stopped": 1}
    found = cycle(world.db)
    assert found["stop_cause"] == "ci" and "its job publish did not succeed" in found["stop_reason"]


def test_no_run_of_the_workflow_30_minutes_after_the_tag_is_a_timeout_stop(world):
    found = stopped_on_timeout(world)
    assert found["state"] == "stopped" and found["stop_cause"] == "timeout"
    assert f"no run of {WORKFLOW} for v0.25.0 30 minutes after" in found["stop_reason"]
    assert tags(world.db)[0]["ci_state"] == "timeout"


def test_a_workflow_running_over_three_hours_is_a_timeout_stop(world):
    _, merge = tagged(world)
    t = tables.release_tags
    sql(world.db, update(t).values(watched_since=func.now() - timedelta(hours=3, minutes=10)))  # tagged then
    started = (datetime.now(UTC) - timedelta(hours=3, minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ci(world, "v0.25.0", merge, None, [], started_at=started, status="in_progress")
    assert watch(world) == {"stopped": 1}
    found = cycle(world.db)
    assert found["stop_cause"] == "timeout" and "is still going 3 hours after it started" in found["stop_reason"]


# a4: a fix, an infra failure run again, signs of hacking


def test_a_red_tag_queues_a_fix_run_with_its_failed_tests_and_the_end_of_their_logs(world):
    cut_id, merge = tagged(world)
    run = ci(world, "v0.25.0", merge, "failure", red_jobs())
    assert watch(world) == {"fix": 1}
    [tag] = tags(world.db)
    assert tag["ci_state"] == "failed" and tag["failures"]["ids"] == [TEST_A, TEST_B]
    assert tag["failures"]["infra"] is False and tag["failures"]["jobs"][0]["step"] == "Run pytest"
    [(_, _, _), (fix_id, mode, red)] = release_rows(world.db)
    assert (mode, red) == ("fix", "v0.25.0")
    r = tables.runs
    [(budget, title)] = sql(world.db, select(r.c.budget, r.c.title).where(r.c.id == fix_id))
    assert budget["max_usd"] == 200.0 and "fix v0.25.0 forward" in title
    spec = claimed(world)
    assert spec["id"] == fix_id
    assert spec["release"] | {"cycle_id": 0} == {
        "cycle_id": 0,
        "mode": "fix",
        "repo": REPO,
        "start_commit": merge,
        "base_tag": "v0.24.0",
        "tag_prefix": "v",
        "red_tag": "v0.25.0",
    }
    prompt = spec["prompt"]
    for said in (
        "The tag v0.25.0 stays as it is",
        "The version is 0.25.1, the next patch after v0.25.0",
        f"https://github.com/{MINE}/{REPO}/actions/runs/{run.id}",
        "Job ci / test-windows (3.12), step Run pytest (failure)",
        f"  - {TEST_A}",
        f"  - {TEST_B}",
        "PermissionError: [WinError 32]",
        "Never delete a test",
    ):
        assert said in prompt, said
    assert len(prompt.encode()) <= runs.MAX_PROMPT_BYTES
    assert watch(world) == {}, "the fix is queued once"
    assert len(release_rows(world.db)) == 2 and cut_id < fix_id


def test_a_fix_lands_as_the_next_patch_on_its_merge_commit_and_the_red_tag_stays(world):
    merge, fix_id = red_tag_fixing(world)
    assert fixed(world, fix_id) == {"tagged": 1}
    assert tag_of(world.github, "v0.25.0") == (merge, None), "the red tag stays as it was"
    patch_merge = tag_of(world.github, "v0.25.1")[0]
    assert landed_row(world.db, fix_id)["tagged"] == "v0.25.1"
    assert [(item["tag"], item["ci_state"], item["run_id"]) for item in tags(world.db)] == [
        ("v0.25.0", "failed", tags(world.db)[0]["run_id"]),
        ("v0.25.1", "pending", fix_id),
    ]
    ci(world, "v0.25.1", patch_merge)
    assert watch(world) == {"published": 1}
    assert cycle(world.db)["state"] == "published"
    assert [row[1] for row in notifications(world.db, "notice", "release")] == [
        f"Released 0.25.1 of {REPO}: v0.25.1 passed its CI and is on PyPI"
    ]


def test_an_infra_failure_runs_the_failed_jobs_again_once_then_queues_a_fix(world):
    _, merge = tagged(world)
    run = ci(world, "v0.25.0", merge, "failure", red_jobs(INFRA_LOG, "build"))
    assert watch(world) == {"rerun": 1}
    assert world.github.reruns == [(MINE, REPO, run.id, "workers")]
    [tag] = tags(world.db)
    assert (tag["ci_state"], tag["reruns"], tag["failures"]["infra"]) == ("running", 1, True)
    assert len(release_rows(world.db)) == 1, "no fix run for the infrastructure"
    assert watch(world) == {}, "the next attempt is queued: the hub waits for it"
    world.github.attempt(run.id, conclusion="failure", jobs=red_jobs(INFRA_LOG, "build"))
    assert watch(world) == {"fix": 1}, "run again once only"
    assert len(world.github.reruns) == 1
    assert [row[1:] for row in release_rows(world.db)] == [("cut", None), ("fix", "v0.25.0")]


def test_an_infra_failure_rerun_that_passes_publishes(world):
    _, merge = tagged(world)
    run = ci(world, "v0.25.0", merge, "failure", red_jobs(INFRA_LOG, "build"))
    assert watch(world) == {"rerun": 1}
    world.github.attempt(run.id, conclusion="success", jobs=GREEN)
    assert watch(world) == {"published": 1}


def test_a_fix_with_signs_of_test_hacking_is_not_merged_and_stops_the_cycle(world):
    _, fix_id = red_tag_fixing(world)
    files = [
        patch(
            "tests/hub/test_paths.py",
            " def test_a_written_file_ends_lines_in_lf():",
            "-    assert a == b",
            "+    assert True",
        ),
        patch("tests/hub/test_store.py", "+@pytest.mark.skip(reason='locks on Windows')", " def test_x():"),
    ]
    assert fixed(world, fix_id, files=files) == {"refused": 1}
    pull = world.github.pull_of(MINE, REPO, "release/0.25.1")
    assert pull is not None and pull.state == "open" and not pull.merged, "opened, never merged"
    assert tag_of(world.github, "v0.25.1") is None and len(world.github.merges) == 1, "only the cut was merged"
    found = cycle(world.db)
    assert found["state"] == "stopped" and found["stop_cause"] == "hacking"
    assert "signs of test hacking" in found["stop_reason"] and "an assertion of a test removed" in found["stop_reason"]
    assert landed_row(world.db, fix_id)["land_state"] == "refused"


def test_a_fix_that_adds_importorskip_and_a_platform_skip_with_its_reason_is_no_hacking(world):
    _, fix_id = red_tag_fixing(world)
    files = [
        patch("tests/hub/test_store.py", '+psycopg = pytest.importorskip("psycopg")', " import json"),
        patch("tests/hub/test_paths.py", '+@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")', " def t():"),
    ]
    assert fixed(world, fix_id, files=files) == {"tagged": 1}


# a5: stops, the thread and the decision


def test_the_same_test_red_at_two_tags_in_a_row_stops_the_cycle(world):
    _, fix_id = red_tag_fixing(world)
    assert fixed(world, fix_id) == {"tagged": 1}
    patch_merge = tag_of(world.github, "v0.25.1")[0]
    ci(world, "v0.25.1", patch_merge, "failure", red_jobs(logged(f"FAILED {TEST_B} - PermissionError")))
    assert watch(world) == {"stopped": 1}
    found = cycle(world.db)
    assert found["stop_cause"] == "same_test" and TEST_B in found["stop_reason"]
    assert "v0.25.0 and v0.25.1" in found["stop_reason"]
    assert [row[1] for row in release_rows(world.db)] == ["cut", "fix"], "no third run"


def test_another_test_red_at_the_next_tag_queues_the_next_fix(world):
    _, fix_id = red_tag_fixing(world)
    assert fixed(world, fix_id) == {"tagged": 1}
    ci(world, "v0.25.1", tag_of(world.github, "v0.25.1")[0], "failure", red_jobs(PLAYWRIGHT_LOG, "ci / web-check"))
    assert watch(world) == {"fix": 1}
    assert release_rows(world.db)[-1][1:] == ("fix", "v0.25.1"), "no limit on the tags of a cycle"


def test_a_cap_left_under_the_least_a_run_takes_stops_the_cycle_on_its_budget(world):
    cut_id, merge = tagged(world)
    r = tables.runs
    sql(world.db, update(r).where(r.c.id == cut_id).values(usage={"total_cost_usd": 199.995}))
    ci(world, "v0.25.0", merge, "failure", red_jobs())
    assert watch(world) == {"stopped": 1}
    found = cycle(world.db)
    assert found["stop_cause"] == "budget" and "$200.00" in found["stop_reason"]
    assert len(release_rows(world.db)) == 1
    assert status_of(world)["cycle"]["cost_usd"] == 199.995


def test_a_stop_opens_a_thread_in_the_triage_chat_and_asks_the_owner_a_decision_of_three_options(world):
    spec = opened(world)
    assert report(world.client, world.worker, spec["id"], "failed", error="the bump script failed").status_code == 200
    found = cycle(world.db)
    assert found["stop_cause"] == "run_failed" and found["decision_id"] is not None
    d = tables.decisions
    [(run_id, category, options, state, question)] = sql(
        world.db,
        select(d.c.run_id, d.c.category, d.c.options, d.c.state, d.c.question).where(d.c.id == found["decision_id"]),
    )
    assert (run_id, category, state) == (spec["id"], "release", "open")
    assert [item["key"] for item in options] == ["continue", "close", "pause"]
    assert "one more cap of $200.00" in options[0]["label"] and "the bump script failed" in question
    # the hub recommends closing it, which spends nothing and keeps the schedule, and says so
    assert [item["key"] for item in options if item.get("recommended")] == ["close"]
    reason = sql(world.db, select(d.c.recommended_reason).where(d.c.id == found["decision_id"]))[0][0]
    assert reason.startswith("Closing spends nothing more of the cap")
    [(_, title, _, _, link, decision_id)] = notifications(world.db, "decision")
    assert decision_id == found["decision_id"] and link == f"/inbox?decision={decision_id}"
    assert f"Release cycle #{found['id']} of {PROJECT} stopped" in title
    tc = tables.triage_chats
    assert sql(world.db, select(tc.c.chat_id).where(tc.c.plan_id.is_(None))) == [(found["triage_chat_id"],)]
    [body] = thread_messages(world, found["triage_chat_id"])
    for said in ("stopped (run_failed)", "the bump script failed", "Cost: $0.00 of the cycle's cap $200.00", "@"):
        assert (said in body) is (said != "@"), said  # facts, and no agent mentioned: the owner decides
    assert f"decision #{decision_id}" in body and f"/inbox?decision={decision_id}" in body
    r = tables.runs
    assert sql(world.db, select(func.count()).select_from(r).where(r.c.kind == "chat")) == [(0,)]
    status = status_of(world)["cycle"]
    assert (status["state"], status["stop_cause"], status["decision_id"]) == ("stopped", "run_failed", decision_id)
    assert status["thread"].startswith(f"/chats/{found['triage_chat_id']}?message=")
    shown = world.client.get(f"/v1/projects/{PROJECT}/decisions/{decision_id}", headers=world.headers["owner"])
    assert shown.status_code == 200 and shown.json()["category"] == "release"


def test_a_stopped_cycle_does_not_go_on_until_its_decision_and_keeps_the_check_busy(world):
    stopped_on_timeout(world)
    assert watch(world) == {}
    state = world.app.state
    assert world.client.portal.call(partial(release_pass, state.engine, state.github_app, now=NEXT)) == {"busy": 1}
    assert len(release_rows(world.db)) == 1


def test_a_stopped_cycle_a_newer_tag_supersedes_closes_and_cancels_its_decision(world):
    found = stopped_on_timeout(world)
    world.github.tag(MINE, REPO, "v0.25.1", HEAD)  # released by hand, outside the cycle, on nightly
    state = world.app.state
    assert world.client.portal.call(partial(release_pass, state.engine, state.github_app, now=NEXT)) == {
        "unchanged": 1
    }, "the check goes on from v0.25.1 as if no cycle were there"
    closed = cycle(world.db)
    assert (closed["id"], closed["state"]) == (found["id"], "closed") and closed["ended_at"] is not None
    assert (
        "v0.25.1 was tagged outside cycle" in closed["stop_reason"] and "no run of release.yml" in closed["stop_reason"]
    )
    d, n = tables.decisions, tables.notifications
    assert sql(world.db, select(d.c.state).where(d.c.id == found["decision_id"])) == [("cancelled",)]
    unread = select(func.count()).select_from(n).where(n.c.decision_id == found["decision_id"], n.c.read_at.is_(None))
    assert sql(world.db, unread) == [(0,)]
    said = thread_messages(world, found["triage_chat_id"])[-1]
    assert said.startswith("v0.25.1 was tagged outside cycle") and f"cancelled decision #{found['decision_id']}" in said
    assert answer(world, found["decision_id"], "close").status_code == 409, "a cancelled decision takes no answer"
    assert status_of(world)["cycle"] is None and len(release_rows(world.db)) == 1


def test_a_stopped_cycle_with_a_run_active_or_no_newer_tag_stays_busy(world):
    found = stopped_on_timeout(world)
    world.github.tag(MINE, REPO, "v0.24.5", HEAD)  # newer than its base, older than the v0.25.0 it made
    state = world.app.state
    assert world.client.portal.call(partial(release_pass, state.engine, state.github_app, now=NEXT)) == {"busy": 1}
    world.github.tag(MINE, REPO, "v0.26.0", HEAD)
    r, rr = tables.runs, tables.release_runs
    sql(world.db, update(r).values(state="review", finished_at=None).where(r.c.id.in_(select(rr.c.run_id))))
    later = NEXT + timedelta(days=1)
    assert world.client.portal.call(partial(release_pass, state.engine, state.github_app, now=later)) == {"busy": 1}
    assert cycle(world.db)["state"] == "stopped"
    d = tables.decisions
    assert sql(world.db, select(d.c.state).where(d.c.id == found["decision_id"])) == [("open",)]


def test_the_decision_continue_reopens_the_cycle_with_one_more_cap_and_queues_the_next_fix(world):
    _, fix_id = red_tag_fixing(world)
    assert fixed(world, fix_id) == {"tagged": 1}
    ci(world, "v0.25.1", tag_of(world.github, "v0.25.1")[0], "failure", red_jobs())
    assert watch(world) == {"stopped": 1}
    found = cycle(world.db)
    refused = answer(world, found["decision_id"], "continue", "other")
    assert refused.status_code == 403
    assert answer(world, found["decision_id"], None, text="go on").status_code == 422
    answered = answer(world, found["decision_id"], "continue")
    assert answered.status_code == 200, answered.text
    assert answered.json()["state"] == "answered" and answered.json()["answer_option"] == "continue"
    again = cycle(world.db)
    assert (again["state"], again["budget_usd"], again["stop_cause"]) == ("open", 400.0, None)
    assert release_rows(world.db)[-1][1:] == ("fix", "v0.25.1")
    replies = thread_messages(world, found["triage_chat_id"])
    assert len(replies) == 2 and replies[1].startswith(f"{OWNER} chose continue: cycle #{found['id']} goes on")
    assert answer(world, found["decision_id"], "close").status_code == 409, "answered once"
    a = tables.audit
    assert sql(world.db, select(func.count()).where(a.c.action == "decision.answer")) == [(1,)]


def test_the_decision_continue_after_a_timeout_follows_the_ci_again_from_now(world):
    found = stopped_on_timeout(world)
    assert answer(world, found["decision_id"], "continue").status_code == 200
    assert cycle(world.db)["state"] == "open" and tags(world.db)[0]["ci_state"] == "pending"
    assert watch(world) == {}, "the 30 minutes count from the answer"


def test_the_decision_close_closes_the_cycle_and_the_next_check_may_open_one(world):
    found = stopped_on_timeout(world)
    assert answer(world, found["decision_id"], "close").status_code == 200
    closed = cycle(world.db)
    assert closed["state"] == "closed" and closed["ended_at"] is not None and "closed by" in closed["stop_reason"]
    assert status_of(world)["cycle"] is None


def test_the_decision_pause_pauses_the_schedule_and_closes_the_cycle(world):
    found = stopped_on_timeout(world)
    assert answer(world, found["decision_id"], "pause").status_code == 200
    status = status_of(world)
    assert status["paused"] is True and status["schedule"]["paused_by"] == OWNER and status["cycle"] is None
    assert cycle(world.db)["state"] == "closed"
    assert fire(world, NEXT) == {"paused": 1}


def test_a_stop_of_a_landing_refused_asks_the_owner_too(world):
    at_head(world.github, "0.25.0")
    cut(world)
    assert land(world) == {"refused": 1}
    found = cycle(world.db)
    assert found["stop_cause"] == "refused" and found["decision_id"] is not None


# a6: a red newest tag, a tag a person pushed


def test_a_red_latest_tag_opens_a_cycle_that_starts_with_its_fix_even_with_no_packaging_change(world):
    world.github.tag(MINE, REPO, "nightly", DOCS)  # only docs and tests changed since v0.24.0
    run = ci(world, "v0.24.0", BASE, "failure", red_jobs())
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    last = status_of(world)["last_check"]
    assert "v0.24.0 is red" in last["reason"] and last["changed_paths"] == []
    found = status_of(world)["cycle"]
    assert (found["start"], found["base_tag"]) == ("fix", "v0.24.0")
    assert [(item["tag"], item["ci_state"], item["run_id"]) for item in found["tags"]] == [("v0.24.0", "failed", None)]
    assert found["tags"][0]["failed_tests"] == [TEST_A, TEST_B]
    assert found["tags"][0]["workflow_url"] == f"https://github.com/{MINE}/{REPO}/actions/runs/{run.id}"
    [(run_id, mode, red)] = release_rows(world.db)
    assert (mode, red, last["run_id"]) == ("fix", "v0.24.0", run_id)
    spec = claimed(world)
    assert spec["release"]["start_commit"] == BASE and spec["release"]["red_tag"] == "v0.24.0"
    assert "The version is 0.24.1" in spec["prompt"] and TEST_A in spec["prompt"]
    assert watch(world) == {}, "its fix is queued already"


def test_a_red_latest_tag_whose_ci_cannot_be_read_is_not_red_and_the_check_says_why(blind):
    blind.github.tag(MINE, REPO, "nightly", DOCS)
    ci(blind, "v0.24.0", BASE, "failure", red_jobs())
    set_schedule(blind, repo=REPO)
    assert fire(blind, SIX) == {"unchanged": 1}
    assert "actions_permission" in status_of(blind)["last_check"]["reason"]


def test_a_green_latest_tag_with_a_packaging_change_cuts_as_before(world):
    ci(world, "v0.24.0", BASE)
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    assert status_of(world)["cycle"]["start"] == "cut"


def test_a_human_tag_pushed_while_a_cycle_is_open_is_followed(world):
    opened(world)
    world.github.tag(MINE, REPO, "v0.24.1", PERSON)
    world.github.tag(MINE, REPO, "v0.24.0-hotfix", PERSON)  # no version X.Y.Z: not a release tag
    assert watch(world) == {"followed": 1}
    assert [(item["tag"], item["commit"], item["run_id"]) for item in tags(world.db)] == [("v0.24.1", PERSON, None)]
    assert watch(world) == {}, "followed once"
    ci(world, "v0.24.1", PERSON)
    assert watch(world) == {"published": 1}
    assert cycle(world.db)["state"] == "published"


# The App without actions


def test_an_installation_without_actions_says_actions_permission_and_tells_the_owner_once(blind):
    tagged(blind)
    assert watch(blind) == {}
    assert watch(blind) == {}
    [tag] = tags(blind.db)
    assert tag["problem"].startswith("actions_permission: the App's installation on")
    assert "actions_permission" in json.dumps(status_of(blind))
    [(_, title, body, details, _, _)] = notifications(blind.db, "notice", "release")
    assert details["event"] == "actions_permission" and "installation's page" in body and "v0.25.0" in title
    t = tables.release_tags
    sql(blind.db, update(t).values(watched_since=func.now() - timedelta(minutes=31)))
    assert watch(blind) == {"stopped": 1}
    found = cycle(blind.db)
    assert found["stop_cause"] == "timeout" and "actions_permission" in found["stop_reason"]


def test_the_release_job_checks_lands_then_watches(world):
    cut_id = cut(world)
    state = world.app.state
    assert world.client.portal.call(partial(release_pass, state.engine, state.github_app, now=SIX)) == {
        "done": 1,
        "landed_tagged": 1,
    }
    merge = tag_of(world.github, "v0.25.0")[0]
    ci(world, "v0.25.0", merge)
    found = world.client.portal.call(partial(release_pass, state.engine, state.github_app, now=SIX))
    assert found == {"done": 1, "watched_published": 1}
    assert landed_row(world.db, cut_id)["tagged"] == "v0.25.0"
