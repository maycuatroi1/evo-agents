"""The gate before merge of the hub's merge queue on GitLab (``evo_agents.hub.server.merge_gate`` and
``.merge_judge``; plan gitlab-merge-queue, criterion a4), against Postgres and the fake GitLab of
``tests.hub.fake_gitlab``: the real GitLab is never called.

The checks step 4 names: a merge request in scope (one the hub opens for a plan once its steps in the repo are done,
a person's labeled by a Developer, the Curator's its Judge passed) passes, in order, its pipeline on the exact head, the
repo and branch of its plan step, the scan for score hacking, the checks and the Judge (Claude, fresh context, each
dimension apart, the description against the diff among them), the risk, and for a high risk a second Judge; each
gate is a line of the ledger with its reason, and a merge request held shows why, on the route and once on GitLab.
No gate caps a diff. Out of scope, or in a project without a policy, nothing changes. The projects and people are
fictional.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.hub import judge, merge_gate, runs, tables
from evo_agents.hub.server import merge_gate as queue
from evo_agents.hub.server import post_merge as after_merge
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.sealing import KEY_BYTES
from tests.hub import test_credentials_api as leases_api
from tests.hub.fake_gitlab import FakeGitLab, sha_of
from tests.hub.live import sql
from tests.hub.test_runs import PROJECT, claim, moved

KB_HOST = "gitlab.lumenops.example"
GROUP = f"https://{KB_HOST}/fir-gb-lumen"
REPO = "lumen-kb-docs"
PATH = "fir-gb-lumen/lumen-kb-docs"
PLAN_ID = "lumen-report"
BRANCH = "plan/lumen-report"
VERIFY = "python -m pytest -q"
ADMIN = {"role": "admin", "max_level": "internal"}
CLEAN = [
    {
        "old_path": "kb/report.py",
        "new_path": "kb/report.py",
        "diff": "@@ -0,0 +1,2 @@\n+def total():\n+    return 3\n",
        "new_file": True,
        "renamed_file": False,
        "deleted_file": False,
    },
    {
        "old_path": "tests/test_report.py",
        "new_path": "tests/test_report.py",
        "diff": "@@ -0,0 +1,2 @@\n+def test_total():\n+    assert total() == 3\n",
        "new_file": True,
        "renamed_file": False,
        "deleted_file": False,
    },
]
MIGRATION = [
    {
        "old_path": "kb/migrations/0003_total.py",
        "new_path": "kb/migrations/0003_total.py",
        "diff": "@@ -0,0 +1 @@\n+op.add_column('reports', 'total')\n",
        "new_file": True,
        "renamed_file": False,
        "deleted_file": False,
    }
]
WEAKENED = [
    {
        "old_path": "tests/test_report.py",
        "new_path": "tests/test_report.py",
        "diff": "@@ -1,3 +1,2 @@\n def test_total():\n-    assert total() == 3\n+    pass\n",
        "new_file": False,
        "renamed_file": False,
        "deleted_file": False,
    }
]


def plan_body(steps_done: bool = False) -> dict:
    return {
        "id": PLAN_ID,
        "goal": "Ship the lumen report.",
        "repos": [{"repo": REPO, "branch": BRANCH}],
        "steps": [
            {
                "id": 1,
                "title": "Report",
                "repo": REPO,
                "what": "a report of the totals",
                "verify": VERIFY,
                "status": "done" if steps_done else "pending",
            }
        ],
    }


def taking(client, worker: dict, features=tuple(runs.WORKER_FEATURES)) -> dict:
    """``worker`` after a heartbeat that says it runs every kind of run and does ``features``."""
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": [],
        "agent_version": "0.7.0",
        "run_kinds": list(runs.RUN_KINDS),
        "features": list(features),
    }
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return worker


def put_policy(client, headers, policy: dict) -> None:
    response = client.put(f"/v1/projects/{PROJECT}/policy", json={"policy": policy}, headers=headers)
    assert response.status_code == 200, response.text


def auto(**judge_keys) -> dict:
    return {"repos": {REPO: {"mode": "auto", "target_branch": "dev"}}, "judge": {"worker": "mac-mini", **judge_keys}}


@pytest.fixture
def world(hub_db, tmp_path, github, monkeypatch):
    """test_credentials_api's project (lumen-kb-docs on GitLab among its repos), its hub holding the secrets key; owner
    an admin with a forge secret over the GitLab group and the worker mac-mini, which judges merge requests; the
    policy has lumen-kb-docs in mode auto with target branch dev; the fake GitLab holds its project, set up as the
    queue needs it. The audit sample's draw is 0.5: a merge is a sample only at a rate above it."""
    monkeypatch.setattr(after_merge, "draw", lambda: 0.5)
    config = live.hub_config(
        hub_db, tmp_path, github, secrets_key=secrets.token_bytes(KEY_BYTES), **live.web_changes(github)
    )
    token = "glpat-" + secrets.token_hex(16)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = leases_api.members(client, github)
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/owner", json=ADMIN, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
        worker = taking(client, leases_api.worker_of(client, headers["owner"], "mac-mini"))
        expires = (datetime.now(UTC) + timedelta(days=90)).isoformat()
        forge = {"kind": "forge", "url_prefix": GROUP, "projects": [PROJECT], "expires_at": expires, "value": token}
        assert client.put("/v1/secrets/gitlab-merge", json=forge, headers=headers["owner"]).status_code == 200
        put_policy(client, headers["owner"], auto())
        fake = FakeGitLab(token, host=KB_HOST)
        project = fake.add_project(PATH, protected={"dev": {"push": [0], "merge": [40]}})
        project.branches["dev"] = sha_of("dev")
        yield SimpleNamespace(
            client=client, headers=headers, worker=worker, fake=fake, project=project, db=hub_db, token=token
        )


def gate_pass(world) -> dict:
    """One pass of the job hub.merge_queue, GitLab being the fake."""
    state = world.client.app.state
    return world.client.portal.call(
        partial(queue.advance, state.engine, state.sealer, transport=world.fake.transport())
    )


def plan_done(world) -> int:
    """The plan pushed, and a run of its step on mac-mini ended done; the run's id."""
    owner = world.headers["owner"]
    pushed = world.client.put(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", json={"body": plan_body()}, headers=owner)
    assert pushed.status_code == 200, pushed.text
    body = {"plan_id": PLAN_ID, "steps": [1], "worker_id": world.worker["id"], "approval": "auto"}
    response = world.client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=owner)
    assert response.status_code == 201, response.text
    run_id = response.json()[0]["id"]
    assert claim(world.client, world.worker)["id"] == run_id
    verify = [{"command": VERIFY, "exit_code": 0}]
    moved(world.client, world.worker, run_id, "running", "verifying")
    moved(world.client, world.worker, run_id, "done", verify=verify, commit_sha=sha_of("work"))
    world.project.branches[BRANCH] = sha_of("work")
    return run_id


def green(world, merge_request, files=None, *, status: str = "success", message: str = "Add the lumen report"):
    """``merge_request`` with its diff, its head commit and a head pipeline of ``status`` on its head."""
    merge_request.files = list(CLEAN if files is None else files)
    merge_request.commits = [
        {"id": merge_request.sha, "title": message.splitlines()[0], "message": message, "parent_ids": []}
    ]
    merge_request.pipeline = world.fake.add_pipeline(
        world.project, merge_request.sha, merge_request.source_branch, status
    )


def listed(world, headers=None, **query) -> list[dict]:
    response = world.client.get(
        f"/v1/projects/{PROJECT}/merge-queue", params=query, headers=headers or world.headers["owner"]
    )
    assert response.status_code == 200, response.text
    return response.json()["requests"]


def one(world) -> dict:
    (found,) = listed(world)
    return found


def gates(entry: dict) -> list[tuple]:
    """(gate, passed) of each gate line of the ledger, the oldest first."""
    return [(event["gate"], event["passed"]) for event in reversed(entry["events"]) if event["kind"] == "gate"]


def judge_headers(world, spec: dict) -> dict:
    return {**world.worker["headers"], judge.JUDGE_KEY_HEADER: spec["curator"]["judge_key"]}


def dimensions(names=merge_gate.DIMENSIONS, **failed) -> dict:
    return {
        name: {"verdict": "fail" if name in failed else "pass", "reason": failed.get(name, "fine")} for name in names
    }


def judged(world, *, head: str, names=merge_gate.DIMENSIONS, verdict: str = "pass", **failed) -> tuple[dict, dict]:
    """The judge run the queue queued, claimed by mac-mini, its inputs read and its verdict posted, then ended
    done; (its claim, the verdict's answer)."""
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "judge" and not spec["plan_id"], spec
    headers = judge_headers(world, spec)
    inputs = world.client.get(f"/v1/worker/runs/{spec['id']}/merge-judge", headers=headers)
    assert inputs.status_code == 200, inputs.text
    moved(world.client, world.worker, spec["id"], "running")
    body = {
        "verdict": verdict,
        "reasons": "I read the diff against the plan.",
        "head_sha": head,
        "verify": [{"command": item, "exit_code": 0} for item in inputs.json()["verify"]],
        "hidden": [],
        "signs": [],
        "dimensions": dimensions(names, **failed),
    }
    answer = world.client.post(f"/v1/worker/runs/{spec['id']}/merge-verdict", json=body, headers=headers)
    assert answer.status_code == 200, answer.text
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    return spec, answer.json()


# A plan's merge request, through every gate


def test_merge_gate_opens_a_plans_merge_request_and_takes_it_through_every_gate(world):
    run_id = plan_done(world)
    assert gate_pass(world) == {"opened": 1}
    (merge_request,) = world.project.merge_requests.values()
    assert (merge_request.source_branch, merge_request.target_branch) == (BRANCH, "dev")
    assert merge_request.title == f"{PLAN_ID}: Ship the lumen report."
    assert f"Opened by the evo-agents hub for plan {PLAN_ID}" in merge_request.description
    entry = one(world)
    assert (entry["source"], entry["plan_id"], entry["steps"], entry["run_id"]) == ("plan", PLAN_ID, ["1"], run_id)
    assert entry["state"] == "gating" and entry["reason"] == "gate 1 (pipeline): no pipeline ran on its head yet"
    assert world.project.notes == []  # a wait for the pipeline is no reason to comment

    green(world, merge_request)
    assert gate_pass(world) == {"judging": 1}
    entry = one(world)
    assert entry["state"] == "judging" and entry["fingerprint"] and entry["risk"] == {"high": False, "reasons": []}
    assert [gate for gate, passed in gates(entry) if passed] == [0, 1, 2, 3]

    r = tables.runs
    (run,) = sql(
        world.db, select(r.c.kind, r.c.plan_id, r.c.runtime, r.c.model, r.c.pinned_worker_id).where(r.c.kind == "judge")
    )
    assert tuple(run) == ("judge", None, "claude-code", "sonnet", world.worker["id"])  # never the Builder's model
    spec = claim(world.client, world.worker)
    curator = spec["curator"]
    assert (curator["role"], curator["change_id"], curator["forge"]) == ("judge", None, "gitlab")
    assert (curator["head_sha"], curator["branch"], curator["base_branch"]) == (merge_request.sha, BRANCH, "dev")
    assert "Merge request !1" in spec["prompt"] and all(
        f"- {name}:" in spec["prompt"] for name in merge_gate.DIMENSIONS
    )
    assert "Builder's transcript" not in spec["prompt"] and "evidence" not in spec["prompt"].lower()

    no_key = world.client.get(f"/v1/worker/runs/{spec['id']}/merge-judge", headers=world.worker["headers"])
    assert no_key.status_code == 403  # the worker token alone reads no hidden check
    inputs = world.client.get(f"/v1/worker/runs/{spec['id']}/merge-judge", headers=judge_headers(world, spec))
    assert inputs.status_code == 200 and inputs.headers["cache-control"] == "no-store"
    assert (inputs.json()["verify"], inputs.json()["hidden_checks"], inputs.json()["second"]) == ([VERIFY], [], False)
    moved(world.client, world.worker, spec["id"], "running")
    body = {
        "verdict": "pass",
        "reasons": "It adds the report and its test.",
        "head_sha": merge_request.sha,
        "verify": [{"command": VERIFY, "exit_code": 0}],
        "dimensions": dimensions(),
    }
    answer = world.client.post(
        f"/v1/worker/runs/{spec['id']}/merge-verdict", json=body, headers=judge_headers(world, spec)
    )
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"queue_id": entry["id"], "passed": True, "state": "ready", "reason": None}

    entry = one(world)
    assert entry["state"] == "ready" and entry["queued_at"] is not None and entry["judge_run_id"] is None
    assert gates(entry) == [(1, False), *((gate, True) for gate in range(8))]  # it waited for its pipeline first
    kinds = [event["kind"] for event in reversed(entry["events"])]
    assert kinds[0] == "opened" and "judged" in kinds and kinds[-1] == "queued"
    verdict = next(event for event in entry["events"] if event["kind"] == "judged")
    assert verdict["detail"]["model"] == "sonnet" and set(verdict["detail"]["dimensions"]) == set(merge_gate.DIMENSIONS)
    assert entry["verdict"]["first"]["passed"] is True and "second" not in entry["verdict"]
    audit = tables.audit
    actions = [row[0] for row in sql(world.db, select(audit.c.action).where(audit.c.action.like("merge.%")))]
    assert sorted(actions) == ["merge.judge", "merge.open"]
    assert world.token not in str(entry) and world.token not in str(spec)
    assert gate_pass(world) == {"merged": 1}  # ready, it takes its turn: tests/hub/test_merge_queue.py


def test_merge_gate_a_reader_sees_the_queue_and_a_stranger_does_not(world, github):
    plan_done(world)
    gate_pass(world)
    reader = live.bearer(live.sign_in(world.client, github, "lumen-reader", 777)["token"])
    assert world.client.get(f"/v1/projects/{PROJECT}/merge-queue", headers=reader).status_code == 404
    granted = world.client.put(
        f"/v1/admin/projects/{PROJECT}/grants/lumen-reader",
        json={"role": "reader", "max_level": "internal"},
        headers=world.headers["admin"],
    )
    assert granted.status_code == 200
    assert [entry["iid"] for entry in listed(world, reader)] == [1]
    assert listed(world, reader, repo="notes") == []


# What holds a merge request


def test_merge_gate_a_red_pipeline_blocks_it_with_why_once_and_a_new_push_gates_it_again(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request, status="failed")
    gate_pass(world)
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked" and entry["reason"] == "gate 1 (pipeline): its head pipeline ended failed"
    assert len(world.project.notes) == 1  # once, however many passes read it
    assert "is blocked at gate 1 (pipeline)" in world.project.notes[0][1]
    assert sql(world.db, select(tables.runs.c.id).where(tables.runs.c.kind == "judge")) == []

    merge_request.sha = sha_of("fixed")
    green(world, merge_request)
    assert gate_pass(world) == {"judging": 1}
    entry = one(world)
    assert entry["state"] == "judging" and entry["head_sha"] == merge_request.sha
    assert any(event["kind"] == "head" for event in entry["events"])


def test_merge_gate_a_skip_ci_head_is_not_green(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request, message="Add the lumen report\n\n[skip ci]")
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked" and "asks CI to skip it ([skip ci])" in entry["reason"]


def test_merge_gate_a_pipeline_that_never_ends_blocks_it_after_pipeline_minutes(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request, status="running")
    gate_pass(world)
    assert one(world)["state"] == "gating" and one(world)["reason"] == "gate 1 (pipeline): its head pipeline is running"
    q = tables.merge_queue
    sql(world.db, update(q).values(head_at=datetime.now(UTC) - timedelta(minutes=61)))
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked" and entry["reason"].endswith("is running after 60 minutes")


def test_merge_gate_the_project_settings_hold_it_at_gate_0_with_a_comment(world):
    world.project.merge_method = "merge"
    world.project.protected["dev"] = {"push": [30], "merge": [30]}
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "gating" and entry["reason"].startswith(
        "gate 0 (repo checked): the GitLab project its merge"
    )
    assert "Allowed to push and merge" in entry["reason"] and "Allowed to merge" in entry["reason"]
    assert len(world.project.notes) == 1 and "waits at gate 0" in world.project.notes[0][1]


def test_merge_gate_signs_of_score_hacking_block_it_before_any_judge(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request, WEAKENED)
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked" and entry["reason"].startswith("gate 3 (score hacking): the diff shows signs")
    assert "assert_removed" in entry["reason"] and "tests/test_report.py:2" in entry["reason"]
    assert sql(world.db, select(tables.runs.c.id).where(tables.runs.c.kind == "judge")) == []


def test_merge_gate_a_branch_other_than_the_plans_is_held_at_gate_2(world):
    plan_done(world)
    gate_pass(world)
    plan = plan_body(steps_done=True)
    plan["repos"] = [{"repo": REPO, "branch": "plan/another"}]
    owner = world.headers["owner"]
    held = world.client.get(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", headers=owner).json()
    body = {"body": plan, "if_revision": held["revision"]}
    replaced = world.client.put(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", json=body, headers=owner)
    assert replaced.status_code == 200, replaced.text
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked"
    assert (
        entry["reason"]
        == f"gate 2 (repo of the plan step): its source branch is {BRANCH}, and its plan step works on plan/another"
    )


def test_merge_gate_a_dimension_the_judge_fails_blocks_it(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    gate_pass(world)
    _, answer = judged(world, head=merge_request.sha, description="it says nothing of the totals it adds")
    assert answer["passed"] is False and answer["state"] == "blocked"
    entry = one(world)
    assert entry["reason"] == "gate 5 (Judge): the Judge failed description: it says nothing of the totals it adds"
    verdict = next(event for event in entry["events"] if event["kind"] == "judged")
    assert verdict["passed"] is False and verdict["detail"]["dimensions"]["description"]["verdict"] == "fail"
    gate_pass(world)
    assert len(world.project.notes) == 1 and "gate 5 (Judge)" in world.project.notes[0][1]


def test_merge_gate_a_failed_check_of_the_judge_run_blocks_it_at_gate_4(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    gate_pass(world)
    spec = claim(world.client, world.worker)
    headers = judge_headers(world, spec)
    world.client.get(f"/v1/worker/runs/{spec['id']}/merge-judge", headers=headers)
    moved(world.client, world.worker, spec["id"], "running")
    body = {
        "verdict": "pass",
        "head_sha": merge_request.sha,
        "verify": [{"command": VERIFY, "exit_code": 1}],
        "dimensions": dimensions(),
    }
    answer = world.client.post(f"/v1/worker/runs/{spec['id']}/merge-verdict", json=body, headers=headers)
    assert answer.json()["state"] == "blocked"
    assert one(world)["reason"] == "gate 4 (checks): 1 verify command(s) of the plan exited other than 0"


def test_merge_gate_judge_runs_that_end_without_a_verdict_block_it_after_three(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    gate_pass(world)
    for attempt in range(1, judge.JUDGE_ATTEMPTS + 1):
        spec = claim(world.client, world.worker)
        assert spec["kind"] == "judge", attempt
        moved(world.client, world.worker, spec["id"], "running")
        moved(world.client, world.worker, spec["id"], "failed", error="the agent stopped")
        gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked" and entry["reason"].startswith(
        "gate 5 (Judge): 3 judge runs ended without a verdict"
    )


# The risk and the second Judge


def test_merge_gate_a_high_risk_needs_a_second_judge_on_opus(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request, MIGRATION)
    gate_pass(world)
    assert one(world)["risk"]["high"] is True
    _, answer = judged(world, head=merge_request.sha)
    assert answer["state"] == "judging" and answer["reason"] == "gate 7 (second Judge): waits for the second Judge"
    risk = next(event for event in one(world)["events"] if event["gate"] == 6)
    assert risk["passed"] and "the risk is high" in risk["reason"] and "kb/migrations/0003_total.py" in risk["reason"]

    assert gate_pass(world) == {"judging": 1}
    spec = claim(world.client, world.worker)
    assert spec["model"] == "opus" and "# Why its risk is high" in spec["prompt"]
    assert all(f"- {name}:" in spec["prompt"] for name in merge_gate.RISK_DIMENSIONS)
    headers = judge_headers(world, spec)
    inputs = world.client.get(f"/v1/worker/runs/{spec['id']}/merge-judge", headers=headers)
    assert inputs.json()["second"] is True
    moved(world.client, world.worker, spec["id"], "running")
    body = {
        "verdict": "pass",
        "head_sha": merge_request.sha,
        "verify": [{"command": VERIFY, "exit_code": 0}],
        "dimensions": dimensions(merge_gate.RISK_DIMENSIONS, migration="it drops a column it cannot bring back"),
    }
    answer = world.client.post(f"/v1/worker/runs/{spec['id']}/merge-verdict", json=body, headers=headers)
    assert answer.json()["state"] == "blocked"
    assert (
        one(world)["reason"]
        == "gate 7 (second Judge): the Judge failed migration: it drops a column it cannot bring back"
    )


def test_merge_gate_a_second_judge_that_passes_lets_it_in(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request, MIGRATION)
    gate_pass(world)
    judged(world, head=merge_request.sha)
    gate_pass(world)
    _, answer = judged(world, head=merge_request.sha, names=merge_gate.RISK_DIMENSIONS)
    assert answer["state"] == "ready"
    entry = one(world)
    assert entry["verdict"]["second"]["passed"] is True and entry["verdict"]["second"]["model"] == "opus"


def test_merge_gate_a_head_with_the_same_patch_keeps_the_verdict(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    gate_pass(world)
    judged(world, head=merge_request.sha)
    assert one(world)["state"] == "ready"
    merge_request.sha = sha_of("rebased")  # the same patch on a newer dev
    moved_files = [
        {**item, "diff": item["diff"].replace("@@ -0,0 +1,2 @@", "@@ -0,0 +1,2 @@ class Kb:")} for item in CLEAN
    ]
    green(world, merge_request, moved_files)
    assert gate_pass(world) == {"ready": 1, "merged": 1}  # ready again, it takes its turn at once
    entry = one(world)
    assert entry["state"] == "merged" and entry["head_sha"] == merge_request.sha
    assert len(sql(world.db, select(tables.runs.c.id).where(tables.runs.c.kind == "judge"))) == 1
    kept = [event for event in entry["events"] if event["gate"] == 5 and event["reason"]]
    assert kept and "its verdict holds" in kept[0]["reason"]


# Who comes in


def test_merge_gate_takes_a_persons_merge_request_labeled_by_a_developer_alone(world):
    world.project.members.update({41: 30, 42: 20})
    labeled = world.fake.add_merge_request(world.project, "fix/kb-typo", target_branch="dev", title="Fix a typo")
    world.fake.label(labeled, "evo-merge", 42)  # a Reporter
    assert gate_pass(world) == {}
    assert listed(world) == []
    world.fake.label(labeled, "evo-merge", 41)  # a Developer
    assert gate_pass(world) == {"taken": 1}
    green(world, labeled)
    gate_pass(world)
    entry = one(world)
    assert (entry["source"], entry["state"]) == ("label", "judging")
    assert (2, True) in gates(entry)  # a person's merge request has no plan step: gate 2 passes
    spec = claim(world.client, world.worker)
    assert spec["model"] is None  # a person's: the runtime's default model
    assert "judge it by what its description says" in spec["prompt"]
    world.fake.label(labeled, "evo-merge", 41, "remove")
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "closed" and "opt-in label" in entry["reason"]


def test_merge_gate_a_draft_waits_and_a_merge_request_into_another_branch_is_not_taken(world):
    world.project.members[41] = 30
    elsewhere = world.fake.add_merge_request(world.project, "fix/main", target_branch="main")
    world.fake.label(elsewhere, "evo-merge", 41)
    draft = world.fake.add_merge_request(world.project, "fix/draft", target_branch="dev", draft=True)
    world.fake.label(draft, "evo-merge", 41)
    assert gate_pass(world) == {"taken": 1}
    entry = one(world)
    assert entry["iid"] == draft.iid and entry["reason"] == "waits: it is a draft"


def test_merge_gate_without_a_policy_or_a_forge_secret_nothing_changes(world):
    put_policy(world.client, world.headers["owner"], {})
    plan_done(world)
    assert gate_pass(world) == {}
    assert world.fake.requests == [] and listed(world) == []
    put_policy(world.client, world.headers["owner"], auto())
    assert world.client.delete("/v1/secrets/gitlab-merge", headers=world.headers["owner"]).status_code in (200, 204)
    assert gate_pass(world) == {"no_forge": 1}
    assert world.fake.requests == [] and listed(world) == []


def test_merge_gate_a_plan_done_before_the_repo_went_auto_gets_no_merge_request(world):
    put_policy(world.client, world.headers["owner"], {})
    plan_done(world)
    put_policy(world.client, world.headers["owner"], auto())
    assert gate_pass(world) == {}
    assert world.project.merge_requests == {}


def test_merge_gate_turning_the_repo_off_hands_its_merge_requests_back_to_people(world):
    plan_done(world)
    gate_pass(world)
    put_policy(world.client, world.headers["owner"], {"repos": {REPO: {"mode": "off"}}})
    assert gate_pass(world) == {"closed": 1}
    entry = one(world)
    assert entry["state"] == "closed" and f"the policy has {REPO} in mode off" in entry["reason"]
    assert world.project.merge_requests[1].state == "opened"  # the merge request itself stays as it is


def test_merge_gate_a_worker_whose_daemon_does_not_judge_merge_requests_gets_no_judge_run(world):
    taking(world.client, world.worker, features=[f for f in runs.WORKER_FEATURES if f != runs.MERGE_JUDGE])
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "judging"
    assert entry["reason"] == (
        "gate 5 (Judge): waits: the daemon of worker mac-mini does not say it judges merge requests: upgrade it and "
        "restart it"
    )


def test_merge_gate_a_policy_without_a_judge_worker_or_charter_says_so(world):
    put_policy(world.client, world.headers["owner"], {"repos": {REPO: {"mode": "auto", "target_branch": "dev"}}})
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    gate_pass(world)
    assert one(world)["reason"] == (
        "gate 5 (Judge): waits: the policy names no judge.worker and the project has no charter with a worker on duty"
    )


def test_merge_gate_a_task_runs_branch_gets_its_merge_request(world):
    from sqlalchemy import insert

    from tests.hub.test_plugin_runtimes import install
    from tests.hub.test_task_runs import CONTRIBUTES, DISPATCH, PLUGIN, enable_plugin, enabled

    install(world.db, name=PLUGIN, contributes=CONTRIBUTES, permissions=[DISPATCH])
    enable_plugin(world.client, world.headers["owner"], PLUGIN, [DISPATCH])
    enabled(world.client, world.headers["owner"])
    agent_type_id = sql(world.db, select(tables.project_agent_types.c.id))[0][0]
    r = tables.runs
    owner_id = sql(world.db, select(tables.users.c.id).where(tables.users.c.login == "owner"))[0][0]
    project_id = sql(world.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    now = datetime.now(UTC)
    sql(
        world.db,
        insert(r).values(
            project_id=project_id,
            kind="task",
            agent_type_id=agent_type_id,
            title="Index the kb",
            request="Index every page of the kb.",
            dispatched_by=owner_id,
            dispatched_via="machine",
            requested_runtime="claude-code",
            runtime="claude-code",
            mode="headless",
            approval="auto",
            timeout_s=600,
            state="done",
            worker_id=world.worker["id"],
            queued_at=now,
            leased_at=now,
            finished_at=now,
        ),
    )
    (task_id,) = sql(world.db, select(r.c.id).where(r.c.kind == "task"))[0]
    sql(
        world.db,
        insert(tables.run_events).values(
            run_id=task_id, seq=1, kind="system", body={"text": "pushed", "pull": {"repo": REPO, "url": None}}
        ),
    )
    world.project.branches[f"task/{task_id}"] = sha_of("task")
    assert gate_pass(world) == {"opened": 1}
    entry = one(world)
    assert (entry["source"], entry["run_id"], entry["source_branch"]) == ("task", task_id, f"task/{task_id}")
    assert world.project.merge_requests[1].title == "Index the kb"
