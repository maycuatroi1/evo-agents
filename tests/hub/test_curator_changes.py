"""The Curator's changes on the hub (``evo_agents.hub.server.changes``): an accepted proposal of tier 0 or 1 becomes the
Curator's plan on a branch of its own, the night shift builds it with a token of the Curator's App, the hub opens its
pull request and queues its Judge, the Judge reads the project's hidden checks alone and posts its verdict, and the hub
merges a tier 0 change with the workers' App, at the head the Judge passed, only when everything allows it; and schema
0017. GitHub is the fake of ``tests.hub.fake_github``, with the workers' App and the Curator's App installed.

The checks step 6 of the curator-agent plan names on the hub: a8 (the plan, the branch, the pull request, the judge run
that reads no transcript, its runtime by the project's policy), a9 (the merge at the head the Judge passed, with CI
green, no protected path, a checked repo and tier 0 in auto_merge; tier 1, a protected path, an unchecked repo and a
GitLab merge request stay open), and a10 (the Curator's token is never the workers' App's, a Builder waits for a checked
ruleset, a worker token reads no hidden check and writes no charter, a sign of score hacking puts the proposal at tier 3
and fails the Judge). And a12: a member's own plan run still gets the workers' App's token and its default branch."""

from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from evo_agents.hub import judge, runs, tables
from evo_agents.hub.curator_cli import CHANGE_KEYS
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.changes import JudgeInputs, advance
from evo_agents.hub.worker import queue
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_credentials_api import (  # noqa: F401 (app_key is a fixture)
    MINE,
    app_key,
    env_secret,
    git_secret,
    install,
    leased,
)
from tests.hub.test_credentials_api import hub_config as credentials_config
from tests.hub.test_credentials_api import members as credential_members
from tests.hub.test_credentials_api import worker_of as credential_worker
from tests.hub.test_curator import NIGHT, PROJECT, THE_NIGHT, charter_body, fire, grant, put_charter, written
from tests.hub.test_migrate import move_to
from tests.hub.test_plan_runs import reported
from tests.hub.test_review_runs import DRAFT, MCP_HEADERS, answer, answered, collected, proposed
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import claim, moved

WORKERS_APP = {"contents": "write", "metadata": "read", "pull_requests": "write", "checks": "write", "statuses": "read"}
RULESET = {
    "id": 11,
    "enforcement": "active",
    "rules": ["update", "deletion", "non_fast_forward"],
    "bypass": {"workers"},
    "required_checks": ["test"],  # what a merge needs to have passed
}
HEAD = "1" * 40
MOVED = "2" * 40
BRANCH = "curator/1-curator-fix-sleep"
PLAN_ID = "curator-1-curator-fix-sleep"
VERIFY = DRAFT["steps"][0]["verify"]
HIDDEN = ["python -m pytest -q tests/hidden"]
CLEAN = [
    {
        "filename": "tests/test_wait.py",
        "status": "added",
        "patch": "@@ -0,0 +1,2 @@\n+def test_wait():\n+    assert wait_for()\n",
    }
]
GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]
SUCCESS = {"state": "success", "total_count": 1, "statuses": [{"state": "success", "context": "ci"}]}


def taking(client, worker: dict) -> dict:
    """``worker`` after a heartbeat that says its daemon runs every kind of run, judge runs included."""
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": [],
        "agent_version": "0.7.0",
        "run_kinds": list(runs.RUN_KINDS),
    }
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return worker


def curator_charter(**changes) -> dict:
    return charter_body(worker="mac-mini", night_plans=[], max_runs_per_night=20, night_budget_usd=5.0, **changes)


@pytest.fixture
def world(hub_db, tmp_path, github, app_key):  # noqa: F811
    """test_credentials_api's project (evo-agents on GitHub, m1-kb-docs on GitLab, two more repos), its hub holding the
    secrets key, the workers' App and the Curator's App, both installed on evo-agents, whose default branch has a
    ruleset only the workers' App bypasses; owner an admin with a charter whose worker on duty, mac-mini, runs every
    kind of run."""
    config = credentials_config(hub_db, tmp_path, github, app_key, curator=True)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = credential_members(client, github)
        grant(client, headers, "owner", "admin")
        worker = taking(client, credential_worker(client, headers["owner"], "mac-mini"))
        written(client, headers["owner"], curator_charter())
        install(github, "evo-agents", permissions=WORKERS_APP)
        install(github, "evo-agents", app="curator")
        github.add_repo(MINE, "evo-agents", rulesets=[dict(RULESET)])
        yield SimpleNamespace(client=client, headers=headers, worker=worker, github=github, db=hub_db, app=client.app)


def path(*parts: str) -> str:
    return "/".join((f"/v1/projects/{PROJECT}/curator", *parts))


def moved_on(world) -> dict:
    """One pass of the job curator.changes."""
    state = world.app.state
    return world.client.portal.call(partial(advance, state.engine, state.github_app, state.curator_app))


def review_run(world) -> int:
    assert collected(world.client, NIGHT)["review"] == 1
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "review" and spec["curator"]["role"] == "reviewer"
    moved(world.client, world.worker, spec["id"], "running")
    return spec["id"]


def accepted(world, *, kind: str = "test_add", paths=None, plan=None, action: str = "accept") -> dict:
    """A proposal of the night's review run, the run ended, and the proposal answered by owner; the proposal."""
    run_id = review_run(world)
    made = proposed(
        world.client,
        world.worker,
        run_id,
        kind=kind,
        paths=paths if paths is not None else [{"repo": "evo-agents", "path": "tests/test_wait.py"}],
        evidence=[{"kind": "run", "run_id": run_id, "seq": 1}],
        plan=plan or DRAFT,
    )
    moved(world.client, world.worker, run_id, "verifying", "done")
    return answered(world.client, world.headers["owner"], made["id"], action)


def changes(world, headers=None) -> list[dict]:
    response = world.client.get(path("changes"), headers=headers or world.headers["owner"])
    assert response.status_code == 200, response.text
    return response.json()["changes"]


def protect(world, repo: str = "evo-agents", headers=None):
    return world.client.post(path("protection", repo, "check"), headers=headers or world.headers["owner"])


def builder(world, files=None) -> int:
    """The Builder of the planned change: queued by the night shift, claimed, its step done, ended done; then the
    branch on the fake GitHub at HEAD with ``files``. Its run's id."""
    assert protect(world).json()["protected"] is True
    assert fire(world.client, NIGHT)["builder"] == 1
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "plan" and spec["plan_id"] == PLAN_ID and spec["curator"]["role"] == "builder"
    run_id = spec["id"]
    moved(world.client, world.worker, run_id, "running")
    verify = [{"command": VERIFY, "exit_code": 0}]
    reported(world.client, world.worker, run_id, 1, "done", repo="evo-agents", verify=verify, commit_sha=HEAD)
    moved(world.client, world.worker, run_id, "verifying", "done")
    world.github.push(MINE, "evo-agents", BRANCH, HEAD, files=CLEAN if files is None else files)
    return run_id


def judge_run(world) -> dict:
    """The judge run the night shift queues, claimed; its spec, whose key ``inputs`` and ``verdict`` send."""
    assert fire(world.client, NIGHT)["judge"] == 1
    spec = judge_key(claim(world.client, world.worker))
    assert spec["kind"] == "judge"
    return spec


KEYS: dict[int, str] = {}  # judge run id -> the key its claim handed the worker


def judge_key(spec: dict) -> dict:
    """The judge run's spec, its key kept for ``inputs`` and ``verdict``, which a daemon sends with them."""
    if spec.get("kind") == "judge" and (spec.get("curator") or {}).get("judge_key"):
        KEYS[spec["id"]] = spec["curator"]["judge_key"]
    return spec


def inputs(world, run_id: int, worker=None, key: str | None = None):
    headers = {**(worker or world.worker)["headers"], judge.JUDGE_KEY_HEADER: key or KEYS.get(run_id, "")}
    return world.client.get(f"/v1/worker/runs/{run_id}/judge", headers=headers)


def verdict(world, run_id: int, *, key: str | None = None, **body):
    sent = {
        "verdict": "pass",
        "reasons": "The test covers the helper.",
        "head_sha": HEAD,
        "verify": [{"command": VERIFY, "exit_code": 0}],
        "hidden": [{"index": 1, "exit_code": 0}],
        "signs": [],
        **body,
    }
    headers = {**world.worker["headers"], judge.JUDGE_KEY_HEADER: key or KEYS.get(run_id, "")}
    return world.client.post(f"/v1/worker/runs/{run_id}/verdict", json=sent, headers=headers)


def judged(world, **body) -> dict:
    """The judge run queued, claimed, its inputs read, its verdict posted, and the run ended done; the change."""
    spec = judge_run(world)
    assert inputs(world, spec["id"]).status_code == 200
    response = verdict(world, spec["id"], **body)
    assert response.status_code == 200, response.text
    moved(world.client, world.worker, spec["id"], "running", "verifying", "done")
    return response.json()


def through_the_judge(world, *, files=None, **body) -> dict:
    """An accepted tier 0 proposal built, its pull request opened, judged; the change."""
    accepted(world)
    builder(world, files)
    assert moved_on(world) == {"opened": 1}
    return judged(world, **body)


def one_change(world) -> dict:
    (found,) = changes(world)
    return found


def proposal_of(world, proposal_id: int) -> dict:
    response = world.client.get(path("proposals", str(proposal_id)), headers=world.headers["owner"])
    assert response.status_code == 200, response.text
    return response.json()


def calls(github, method: str, suffix: str) -> list:
    return [r for r in github.requests if r.method == method and r.path.endswith(suffix)]


def token_permissions(github, token: str) -> tuple[str, dict]:
    """(the App, the permissions) of an installation token the fake made."""
    made = github.app_tokens[token]
    return github.installations[made.installation].app, made.permissions


# An accepted proposal becomes the Curator's plan


def test_curator_plan_an_accepted_tier_0_proposal_becomes_a_plan_on_a_branch_of_its_own(world):
    proposal = accepted(world)
    assert proposal["state"] == "accepted" and proposal["tier"] == 0
    change = one_change(world)
    assert {key: change[key] for key in ("proposal_id", "plan_id", "repo", "branch", "forge", "tier", "state")} == {
        "proposal_id": proposal["id"],
        "plan_id": PLAN_ID,
        "repo": "evo-agents",
        "branch": BRANCH,
        "forge": "github",
        "tier": 0,
        "state": "planned",
    }
    assert set(change) == set(CHANGE_KEYS)
    plan = world.client.get(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", headers=world.headers["owner"]).json()
    assert plan["body"]["repos"] == [{"repo": "evo-agents", "branch": BRANCH}]
    assert [step["status"] for step in plan["body"]["steps"]] == ["pending"]
    assert plan["body"]["context"].startswith(f"Made by the Curator of project {PROJECT} from proposal #1")
    audit = tables.audit
    (row,) = sql(world.db, select(audit.c.target).where(audit.c.action == "curator.plan"))
    assert row[0] == f"{PROJECT}/{PLAN_ID} proposal:1 change:1 branch={BRANCH}"


def test_curator_plan_a_tier_2_proposal_accepted_records_the_answer_alone(world):
    accepted(world, kind="feature", paths=[{"repo": "evo-agents", "path": "evo_agents/hub/new.py"}])
    assert changes(world) == []


def test_curator_plan_a_draft_that_cannot_become_a_plan_is_left_open_with_why(world):
    two = [{"repo": "evo-agents", "path": "tests/test_wait.py"}, {"repo": "m1-identity", "path": "tests/test_x.py"}]
    accepted(world, paths=two)
    change = one_change(world)
    assert change["state"] == "open" and change["plan_id"] is None and change["branch"] is None
    assert "works in one repo, and the proposal names evo-agents, m1-identity" in change["reason"]
    assert fire(world.client, NIGHT).get("builder") is None  # nothing of it runs


def test_curator_plan_never_names_a_default_branch_and_keeps_its_verify(world):
    accepted(world)
    owner = world.headers["owner"]
    plan = world.client.get(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", headers=owner).json()
    for change in (
        {"repos": [{"repo": "evo-agents", "branch": "main"}]},
        {"repos": [{"repo": "evo-agents", "branch": BRANCH}, {"repo": "m1-identity", "branch": "x"}]},
        {"steps": [{**plan["body"]["steps"][0], "verify": "true"}]},
        {"steps": [{**plan["body"]["steps"][0], "acceptance": ["anything"]}]},
        {"steps": [{**plan["body"]["steps"][0], "repo": "m1-identity"}]},
        {"goal": "A clearer goal."},  # its goal, what and context are the hub's too (H1 of the security review)
    ):
        body = {**plan["body"], **change}
        put = {"body": body, "if_revision": plan["revision"]}
        refused = world.client.put(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", json=put, headers=owner)
        assert refused.status_code == 409, (change, refused.text)
        assert "is the Curator's: it works on curator/1-curator-fix-sleep of evo-agents alone" in refused.text
    kept = {**plan["body"], "steps": [{**plan["body"]["steps"][0], "status": "blocked", "note": "waits for CI"}]}
    put = {"body": kept, "if_revision": plan["revision"]}
    assert world.client.put(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", json=put, headers=owner).status_code == 200


def test_curator_plan_runs_only_on_the_night_shift(world):
    accepted(world)
    owner, worker_id = world.headers["owner"], world.worker["id"]
    for route, body in (
        ("plan-runs", {"plan_id": PLAN_ID, "worker_id": worker_id}),
        ("runs", {"plan_id": PLAN_ID, "steps": [1], "worker_id": worker_id}),
    ):
        refused = world.client.post(f"/v1/projects/{PROJECT}/{route}", json=body, headers=owner)
        assert refused.status_code == 409 and "only the night shift of the project's charter runs it" in refused.text
    assert sql(world.db, select(func.count()).select_from(tables.runs).where(tables.runs.c.plan_id == PLAN_ID)) == [
        (0,)
    ]


# The ruleset, and the Builder


RULESETS = [
    ({"bypass": {"workers", "curator"}}, "says the Curator's App may never bypass it"),
    ({"enforcement": "evaluate"}, "says the Curator's App may never bypass it"),
    ({"rules": ["deletion"]}, "no ruleset restricts updates"),
    ({"hide_bypass": True}, "says the Curator's App may never bypass it"),
]


@pytest.mark.parametrize("changed, why", RULESETS)
def test_protection_a_ruleset_that_lets_the_curator_by_is_not_protected(world, changed, why):
    world.github.repos[(MINE.lower(), "evo-agents")].rulesets = [{**RULESET, **changed}]
    found = protect(world)
    assert found.status_code == 200, found.text
    assert found.json()["protected"] is False and why in found.json()["reason"]


def test_protection_is_checked_by_an_admin_with_the_curators_app_and_listed_for_readers(world):
    client, headers = world.client, world.headers
    assert protect(world, headers=headers["other"]).status_code == 403  # a writer, not an admin
    assert protect(world, "m1-kb-docs").status_code == 422  # on GitLab
    assert protect(world, "gone").status_code == 404
    found = protect(world).json()
    assert found["protected"] is True and found["github_repo"] == f"{MINE}/evo-agents"
    assert found["default_branch"] == "main" and found["checked_by"] == "owner"
    assert found["rulesets"] == [{"id": 11, "enforcement": "active", "can_bypass": "never"}]
    assert_json_keys("hub curator protection", found, "--check")
    listed = client.get(path("protection"), headers=headers["other"]).json()
    assert [repo["repo"] for repo in listed["repos"]] == ["evo-agents", "m1-identity", "m1-kb-docs", "notes"]
    assert [repo["protected"] for repo in listed["repos"]] == [True, None, None, None]
    asked = [r for r in world.github.requests if r.path.startswith("/repos/") and "rulesets" in r.path]
    app, permissions = token_permissions(world.github, asked[0].headers["authorization"].partition(" ")[2])
    assert app == "curator" and permissions == {"metadata": "read"}
    (row,) = sql(world.db, select(tables.audit.c.target).where(tables.audit.c.action == "curator.protection"))
    assert row[0] == f"{PROJECT} repo=evo-agents protected=true"


def test_protection_a_builder_waits_for_a_checked_ruleset(world):
    accepted(world)
    assert fire(world.client, NIGHT).get("builder") is None  # never checked
    world.github.repos[(MINE.lower(), "evo-agents")].rulesets = [{**RULESET, "bypass": {"workers", "curator"}}]
    assert protect(world).json()["protected"] is False
    assert fire(world.client, NIGHT).get("builder") is None  # checked, and the Curator could push to main
    world.github.repos[(MINE.lower(), "evo-agents")].rulesets = [dict(RULESET)]
    assert protect(world).json()["protected"] is True
    rc = tables.curator_repo_checks
    sql(world.db, update(rc).values(checked_at=func.now() - func.make_interval(0, 0, 0, 3)))
    assert fire(world.client, NIGHT).get("builder") is None  # checked too long ago
    assert protect(world).json()["protected"] is True
    assert fire(world.client, NIGHT)["builder"] == 1


def test_curator_plan_builder_gets_the_curators_token_and_a_members_plan_run_the_workers(world):
    accepted(world)
    assert protect(world).status_code == 200
    assert fire(world.client, NIGHT)["builder"] == 1
    spec = claim(world.client, world.worker)
    assert spec["repos"] == [{"repo": "evo-agents", "branch": BRANCH}]
    assert spec["curator"] == {
        "role": "builder",
        "protected_paths": ["curator.yaml", ".github/workflows/**"],
        "change_id": 1,
        "branch": BRANCH,
        "forge": "github",
        "base_branch": None,
        "head_sha": None,
        "pr_url": None,
        "judge_key": None,  # a judge run's alone
    }
    lease = next(
        item for item in leased(world.client, world.worker, spec["id"])["leases"] if item["provider"] == "github-app"
    )
    app, permissions = token_permissions(world.github, lease["value"])
    assert app == "curator" and permissions == {"contents": "write", "metadata": "read"}
    moved(world.client, world.worker, spec["id"], "running", "failed", error="stopped for the test")

    # a member's own plan run, on the same repo: the workers' App, as before
    from tests.hub.test_credentials_api import plan_run as member_plan_run

    run_id = member_plan_run(world.client, world.headers["owner"], world.worker)
    lease = next(
        item for item in leased(world.client, world.worker, run_id)["leases"] if item["provider"] == "github-app"
    )
    app, permissions = token_permissions(world.github, lease["value"])
    assert app == "workers" and permissions == {"contents": "write", "metadata": "read"}


def test_curator_plan_without_the_curators_app_a_builder_gets_no_github_token(hub_db, tmp_path, github, app_key):  # noqa: F811
    config = credentials_config(hub_db, tmp_path, github, app_key)  # the workers' App alone
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = credential_members(client, github)
        grant(client, headers, "owner", "admin")
        worker = taking(client, credential_worker(client, headers["owner"], "mac-mini"))
        written(client, headers["owner"], curator_charter())
        install(github, "evo-agents")
        world = SimpleNamespace(client=client, headers=headers, worker=worker, github=github, db=hub_db, app=client.app)
        accepted(world)
        assert protect(world).status_code == 503  # nothing to check rulesets with
        rc = tables.curator_repo_checks
        sql(
            hub_db,
            rc.insert().values(
                project_id=1,
                repo="evo-agents",
                github_repo=f"{MINE}/evo-agents",
                protected=True,
                reason="checked before",
                rulesets=[],
            ),
        )
        assert fire(client, NIGHT)["builder"] == 1
        spec = claim(client, worker)
        answer_ = leased(client, worker, spec["id"])
        assert [item["provider"] for item in answer_["leases"]] == []
        (missing,) = [item for item in answer_["missing"] if item["repo"] == "evo-agents"]
        assert "gets its GitHub token from the Curator's App alone" in missing["reason"]
        assert "EVO_HUB_CURATOR_APP_ID" in missing["reason"]
        assert github.calls("/app/installations/1001/access_tokens") == []  # never the workers' App


def test_curator_plan_a_gitlab_builder_gets_the_charters_git_secret_and_env_secrets_alone(world):
    from tests.hub.test_credentials_api import GITLAB_KB, sample

    owner = world.headers["owner"]
    developer, maintainer, oauth, other = sample("glpat-dev-"), sample("glpat-max-"), sample("oat-"), sample("tok-")
    git_secret(world.client, owner, "gitlab-dev", GITLAB_KB, developer)
    git_secret(world.client, owner, "gitlab-maintainer", "https://gitlab.m1ops.com/fis-gb-m1", maintainer)
    env_secret(world.client, owner, "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", oauth)
    env_secret(world.client, owner, "gh-admin", "GH_TOKEN", other)
    plan = {**DRAFT, "steps": [{**DRAFT["steps"][0], "repo": "m1-kb-docs"}]}
    accepted(world, paths=[{"repo": "m1-kb-docs", "path": "tests/test_kb.py"}], plan=plan)
    assert one_change(world)["forge"] == "gitlab"
    assert fire(world.client, NIGHT).get("builder") is None  # the charter names no git secret
    written(world.client, owner, curator_charter(git_secret="gitlab-dev", env_secrets=["claude-oauth"]))
    assert fire(world.client, NIGHT)["builder"] == 1
    spec = claim(world.client, world.worker)
    assert spec["curator"]["forge"] == "gitlab"
    got = leased(world.client, world.worker, spec["id"])
    names = sorted(item["name"] for item in got["leases"])
    assert names == ["claude-oauth", "gitlab-dev"]  # neither the maintainer's token nor GH_TOKEN
    assert {item["value"] for item in got["leases"]} == {developer, oauth}


# The pull request and the Judge


def test_judge_the_pull_request_opens_and_the_judge_reads_the_change_and_its_hidden_checks(world):
    accepted(world)
    builder_id = builder(world)
    assert one_change(world)["state"] == "pr_pending"
    assert moved_on(world) == {"opened": 1}
    change = one_change(world)
    assert (change["state"], change["pr_number"], change["head_sha"], change["base_branch"]) == (
        "judge_pending",
        1,
        HEAD,
        "main",
    )
    assert change["pr_url"] == f"https://github.com/{MINE}/evo-agents/pull/1" and change["builder_run_id"] == builder_id
    (opened,) = calls(world.github, "POST", "/pulls")
    sent = json.loads(opened.body)
    assert (sent["head"], sent["base"]) == (BRANCH, "main") and "proposal #1 (tier 0)" in sent["body"]
    app, permissions = token_permissions(world.github, opened.headers["authorization"].partition(" ")[2])
    assert app == "workers" and permissions == {"pull_requests": "write", "metadata": "read"}

    spec = judge_run(world)
    assert spec["plan_id"] == PLAN_ID and spec["curator"]["role"] == "judge" and spec["curator"]["head_sha"] == HEAD
    assert (spec["runtime"], spec["model"]) == ("claude-code", "sonnet")  # no sink for Codex in the project's policy
    assert "You are the Judge of a change" in spec["prompt"] and VERIFY in spec["prompt"]
    assert HIDDEN[0] not in spec["prompt"]
    lease = next(
        item for item in leased(world.client, world.worker, spec["id"])["leases"] if item["provider"] == "github-app"
    )
    assert token_permissions(world.github, lease["value"]) == ("curator", {"contents": "read", "metadata": "read"})
    found = inputs(world, spec["id"])
    assert found.status_code == 200 and found.headers["cache-control"] == "no-store"
    assert found.json()["hidden_checks"] == HIDDEN and found.json()["verify"] == [VERIFY]
    assert found.json()["protected_paths"] == ["curator.yaml", ".github/workflows/**"]
    assert set(found.json()) == set(JudgeInputs.model_fields)
    events = world.client.get(f"/v1/projects/{PROJECT}/runs/{spec['id']}/events", headers=world.headers["owner"])
    assert HIDDEN[0] not in events.text  # the run's log never names a hidden check

    response = verdict(world, spec["id"])
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "judged" and response.json()["passed"] is True
    moved(world.client, world.worker, spec["id"], "running", "verifying", "done")


def queue_now(world, which: str):
    """``changes.queue_builder`` or ``queue_judge`` called as the night shift calls them, past the schedule's own
    gate (which waits while any run of the schedule is queued or held), so their own rule is what answers."""
    from evo_agents.hub.server import changes as change_routes
    from evo_agents.hub.server.collect import Gate
    from evo_agents.hub.server.curator import _due, _on_duty, _writer_access

    async def go():
        async with world.app.state.engine.begin() as conn:
            (due,) = (await conn.execute(_due(NIGHT))).all()
            access = await _writer_access(conn, due)
            worker, _ = await _on_duty(conn, due.worker_id, due.project_id)
            budget = {"max_usd": 0.5, "max_turns": 40, "max_seconds": 1800}
            gate = Gate(night=THE_NIGHT, figures=None, budget=budget, access=access, worker=worker)
            return await getattr(change_routes, f"queue_{which}")(conn, due, gate)

    return world.client.portal.call(go)


def test_judge_and_its_builder_never_run_at_once(world):
    accepted(world)
    builder_id = builder(world)
    assert moved_on(world) == {"opened": 1}
    r, c = tables.runs, tables.curator_changes
    # a run of the plan is active again (a lost Builder's next attempt): no Judge of the change while it is
    sql(world.db, update(r).values(state="queued", worker_id=None, finished_at=None).where(r.c.id == builder_id))
    assert queue_now(world, "judge") is None
    assert one_change(world)["state"] == "judge_pending"
    sql(world.db, update(r).values(state="cancelled", finished_at=func.now()).where(r.c.id == builder_id))
    judge_id = queue_now(world, "judge")
    assert judge_id is not None and one_change(world)["state"] == "judging"
    # and no Builder of the plan while its judge run is active, should the change be planned again with a step to do
    sql(world.db, update(c).values(state="planned"))
    owner = world.headers["owner"]
    plan = world.client.get(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", headers=owner).json()
    patch = {"section": "steps", "step": "1", "updates": {"status": "pending"}, "if_revision": plan["revision"]}
    assert world.client.patch(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", json=patch, headers=owner).status_code == 200
    assert queue_now(world, "builder") is None
    sql(world.db, update(r).values(state="cancelled", finished_at=func.now()).where(r.c.id == judge_id))
    assert queue_now(world, "builder") is not None


def test_judge_runs_codex_when_the_projects_policy_clears_it(world):
    from tests.hub.test_credentials_api import registration

    codex = {"id": "codex@openai", "kind": "agent-session", "clearance": {"level": "internal"}}
    body = registration()
    body["sinks"] = [*body["sinks"], codex]
    assert world.client.put(f"/v1/projects/{PROJECT}", json=body, headers=world.headers["admin"]).status_code == 200
    world.worker["runtimes"] = {**world.worker["runtimes"], "codex": {"available": True, "version": "0.50.0"}}
    taking(world.client, world.worker)
    accepted(world)
    builder(world)
    moved_on(world)
    spec = judge_run(world)
    assert (spec["runtime"], spec["model"]) == ("codex", None)
    (row,) = sql(world.db, select(tables.audit.c.target).where(tables.audit.c.action == "curator.judge"))
    assert row[0].endswith("runtime=codex")


def test_judge_a_run_that_ends_without_a_verdict_queues_another_then_leaves_the_change_open(world):
    # its judge runs fail, and two failures in a row would trip the circuit breaker (tests/hub/test_curator_ledger.py)
    written(world.client, world.headers["owner"], curator_charter(circuit_breaker={"max_failed_in_a_row": 10}))
    accepted(world)
    builder(world)
    moved_on(world)
    for attempt in range(1, judge.JUDGE_ATTEMPTS + 1):
        spec = judge_run(world)
        moved(world.client, world.worker, spec["id"], "running", "failed", error="the agent gave up")
        change = one_change(world)
        expected = "open" if attempt == judge.JUDGE_ATTEMPTS else "judge_pending"
        assert change["state"] == expected, (attempt, change)
    assert "3 judge runs ended without a verdict" in one_change(world)["reason"]
    n = tables.notifications
    failed = sql(world.db, select(func.count()).select_from(n).where(n.c.notice_kind == "run_failed"))[0][0]
    assert failed == judge.JUDGE_ATTEMPTS


def test_hidden_check_only_the_judge_run_reads_them(world):
    accepted(world)
    builder_id = builder(world)
    moved_on(world)
    spec = judge_run(world)
    other = taking(world.client, credential_worker(world.client, world.headers["other"], "other-mac"))
    assert inputs(world, spec["id"], other).status_code == 404  # another worker's token
    assert inputs(world, builder_id).status_code == 404  # a run of the Builder, not a judge run
    machine = {**world.headers["owner"], "X-Evo-Worker-Protocol": "1"}
    assert world.client.get(f"/v1/worker/runs/{spec['id']}/judge", headers=machine).status_code == 403
    shown = world.client.get(path("charter"), headers=world.worker["headers"])
    assert shown.status_code == 403  # a worker token reads no charter, its hidden checks least of all
    as_writer = world.client.get(path("charter"), headers=world.headers["other"]).json()
    assert as_writer["judge"]["hidden_checks"] is None
    assert inputs(world, spec["id"]).json()["hidden_checks"] == HIDDEN
    moved(world.client, world.worker, spec["id"], "running", "verifying", "done")
    assert inputs(world, spec["id"]).status_code == 404  # once it ended


def test_charter_forbidden_to_a_worker_token(world):
    for method in ("put", "get"):
        refused = getattr(world.client, method)(
            path("charter"), headers=world.worker["headers"], **({"json": curator_charter()} if method == "put" else {})
        )
        assert refused.status_code == 403, refused.text
    assert put_charter(world.client, world.headers["other"], curator_charter()).status_code == 403  # a writer


def test_judge_the_mcp_tools_of_a_judge_run_are_the_code_graphs_alone(world):
    accepted(world)
    builder(world)
    moved_on(world)
    spec = judge_run(world)
    token = {"Authorization": world.worker["headers"]["Authorization"]}

    def tool(name: str, arguments: dict | None = None) -> dict:
        message = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        response = world.client.post(
            "/mcp", json=message, headers={**token, **MCP_HEADERS, "X-Evo-Run": str(spec["id"])}
        )
        assert response.status_code == 200, response.text
        return response.json()["result"]

    for name, arguments in (
        ("run_events", {"run_id": one_change(world)["builder_run_id"]}),
        ("plan_show", {"plan_id": PLAN_ID}),
        ("memory_search", {"query": "judge"}),
        ("digest_list", {"days": 7}),
    ):
        refused = tool(name, arguments)
        assert refused.get("isError") and "is not for a judge run" in refused["content"][0]["text"], name
    assert not tool("hub_projects").get("isError")


# Signs of score hacking


def test_hack_a_protected_path_in_the_pull_request_fails_the_change_before_its_judge(world):
    proposal = accepted(world)
    files = [*CLEAN, {"filename": "curator.yaml", "status": "modified", "patch": "@@ -1 +1 @@\n-a: 1\n+a: 2\n"}]
    builder(world, files)
    assert moved_on(world) == {"signs": 1}
    change = one_change(world)
    assert (change["state"], change["passed"], change["tier"]) == ("judged", False, 3)
    assert change["verdict"]["source"] == "hub" and change["verdict"]["signs"][0]["kind"] == "protected_path"
    found = proposal_of(world, proposal["id"])
    assert found["tier"] == 3 and "signs of score hacking" in found["tier_reasons"][-1]
    assert fire(world.client, NIGHT).get("judge") is None  # no Judge for a change that failed already
    assert moved_on(world) == {"checked": 1, "open": 1}
    (check,) = calls(world.github, "POST", "/check-runs")
    assert json.loads(check.body)["conclusion"] == "failure"
    assert one_change(world)["state"] == "open" and world.github.merges == []


def test_hack_signs_the_worker_found_put_the_proposal_at_tier_3_and_fail_the_judge(world):
    proposal = accepted(world)
    builder(world)
    moved_on(world)
    sign = {"kind": "skip_added", "path": "tests/test_wait.py", "line": 3, "text": "@pytest.mark.skip"}
    change = judged(world, signs=[sign])
    assert (change["state"], change["passed"], change["tier"]) == ("judged", False, 3)
    assert "signs of score hacking: skip_added" in change["verdict"]["failures"][0]
    assert proposal_of(world, proposal["id"])["tier"] == 3
    moved_on(world)
    assert one_change(world)["state"] == "open" and world.github.merges == []


def test_judge_a_verdict_without_its_hidden_checks_run_fails_the_change(world):
    change = through_the_judge(world, hidden=[])
    assert change["passed"] is False
    assert change["verdict"]["failures"] == ["1 hidden check(s) of the project did not run"]
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    moved_on(world)
    assert one_change(world)["state"] == "open" and world.github.merges == []


# The merge


def test_merge_a_tier_0_change_the_judge_passed_at_its_head_with_ci_green(world):
    change = through_the_judge(world)
    assert change["passed"] is True
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "merged": 1}
    change = one_change(world)
    assert change["state"] == "merged" and change["merge_sha"] and change["merged_at"]
    assert world.github.merges == [(MINE, "evo-agents", 1, HEAD, "workers")]  # at the head judged, by the workers' App
    (merged,) = calls(world.github, "PUT", "/merge")
    assert json.loads(merged.body)["sha"] == HEAD
    assert token_permissions(world.github, merged.headers["authorization"].partition(" ")[2]) == (
        "workers",
        {"contents": "write", "pull_requests": "write", "metadata": "read"},
    )
    (check,) = calls(world.github, "POST", "/check-runs")
    sent = json.loads(check.body)
    assert (sent["name"], sent["head_sha"], sent["conclusion"]) == (judge.JUDGE_CHECK_NAME, HEAD, "success")
    assert HIDDEN[0] not in check.body and "1 of 1 passed" in sent["output"]["summary"]
    assert token_permissions(world.github, check.headers["authorization"].partition(" ")[2]) == (
        "workers",
        {"checks": "write", "metadata": "read"},
    )
    n = tables.notifications
    (note,) = sql(world.db, select(n.c.title, n.c.details).where(n.c.notice_kind == "merge_default_branch"))
    assert note[0] == "The hub merged pull request #1 of evo-agents into main"
    assert note[1]["change_id"] == 1 and note[1]["commits"] == [change["merge_sha"]]
    (row,) = sql(world.db, select(tables.audit.c.target).where(tables.audit.c.action == "curator.merge"))
    assert f"sha={change['merge_sha']}" in row[0]
    assert moved_on(world) == {}  # nothing left to move


def test_merge_waits_for_ci_and_stays_open_when_it_fails(world):
    through_the_judge(world)
    world.github.ci(HEAD, runs=[{"name": "test", "status": "in_progress"}])
    assert moved_on(world) == {"checked": 1, "wait": 1}
    assert one_change(world)["state"] == "judged" and "test is in_progress" in one_change(world)["reason"]
    world.github.ci(HEAD, runs=[{"name": "test", "status": "completed", "conclusion": "failure"}])
    assert moved_on(world) == {"open": 1}
    change = one_change(world)
    assert change["state"] == "open" and change["reason"] == "test ended failure" and world.github.merges == []


def test_merge_with_no_ci_at_all_stays_open(world):
    through_the_judge(world)
    assert moved_on(world) == {"checked": 1, "open": 1}
    assert one_change(world)["reason"] == "no CI ran on the commit"


@pytest.mark.parametrize(
    "setup, why",
    [
        ("tier_1", "tier 1 waits for its owner to merge it"),
        ("auto_merge", "the charter's auto_merge does not name tier 0"),
        ("ruleset", "has not checked that the repo's ruleset keeps the Curator off"),
        ("head_moved", "not the commit the Judge passed"),
        ("protected", "protected paths: ci_changed, protected_path"),
    ],
)
def test_merge_refusals_leave_the_pull_request_open(world, setup, why):
    if setup == "tier_1":
        accepted(world, kind="fix", paths=[{"repo": "evo-agents", "path": "tests/test_wait.py"}])
        builder(world)
        moved_on(world)
        judged(world)
    else:
        through_the_judge(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    if setup == "auto_merge":
        written(world.client, world.headers["owner"], curator_charter(auto_merge=[]))
    elif setup == "ruleset":
        world.github.repos[(MINE.lower(), "evo-agents")].rulesets = [{**RULESET, "bypass": {"workers", "curator"}}]
    elif setup == "head_moved":
        world.github.push(MINE, "evo-agents", BRANCH, MOVED)
        world.github.ci(MOVED, runs=GREEN, statuses=SUCCESS)
    elif setup == "protected":
        files = [
            *CLEAN,
            {"filename": ".github/workflows/ci.yml", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b\n"},
        ]
        world.github.repos[(MINE.lower(), "evo-agents")].files[BRANCH] = files
    found = moved_on(world)
    assert found.get("open") == 1 and "merged" not in found, found
    change = one_change(world)
    assert change["state"] == "open" and why in change["reason"], change
    assert world.github.merges == [] and calls(world.github, "PUT", "/merge") == []


def test_merge_never_for_a_gitlab_merge_request(world):
    from tests.hub.test_credentials_api import GITLAB_KB, sample

    owner = world.headers["owner"]
    git_secret(world.client, owner, "gitlab-dev", GITLAB_KB, sample("glpat-dev-"))
    written(world.client, owner, curator_charter(git_secret="gitlab-dev"))
    plan = {**DRAFT, "steps": [{**DRAFT["steps"][0], "repo": "m1-kb-docs"}]}
    accepted(world, paths=[{"repo": "m1-kb-docs", "path": "tests/test_kb.py"}], plan=plan)
    assert fire(world.client, NIGHT)["builder"] == 1
    spec = claim(world.client, world.worker)
    moved(world.client, world.worker, spec["id"], "running")
    verify = [{"command": VERIFY, "exit_code": 0}]
    reported(world.client, world.worker, spec["id"], 1, "done", repo="m1-kb-docs", verify=verify, commit_sha=HEAD)
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    assert one_change(world)["state"] == "judge_pending"  # the push opened the merge request: no pull request to open
    assert moved_on(world) == {}
    spec = judge_run(world)
    assert spec["curator"]["head_sha"] is None and spec["curator"]["forge"] == "gitlab"
    response = verdict(world, spec["id"])
    assert response.status_code == 200 and response.json()["passed"] is True
    assert response.json()["head_sha"] == HEAD  # the tip the Judge read is the one judged
    assert moved_on(world) == {"open": 1}
    change = one_change(world)
    assert change["state"] == "open" and "the hub never merges one" in change["reason"]
    assert world.github.merges == [] and [r for r in world.github.requests if "/pulls" in r.path] == []


def test_merge_a_members_plan_run_still_works_on_the_default_branch_its_plan_names(world):
    """a12: the plan a member wrote names main for each repo; its plan run gets main, and its claim names no Curator."""
    from tests.hub.test_credentials_api import PLAN as MEMBER_PLAN

    body = {"plan_id": MEMBER_PLAN, "worker_id": world.worker["id"]}
    response = world.client.post(f"/v1/projects/{PROJECT}/plan-runs", json=body, headers=world.headers["owner"])
    assert response.status_code == 201, response.text
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "plan" and spec["curator"] is None
    assert {repo["branch"] for repo in spec["repos"]} == {"main"}


# The brief, the command line and schema 0017


def test_merge_the_morning_brief_lists_the_curators_pull_requests_waiting(world):
    from evo_agents.hub.server.brief import _awaiting

    async def waiting():
        async with world.app.state.engine.begin() as conn:
            return await _awaiting(conn, 1)

    through_the_judge(world)
    total, listed = world.client.portal.call(waiting)
    assert total == 1 and listed[0]["pull_request"] == f"https://github.com/{MINE}/evo-agents/pull/1"
    assert listed[0]["title"] == "pull request #1 of evo-agents (tier 0, judged)"


def test_curator_plan_changes_and_protection_on_the_command_line(world, tmp_path, monkeypatch, capsys):
    accepted(world)
    with serving(create_app(world.app.state.config)) as url:
        token = world.headers["owner"]["Authorization"].removeprefix("Bearer ")
        home = write_credentials(tmp_path / "owner", url, "owner", token)

        def curator(*args: str):
            return cli(monkeypatch, capsys, home, "hub", "curator", *args, "--project", PROJECT)

        checked = as_json(curator("protection", "--check", "evo-agents", "--json"))
        assert_json_keys("hub curator protection", checked, "--check")
        assert checked["protected"] is True
        assert "keeps the Curator's App off main" in ok(curator("protection", "--check", "evo-agents")).out
        listed = as_json(curator("protection", "--json"))
        assert_json_keys("hub curator protection", listed)
        assert [repo["repo"] for repo in listed["repos"]][0] == "evo-agents"
        assert "PROTECTED" in ok(curator("protection")).out
        found = as_json(curator("changes", "--json"))
        assert_json_keys("hub curator changes", found)
        assert found["changes"][0]["plan_id"] == PLAN_ID and set(found["changes"][0]) == set(CHANGE_KEYS)
        assert f"1 change(s) of the Curator in {PROJECT}" in ok(curator("changes")).out


def test_curator_plan_schema_0017_goes_down_to_0016_and_up_again(world):
    through_the_judge(world)
    db = world.db
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(db, update(tables.curator_changes).values(branch="main"))
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(db, update(tables.curator_changes).values(state="merged"))  # merged needs merged_at
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(db, update(tables.curator_changes).values(state="planned", plan_id=None))
    r = tables.runs
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(db, update(r).values(schedule_id=None).where(r.c.kind == "judge"))
    assert sql(db, select(func.count()).select_from(r).where(r.c.kind == "judge")) == [(1,)]
    move_to(db, "0016", down=True)
    assert sql(db, select(func.count()).select_from(r).where(r.c.kind == "judge")) == [(0,)]
    move_to(db, "0017")
    assert sql(db, select(func.count()).select_from(tables.curator_changes))[0][0] == 0
    assert sql(db, select(func.count()).select_from(r).where(r.c.kind == "judge"))[0][0] == 0


def test_curator_plan_the_job_is_registered_every_minute():
    from evo_agents.hub import jobs

    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.CURATOR_CHANGES].cron == "* * * * *"
    assert queue.tasks[jobs.CURATOR_CHANGES].queueing_lock == jobs.CURATOR_CHANGES == "curator.changes"


def test_curator_plan_a_rejected_proposal_becomes_nothing(world):
    run_id = review_run(world)
    made = proposed(
        world.client,
        world.worker,
        run_id,
        kind="test_add",
        paths=[{"repo": "evo-agents", "path": "tests/test_wait.py"}],
        evidence=[{"kind": "run", "run_id": run_id, "seq": 1}],
    )
    response = answer(world.client, world.headers["owner"], made["id"], "reject")
    assert response.status_code == 200 and changes(world) == []
