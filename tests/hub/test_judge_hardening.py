"""The fixes of the security review of step 6 of the curator-agent plan, on the hub, each test failing on what the hub
did before them (evo-agents a1728a8): the world, GitHub and its helpers are ``tests.hub.test_curator_changes``'s.

- C1: a judge run's routes take the run's own key, which its claim hands the daemon and the hub keeps as a SHA-256;
  the worker token alone reads no hidden check and writes no verdict.
- H1: the tier of a proposal is computed again from the files its pull request (or its judge run's diff) really
  touches, and only raised; a Curator's plan is written in the progress of its steps alone; a plan id of the Curator's
  is the hub's alone (L3).
- M4: CI is green only once the checks the default branch's ruleset requires passed, from the App it names; never when
  the ruleset requires none, nor when the head commit's message asks CI to skip it.
- L1: a list of files GitHub cuts short fails closed. L2: a Builder's lease checks the ruleset again. L4: a paused night
  shift merges nothing.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import judge, tables
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, app_key, leased  # noqa: F401 (app_key is a fixture)
from tests.hub.test_curator import NIGHT, PROJECT
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    BRANCH,
    CLEAN,
    GREEN,
    HEAD,
    PLAN_ID,
    RULESET,
    SUCCESS,
    accepted,
    builder,
    calls,
    fire,
    inputs,
    judge_run,
    judged,
    moved,
    moved_on,
    one_change,
    path,
    proposal_of,
    protect,
    through_the_judge,
    verdict,
    world,
)
from tests.hub.test_runs import claim

PLAN_PATH = f"/v1/projects/{PROJECT}/plans"


def repo(world):  # noqa: F811
    return world.github.repos[(MINE.lower(), "evo-agents")]


# C1: the judge run's own key


def test_judge_routes_refuse_the_worker_token_without_the_runs_key(world):  # noqa: F811
    accepted(world)
    builder(world)
    assert moved_on(world) == {"opened": 1}
    spec = judge_run(world)
    key = spec["curator"]["judge_key"]
    assert isinstance(key, str) and len(key) >= 32
    (stored,) = sql(world.db, select(tables.curator_changes.c.judge_key))
    assert stored[0] == hashlib.sha256(key.encode()).hexdigest()  # the hub keeps its digest alone
    route = f"/v1/worker/runs/{spec['id']}"
    for extra in ({}, {judge.JUDGE_KEY_HEADER: "not-the-key"}):
        headers = {**world.worker["headers"], **extra}
        refused = world.client.get(f"{route}/judge", headers=headers)
        assert refused.status_code == 403, refused.text
        assert judge.JUDGE_KEY_HEADER in refused.text and "worker token alone" in refused.text
        assert "tests/hidden" not in refused.text
        body = {"verdict": "pass", "head_sha": HEAD, "verify": [], "hidden": [], "signs": []}
        assert world.client.post(f"{route}/verdict", json=body, headers=headers).status_code == 403
    assert one_change(world)["state"] == "judging"  # nothing was judged by the worker token alone
    assert inputs(world, spec["id"]).status_code == 200
    assert verdict(world, spec["id"]).status_code == 200
    (stored,) = sql(world.db, select(tables.curator_changes.c.judge_key))
    assert stored[0] is None  # the key goes with the verdict
    assert verdict(world, spec["id"]).status_code == 409


# H1: the tier again from the files, and the plan the hub's


def test_merge_a_docs_proposal_whose_pull_request_changes_code_is_tier_1_and_stays_open(world):  # noqa: F811
    proposal = accepted(world, kind="docs", paths=[{"repo": "evo-agents", "path": "docs/wait.md"}])
    assert proposal["tier"] == 0
    files = [
        {"filename": "docs/wait.md", "status": "added", "patch": "@@ -0,0 +1 @@\n+# Wait\n"},
        {"filename": "evo_agents/hub/runs.py", "status": "modified", "patch": "@@ -1 +1 @@\n-A = 1\n+A = 2\n"},
    ]
    builder(world, files)
    assert moved_on(world) == {"opened": 1}
    assert one_change(world)["tier"] == 1
    found = proposal_of(world, proposal["id"])
    assert found["tier"] == 1 and "evo-agents:evo_agents/hub/runs.py is code" in " ".join(found["tier_reasons"])
    assert found["tier_reasons"][-1] == "the files the change really touches give it tier 1, not 0"
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    change = one_change(world)
    assert change["state"] == "open" and change["reason"] == "tier 1 waits for its owner to merge it"
    assert world.github.merges == []


def test_judge_the_paths_of_a_judge_runs_diff_raise_the_tier_and_never_lower_it(world):  # noqa: F811
    proposal = accepted(world)
    builder(world)
    moved_on(world)
    spec = judge_run(world)
    assert inputs(world, spec["id"]).status_code == 200
    response = verdict(world, spec["id"], paths=["tests/test_wait.py", "evo_agents/worker/wait.py"])
    assert response.status_code == 200, response.text
    assert response.json()["tier"] == 1
    assert proposal_of(world, proposal["id"])["tier"] == 1
    moved(world.client, world.worker, spec["id"], "running", "verifying", "done")


def test_curator_plan_writes_of_more_than_its_progress_are_refused(world):  # noqa: F811
    accepted(world)
    owner = world.headers["owner"]
    plan = world.client.get(f"{PLAN_PATH}/{PLAN_ID}", headers=owner).json()
    step = plan["body"]["steps"][0]
    for change in (
        {"context": "Do it some other way."},
        {"title": "Another title"},
        {"steps": [{**step, "what": "Anything at all."}]},
        {"non_goals": ["the tests"]},
    ):
        put = {"body": {**plan["body"], **change}, "if_revision": plan["revision"]}
        refused = world.client.put(f"{PLAN_PATH}/{PLAN_ID}", json=put, headers=owner)
        assert refused.status_code == 409, (change, refused.text)
        assert "status, done_at, evidence and note of its steps" in refused.text
    progress = {"status": "blocked", "evidence": "CI is red", "note": "waits"}
    patch = {"section": "steps", "step": "1", "updates": progress, "if_revision": plan["revision"]}
    assert world.client.patch(f"{PLAN_PATH}/{PLAN_ID}", json=patch, headers=owner).status_code == 200


def test_curator_plan_ids_are_made_by_the_hub_alone(world):  # noqa: F811
    """L3: a plan a member names curator-<proposal>-<slug> ahead of the hub would leave the accepted change stuck."""
    owner = world.headers["owner"]
    body = {"id": PLAN_ID, "title": "Mine", "steps": [{"id": 1, "what": "anything", "status": "pending"}]}
    refused = world.client.put(f"{PLAN_PATH}/{PLAN_ID}", json={"body": body}, headers=owner)
    assert refused.status_code == 409 and "belong to the Curator" in refused.text
    other = {**body, "id": "curator-99-mine"}
    assert world.client.put(f"{PLAN_PATH}/curator-99-mine", json={"body": other}, headers=owner).status_code == 409
    mine = {"body": {**body, "id": "my-curator-notes"}}
    assert world.client.put(f"{PLAN_PATH}/my-curator-notes", json=mine, headers=owner).status_code == 200
    accepted(world)
    change = one_change(world)
    assert (change["state"], change["plan_id"]) == ("planned", PLAN_ID)


# M4: the checks the ruleset requires


def test_merge_waits_for_the_checks_the_ruleset_requires_whatever_else_passed(world):  # noqa: F811
    through_the_judge(world)
    third_party = [{"name": "lint", "status": "completed", "conclusion": "success", "app": {"id": 777}}]
    world.github.ci(HEAD, runs=third_party, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "wait": 1}
    change = one_change(world)
    assert change["state"] == "judged" and change["reason"] == "the required check test has not reported"
    assert world.github.merges == []


def test_merge_a_required_check_of_another_app_does_not_count(world):  # noqa: F811
    repo(world).rulesets = [{**RULESET, "required_checks": [{"context": "test", "integration_id": 15368}]}]
    through_the_judge(world)
    spoofed = [{"name": "test", "status": "completed", "conclusion": "success", "app": {"id": 999}}]
    world.github.ci(HEAD, runs=spoofed, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    assert one_change(world)["reason"] == "test is required from App 15368, and another App wrote it"
    assert world.github.merges == []


def test_merge_never_when_the_ruleset_requires_no_check(world):  # noqa: F811
    repo(world).rulesets = [{key: value for key, value in RULESET.items() if key != "required_checks"}]
    through_the_judge(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    assert "requires no status check" in one_change(world)["reason"] and world.github.merges == []


@pytest.mark.parametrize("message", ["Tidy the helper [skip ci]", "Tidy\n\nskip-checks: true", "[ci skip] tidy"])
def test_merge_never_when_the_head_commit_skips_ci(world, message):  # noqa: F811
    accepted(world)
    builder(world)
    world.github.push(MINE, "evo-agents", BRANCH, HEAD, message=message)
    assert moved_on(world) == {"opened": 1}
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    assert "asks CI to skip it" in one_change(world)["reason"] and world.github.merges == []


# L1: a list GitHub cuts short


@pytest.mark.parametrize("cut", ["claimed", "pages"])
def test_hack_a_pull_request_whose_files_github_cuts_short_fails_closed(world, monkeypatch, cut):  # noqa: F811
    from evo_agents.hub.server import pulls

    proposal = accepted(world)
    if cut == "pages":
        monkeypatch.setattr(pulls, "MAX_PAGES", 1)
        files = [
            {"filename": f"tests/test_{n}.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+x = 1\n"}
            for n in range(100)
        ]
    else:
        files = CLEAN
        repo(world).claimed_files[BRANCH] = 5000
    builder(world, files)
    assert moved_on(world) == {"signs": 1}
    change = one_change(world)
    assert (change["state"], change["passed"], change["tier"]) == ("judged", False, 3)
    (sign,) = [item for item in change["verdict"]["signs"] if item["kind"] == "diff_unreadable"]
    assert sign["path"] == "(the pull request's files)"
    assert proposal_of(world, proposal["id"])["tier"] == 3


# L2: the ruleset again when a Builder's lease is issued


def test_protection_a_builders_lease_checks_the_ruleset_again(world):  # noqa: F811
    accepted(world)
    assert protect(world).json()["protected"] is True
    assert fire(world.client, NIGHT)["builder"] == 1
    spec = claim(world.client, world.worker)
    repo(world).rulesets = [{**RULESET, "bypass": {"workers", "curator"}}]  # changed since the check
    got = leased(world.client, world.worker, spec["id"])
    assert [item for item in got["leases"] if item["provider"] == "github-app"] == []
    (missing,) = [item for item in got["missing"] if item["repo"] == "evo-agents"]
    assert "no longer keeps the Curator's App off its default branch" in missing["reason"]
    rc = tables.curator_repo_checks
    assert sql(world.db, select(rc.c.protected, rc.c.checked_by)) == [(False, None)]
    assert world.github.calls("/app/installations/1001/access_tokens") == []  # never the workers' App either
    repo(world).rulesets = [dict(RULESET)]
    got = leased(world.client, world.worker, spec["id"])
    assert [item["provider"] for item in got["leases"]] == ["github-app"]


# L4: a paused night shift merges nothing


def test_merge_nothing_while_the_night_shift_is_paused(world):  # noqa: F811
    through_the_judge(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    paused = world.client.post(path("pause"), headers=world.headers["owner"])
    assert paused.status_code == 200, paused.text
    assert moved_on(world) == {"checked": 1, "paused": 1}
    change = one_change(world)
    assert change["state"] == "judged" and "night shift of the project is paused" in change["reason"]
    assert world.github.merges == [] and calls(world.github, "PUT", "/merge") == []
    assert world.client.post(path("resume"), headers=world.headers["owner"]).status_code == 200
    assert moved_on(world) == {"merged": 1}
    assert len(world.github.merges) == 1
