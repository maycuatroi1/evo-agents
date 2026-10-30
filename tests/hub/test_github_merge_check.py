"""The merge check of a run's pull request on github.com (``evo_agents.hub.server.merge_check``; plan
super-board-parity-2, step 1), against Postgres and the fake GitHub of ``tests.hub.fake_github``: the real GitHub is
never called.

The checks the step names: in a repo whose policy has merge_check on, the agent of a plan run asks for the check and
waits on a wait of the run, holding no slot; the job queues the merge queue's judge run, whose claim says to merge the
base into the head and run the checks there; its verdict ends the check and answers the wait, and the verdict holds
for the head it judged alone: a new head starts the check again, and the old judge run's verdict no longer counts. A
conflict fails it with needs a rebase. A repo that leaves merge_check off, a pull request of another branch and one
from a fork are refused. The projects and people are fictional.
"""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import judge, runs, tables
from evo_agents.hub.server import merge_check
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.protection import github_target
from tests.hub.fake_github import FakePull
from tests.hub.live import sql
from tests.hub.test_credentials_api import (  # noqa: F401 (app_key is a fixture)
    MINE,
    app_key,
    install,
    members,
    worker_of,
)
from tests.hub.test_credentials_api import hub_config as credentials_config
from tests.hub.test_merge_gate import ADMIN, SECURITY_PASS, dimensions, put_policy
from tests.hub.test_runs import PROJECT, claim, moved

REPO = "evo-agents"
PLAN_ID = "pull-check"
BRANCH = "plan/pull-check"
VERIFY = "python -m pytest -q"
HEAD, NEWER = "a1" * 20, "b2" * 20
READ = {"pull_requests": "read", "contents": "read", "metadata": "read"}


def plan_body() -> dict:
    return {
        "id": PLAN_ID,
        "goal": "Check a pull request before it merges.",
        "repos": [{"repo": REPO, "branch": BRANCH}],
        "steps": [{"id": 1, "title": "Report", "repo": REPO, "what": "a report", "verify": VERIFY}],
    }


def policy(check: str = "on") -> dict:
    return {"repos": {REPO: {"merge_check": check}}, "judge": {"worker": "mac-mini"}}


def beat(world, free_slots: int = 2) -> None:
    worker = world.worker
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": free_slots,
        "runs": [],
        "agent_version": "0.7.0",
        "run_kinds": list(runs.RUN_KINDS),
        "features": list(runs.WORKER_FEATURES),
    }
    response = world.client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text


@pytest.fixture
def world(hub_db, tmp_path, github, app_key):  # noqa: F811
    """test_credentials_api's project, its hub holding the workers' App, installed on evo-agents with pull_requests
    read; owner an admin with the worker mac-mini, which judges merge requests; the policy has merge_check on for
    evo-agents; a plan run of its step on mac-mini running, and its pull request #7 open at HEAD."""
    config = credentials_config(hub_db, tmp_path, github, app_key)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = members(client, github)
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/owner", json=ADMIN, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
        github.add_repo(MINE, REPO)
        install(github, REPO, permissions=READ)
        worker = worker_of(client, headers["owner"], "mac-mini", slots=2)
        world = SimpleNamespace(client=client, headers=headers, github=github, worker=worker, db=hub_db)
        beat(world)
        put_policy(client, headers["owner"], policy())
        owner = headers["owner"]
        pushed = client.put(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", json={"body": plan_body()}, headers=owner)
        assert pushed.status_code == 200, pushed.text
        body = {"plan_id": PLAN_ID, "worker_id": worker["id"]}
        response = client.post(f"/v1/projects/{PROJECT}/plan-runs", json=body, headers=owner)
        assert response.status_code == 201, response.text
        world.run_id = response.json()["id"]
        assert claim(client, worker)["id"] == world.run_id
        moved(client, worker, world.run_id, "running")
        opened(world, HEAD)
        yield world


def opened(world, head: str, *, number: int = 7, branch: str = BRANCH) -> None:
    pull = FakePull(number, MINE, REPO, branch, head, "main", title="Add the report")
    world.github.pulls[(MINE.lower(), REPO, number)] = pull


def ask(world, **body):
    return world.client.post(
        f"/v1/worker/runs/{world.run_id}/merge-check",
        json={"repo": REPO, "branch": BRANCH, **body},
        headers=world.worker["headers"],
    )


def checks_pass(world) -> dict:
    state = world.client.app.state
    return world.client.portal.call(partial(merge_check.advance, state.engine))


def judge_run(world) -> tuple[dict, dict]:
    """The judge run the check queued, claimed by mac-mini and running; (its claim, its key's headers)."""
    spec = claim(world.client, world.worker)
    assert spec is not None and spec["kind"] == "judge" and not spec["plan_id"], spec
    moved(world.client, world.worker, spec["id"], "running")
    return spec, {**world.worker["headers"], judge.JUDGE_KEY_HEADER: spec["curator"]["judge_key"]}


def verdict(world, spec: dict, headers: dict, head: str, **extra):
    body = {
        "verdict": "pass",
        "reasons": "I read the diff against the plan.",
        "head_sha": head,
        "verify": [{"command": VERIFY, "exit_code": 0}],
        "hidden": [],
        "signs": [],
        "paths": ["docs/report.md"],
        "dimensions": dimensions(),
        "security": SECURITY_PASS,
        **extra,
    }
    return world.client.post(f"/v1/worker/runs/{spec['id']}/merge-verdict", json=body, headers=headers)


def decision(world, decision_id: int):
    d = tables.decisions
    (state, text, category) = sql(
        world.db, select(d.c.state, d.c.answer_text, d.c.category).where(d.c.id == decision_id)
    )[0]
    return SimpleNamespace(state=state, answer_text=text, category=category)


def test_github_merge_check_judges_the_head_merged_with_the_base_and_its_verdict_answers_the_wait(world):
    asked = ask(world)
    assert asked.status_code == 200, asked.text
    found = asked.json()
    assert (found["number"], found["head_sha"], found["state"]) == (7, HEAD, "judging")
    assert decision(world, found["decision_id"]).category == "wait"  # the agent ends its turn: the run parks
    again = ask(world).json()
    assert again["decision_id"] == found["decision_id"], "asking again keeps the one wait"

    assert checks_pass(world) == {"checks_judging": 1}
    spec, headers = judge_run(world)
    told = spec["curator"]
    assert (told["forge"], told["head_sha"], told["merge_result"], told["base_branch"]) == (
        "github",
        HEAD,
        True,
        "main",
    )
    assert [item["repo"] for item in spec["repos"]] == [REPO]
    prompt = spec["prompt"]
    assert "Pull request #7: Add the report" in prompt and "merged with the current origin/main" in prompt
    inputs = world.client.get(f"/v1/worker/runs/{spec['id']}/merge-judge", headers=headers)
    assert inputs.status_code == 200, inputs.text
    assert (inputs.json()["iid"], inputs.json()["verify"]) == (7, [VERIFY])
    passed = verdict(world, spec, headers, HEAD)
    assert passed.status_code == 200, passed.text
    assert passed.json()["passed"] and passed.json()["state"] == "judging"  # the risk is weighed next
    moved(world.client, world.worker, spec["id"], "verifying", "done")

    assert checks_pass(world) == {"checks_passed": 1}
    woke = decision(world, found["decision_id"])
    assert woke.state == "answered"
    assert f"gh pr merge 7 --match-head-commit {HEAD}" in woke.answer_text
    settled = ask(world).json()
    assert (settled["state"], settled["decision_id"]) == ("passed", None), "a verdict there needs no wait"


def test_github_merge_check_a_new_head_voids_the_verdict_and_the_old_judge_run(world):
    first = ask(world).json()
    checks_pass(world)
    spec, headers = judge_run(world)
    opened(world, NEWER)  # the run pushed again while its Judge read the old head
    renewed = ask(world).json()
    assert (renewed["head_sha"], renewed["state"]) == (NEWER, "judging")
    old = verdict(world, spec, headers, HEAD)
    assert old.status_code == 409, "the old head's judge run no longer counts"
    m = tables.merge_checks
    ((judge_run_id, stored, decision_id),) = sql(world.db, select(m.c.judge_run_id, m.c.verdict, m.c.decision_id))
    assert judge_run_id is None and not (stored or {}).get("first")
    assert decision_id == first["decision_id"], "the run's open wait still answers"


def test_github_merge_check_a_conflict_with_the_base_fails_it_with_needs_a_rebase(world):
    found = ask(world).json()
    checks_pass(world)
    spec, headers = judge_run(world)
    failed = verdict(world, spec, headers, HEAD, verdict=None, conflicts=["docs/report.md"])
    assert failed.status_code == 200, failed.text
    assert (failed.json()["passed"], failed.json()["state"]) == (False, "failed")
    assert failed.json()["reason"].startswith("needs a rebase")
    woke = decision(world, found["decision_id"])
    assert woke.state == "answered" and "needs a rebase" in woke.answer_text and "gh pr merge" not in woke.answer_text


def test_github_merge_check_refuses_a_repo_left_off_another_branch_and_a_fork(world):
    put_policy(world.client, world.headers["owner"], policy("off"))
    off = ask(world)
    assert off.status_code == 409 and "merge_check off" in off.json()["message"]
    put_policy(world.client, world.headers["owner"], policy())
    opened(world, NEWER, number=8, branch="feat/other")
    other = ask(world, number=8)
    assert other.status_code == 409 and "not of the run's branch" in other.json()["message"]
    target = github_target(f"https://github.com/{MINE}/{REPO}.git")
    fork = {"number": 9, "state": "open", "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": "x/evo-agents"}}}
    assert "fork" in merge_check.pull_refusal(fork, target, BRANCH)
    assert sql(world.db, select(tables.merge_checks.c.id)) == []


def test_github_merge_check_a_check_turned_off_while_judging_fails_and_wakes_the_run(world):
    found = ask(world).json()
    put_policy(world.client, world.headers["owner"], policy("off"))
    assert checks_pass(world) == {"checks_off": 1}
    assert decision(world, found["decision_id"]).state == "answered"


# The gate of the agent's merge command (K5) and the ancestry of its merge


MERGED_AT = "2026-10-30T12:00:00Z"
MERGE = "c3" * 20


def passed_check(world) -> int:
    """Ask for the check of pull request #7 at HEAD and have the Judge pass it; the wait's decision."""
    found = ask(world).json()
    checks_pass(world)
    spec, headers = judge_run(world)
    assert verdict(world, spec, headers, HEAD).status_code == 200
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    assert checks_pass(world) == {"checks_passed": 1}
    return found["decision_id"]


def gate(world, head: str | None = HEAD, **body):
    payload = {"repo": f"{MINE}/{REPO}", "number": 7, "head": head, **body}
    response = world.client.post(
        f"/v1/worker/runs/{world.run_id}/merge-gate", json=payload, headers=world.worker["headers"]
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_merge_gate_k5_holds_a_merge_until_a_passed_verdict_holds_for_the_head(world):
    unasked = gate(world)
    assert (unasked["gated"], unasked["passed"]) == (True, False)
    assert unasked["command"] == "evo-agents worker merge-check evo-agents"
    assert "no merge check" in unasked["reason"]
    ask(world)
    judging = gate(world)
    assert not judging["passed"] and "still judges" in judging["reason"]
    passed_check(world)
    ok = gate(world)
    assert (ok["gated"], ok["passed"], ok["head_sha"], ok["reason"]) == (True, True, HEAD, None)
    moved_on = gate(world, NEWER)
    assert not moved_on["passed"] and moved_on["head_sha"] == HEAD
    assert "verdict holds for " + HEAD[:12] in moved_on["reason"]
    assert not gate(world, None)["passed"], "a head the hook could not read passes nothing"


def test_merge_gate_k5_a_repo_left_off_is_not_the_hubs_to_gate(world):
    put_policy(world.client, world.headers["owner"], policy("off"))
    off = gate(world)
    assert (off["gated"], off["passed"], off["reason"]) == (False, True, None)
    other = gate(world, repo="someone/else")
    assert other["gated"] is False


def test_merge_gate_k5_a_failed_check_holds_the_merge(world):
    ask(world)
    checks_pass(world)
    spec, headers = judge_run(world)
    failed = verdict(world, spec, headers, HEAD, verdict="fail", reasons="no")
    assert failed.status_code == 200, failed.text
    held = gate(world)
    assert held["gated"] and not held["passed"]


def settle_merges(world) -> dict:
    from evo_agents.hub.server import merge_ancestry

    state = world.client.app.state
    return world.client.portal.call(partial(merge_ancestry.settle, state.engine, state.github_app))


def merge_record(world) -> dict:
    m = tables.merge_checks
    return sql(world.db, select(m.c.verdict))[0][0]["merged"]


def merged_pull(world, *, head: str = HEAD, sha: str = MERGE, on_main: bool = True):
    pull = world.github.pulls[(MINE.lower(), REPO, 7)]
    pull.state, pull.merged, pull.merge_sha, pull.merged_at, pull.head_sha = "closed", True, sha, MERGED_AT, head
    if on_main:
        world.github.commit(MINE, REPO, sha, MERGED_AT, {})


def notices(world) -> list:
    n = tables.notifications
    return sql(world.db, select(n.c.title, n.c.body).where(n.c.title.like("Merge of pull request%")))


def test_merge_ancestry_records_a_merge_at_the_judged_head_on_the_base_and_tells_nobody(world):
    passed_check(world)
    assert settle_merges(world) == {}, "an open pull request has no merge to read"
    merged_pull(world)
    assert settle_merges(world) == {"merged": 1}
    record = merge_record(world)
    assert (record["merge_sha"], record["merged_head"], record["on_base"], record["problems"]) == (
        MERGE,
        HEAD,
        True,
        [],
    )
    assert settle_merges(world) == {}, "a merge is read once"
    assert notices(world) == []


def test_merge_ancestry_a_merge_at_another_head_opens_a_notice_for_the_owner(world):
    passed_check(world)
    merged_pull(world, head=NEWER)
    assert settle_merges(world) == {"merged": 1, "notified": 1}
    record = merge_record(world)
    assert record["merged_head"] == NEWER and record["judged_head"] == HEAD
    ((title, body),) = notices(world)
    assert "pull request #7 of evo-agents" in title
    assert NEWER[:12] in body and HEAD[:12] in body


def test_merge_ancestry_a_merge_commit_off_the_base_opens_a_notice(world):
    passed_check(world)
    merged_pull(world, on_main=False)
    assert settle_merges(world) == {"merged": 1, "notified": 1}
    record = merge_record(world)
    assert record["on_base"] is False and record["compare"] == "missing"
    ((_, body),) = notices(world)
    assert "not on the base branch" in body


def test_merge_ancestry_judges_what_github_says_alone():
    from types import SimpleNamespace

    from evo_agents.hub.server import merge_ancestry

    assert merge_ancestry.problems(HEAD, HEAD, MERGE, "ahead") == []
    assert merge_ancestry.problems(HEAD, HEAD, MERGE, "identical") == []
    assert len(merge_ancestry.problems(HEAD, NEWER, MERGE, "ahead")) == 1
    assert len(merge_ancestry.problems(HEAD, HEAD, MERGE, "diverged")) == 1
    assert len(merge_ancestry.problems(HEAD, None, None, None)) == 2
    row = SimpleNamespace(head_sha=HEAD, base_branch="main")
    pull = {"merge_commit_sha": MERGE, "head": {"sha": HEAD}}
    from datetime import UTC, datetime

    now = datetime(2026, 10, 30, tzinfo=UTC)
    assert merge_ancestry.merged_record(row, pull, "behind", now)["on_base"] is False
