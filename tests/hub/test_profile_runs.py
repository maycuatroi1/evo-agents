"""The agent profile of a run where the hub dispatches, claims and leases it (``runs.service.profiles``,
``credentials``), as criterion a3 of the plan agent-profiles-group-chat names it: a dispatch names a profile or goes
under the project's default; one whose project, runtime, model or worker the profile's allowlists leave out is 422
saying why, and nothing is dispatched; a worker claims a run only when the profile names it or one of its labels, and
for a profile that limits tools, skills or models only when its daemon applies profiles; the leases of a run are the
owner's secrets cut to the profile's credential allowlist, by name, the GitHub App's token counted as github-app; an
author run's skill must be in the skill allowlist. The run records the profile and its version, and so do its leases
and the audit. A dispatch that names no profile runs as before (``tests.hub.test_runs``, ``test_credentials_api``)."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import author, profiles, runs, tables
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_credentials_api import (
    CHECKOUTS,
    DISPATCHED,
    GITLAB_KB,
    MINE,
    OWNER,
    PLAN,
    PROJECT,
    STEPS,
    audit_rows,
    by_name,
    env_secret,
    git_secret,
    install,
    leased,
    missing,
    registration,
    sample,
    worker_of,
)
from tests.hub.test_runs import add_worker, claim, of_run, report

app_key, config, hub, client = (
    credentials_api.app_key,
    credentials_api.config,
    credentials_api.hub,
    credentials_api.client,
)

ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
VAR = "CLAUDE_CODE_OAUTH_TOKEN"
HARNESS = "evo-agents-harness"
BOTH = {"claude-code": {"available": True, "version": "2.1.289"}, "codex": {"available": True, "version": "0.153.4"}}


@pytest.fixture
def admin(client, hub) -> dict:
    """The owner made an admin of the project, who manages its agent profiles; the headers of ``hub``."""
    response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=hub["admin"])
    assert response.status_code == 200, response.text
    return hub


def profile(client, headers, handle: str, project: str = PROJECT, **body) -> dict:
    response = client.post(
        f"/v1/projects/{project}/agents", json={"handle": handle, "name": handle, **body}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def allow(**axes) -> dict:
    """Permissions whose axes ``axes`` are allowlists of the items given."""
    return {axis: {"mode": "allowlist", "allow": list(items)} for axis, items in axes.items()}


def dispatch(client, headers, repo: str = "evo-agents", worker: dict | None = None, **extra):
    body = {"plan_id": PLAN, "steps": [STEPS[repo]], **extra}
    if worker is not None:
        body["worker_id"] = worker["id"]
    return client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=headers)


def dispatched(client, headers, repo: str = "evo-agents", worker: dict | None = None, **extra) -> dict:
    response = dispatch(client, headers, repo, worker, **extra)
    assert response.status_code == 201, response.text
    return response.json()[0]


def refused(response, status: int = 422) -> str:
    assert response.status_code == status, response.text
    return response.json()["message"]


def beat(client, worker: dict, features: list[str] | None = None, kinds: list[str] | None = None) -> None:
    body = {"runtimes": worker["runtimes"], "checkouts": worker["checkouts"], "free_slots": 1, "runs": []}
    if features is not None:
        body["features"] = features
    if kinds is not None:
        body["run_kinds"] = kinds
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text


def label(client, headers, worker: dict, *labels: str) -> None:
    response = client.patch(f"/v1/workers/{worker['id']}", json={"labels": list(labels)}, headers=headers)
    assert response.status_code == 200, response.text


def run_count(db) -> int:
    return len(sql(db, select(tables.runs.c.id)))


# Dispatching


def test_a_profile_dispatch_records_the_profile_and_version_and_the_audit_names_it(client, admin, hub_db):
    made = profile(client, admin["owner"], "narrow", permissions=allow(credential=["claude-b"]))
    run = dispatched(client, admin["owner"], "evo-agents", profile="narrow")
    ap = tables.agent_profiles
    (profile_id,) = sql(hub_db, select(ap.c.id).where(ap.c.handle == "narrow"))[0]
    assert (run["profile"], run["profile_id"], run["profile_version"]) == ("narrow", profile_id, made["version"])
    assert audit_rows(hub_db, "run.dispatch")[-1][0] == f"{PROJECT}/{PLAN}#1 run:{run['id']} profile=@narrow:v1"
    # no profile named: the project's default, which the audit leaves unnamed, as before profiles
    plain = dispatched(client, admin["owner"], "lumen-kb-docs")
    assert (plain["profile"], plain["profile_version"]) == (PROJECT, 1)
    assert audit_rows(hub_db, "run.dispatch")[-1][0] == f"{PROJECT}/{PLAN}#2 run:{plain['id']}"


def test_a_profile_dispatch_outside_its_runtime_or_model_allowlist_is_422_and_nothing_is_dispatched(
    client, admin, hub_db
):
    profile(client, admin["owner"], "sonnet", permissions=allow(runtime=["claude-code"], model=["claude-sonnet-4-5"]))
    text = refused(dispatch(client, admin["owner"], runtime="codex", profile="sonnet"))
    assert text == "agent @sonnet does not allow runtime codex: it allows claude-code; nothing was dispatched"
    text = refused(dispatch(client, admin["owner"], runtime="claude-code", model="claude-opus-4", profile="sonnet"))
    assert "does not allow model claude-opus-4: it allows claude-sonnet-4-5" in text
    assert refused(dispatch(client, admin["owner"], profile="nobody"), 404).startswith("no agent @nobody")
    assert run_count(hub_db) == 0
    # without a model, the first the profile allows
    run = dispatched(client, admin["owner"], runtime="claude-code", profile="sonnet")
    assert (run["model"], run["requested_runtime"]) == ("claude-sonnet-4-5", "claude-code")


def test_a_profile_dispatch_to_a_worker_outside_its_worker_allowlist_is_422(client, admin, hub_db):
    mac, linux = worker_of(client, admin["owner"], "mac-mini"), worker_of(client, admin["owner"], "linux-box")
    label(client, admin["owner"], linux, "GPU")
    profile(client, admin["owner"], "gpu-only", permissions=allow(worker=["label:gpu"]))
    text = refused(dispatch(client, admin["owner"], worker=mac, profile="gpu-only"))
    assert text == "agent @gpu-only does not allow worker mac-mini: it allows label:gpu; nothing was dispatched"
    assert dispatched(client, admin["owner"], worker=linux, profile="gpu-only")["pinned_worker_id"] == linux["id"]
    profile(client, admin["owner"], "elsewhere", permissions=allow(worker=["some-other-box"]))
    text = refused(dispatch(client, admin["owner"], "lumen-kb-docs", profile="elsewhere"))
    assert "allows none of your workers of project evo-agents to take the run" in text


def test_a_profile_dispatch_picking_an_account_the_profile_leaves_out_is_422(client, admin, hub_db):
    for name in ("claude-a", "claude-b"):
        env_secret(client, admin["owner"], name, VAR, sample(f"{name}-"))
    profile(client, admin["owner"], "b-only", permissions=allow(credential=["claude-b"]))
    text = refused(dispatch(client, admin["owner"], account="claude-a", profile="b-only"))
    assert "does not allow the credential claude-a picked as the run's account" in text
    assert dispatched(client, admin["owner"], account="claude-b", profile="b-only")["profile"] == "b-only"


def test_a_profile_of_another_project_dispatches_only_where_its_project_axis_allows(client, admin, hub_db):
    second = client.put("/v1/projects/second", json=registration(DISPATCHED), headers=admin["admin"])
    assert second.status_code == 200, second.text
    granted = client.put(f"/v1/admin/projects/second/grants/{OWNER}", json=ADMIN_GRANT, headers=admin["admin"])
    assert granted.status_code == 200, granted.text
    profile(client, admin["owner"], "outsider", project="second", permissions=allow(project=[]))
    text = refused(dispatch(client, admin["owner"], profile="outsider"))
    assert text == "agent @outsider of project second does not take runs of project evo-agents; nothing was dispatched"
    opened = client.patch(
        "/v1/projects/second/agents/outsider",
        json={"permissions": allow(project=[PROJECT])},
        headers=admin["owner"],
    )
    assert opened.status_code == 200, opened.text
    assert dispatched(client, admin["owner"], profile="outsider")["profile"] == "outsider"
    # a writer of this project without a grant on the profile's sees no such agent
    assert refused(dispatch(client, admin["other"], "lumen-kb-docs", profile="outsider"), 404).startswith("no agent")


def test_a_deleted_profile_takes_no_new_dispatch_and_a_rerun_goes_under_the_profile_as_it_is_now(client, admin, hub_db):
    worker = worker_of(client, admin["owner"], "mac-mini")
    profile(client, admin["owner"], "rerunner")
    first = dispatched(client, admin["owner"], worker=worker, profile="rerunner")
    assert claim(client, worker)["id"] == first["id"]
    assert report(client, worker, first["id"], "failed", error="it broke").status_code == 200
    renamed = client.patch(f"/v1/projects/{PROJECT}/agents/rerunner", json={"name": "R"}, headers=admin["owner"])
    assert renamed.json()["version"] == 2
    rerun = client.post(f"/v1/projects/{PROJECT}/runs/{first['id']}/rerun", headers=admin["owner"])
    assert rerun.status_code == 201, rerun.text
    assert (rerun.json()["profile"], rerun.json()["profile_version"]) == ("rerunner", 2)
    assert audit_rows(hub_db, "run.rerun")[-1][0].endswith(" profile=@rerunner:v2")
    assert claim(client, worker)["id"] == rerun.json()["id"]
    assert report(client, worker, rerun.json()["id"], "failed", error="again").status_code == 200

    assert client.delete(f"/v1/projects/{PROJECT}/agents/rerunner", headers=admin["owner"]).status_code == 200
    text = refused(client.post(f"/v1/projects/{PROJECT}/runs/{rerun.json()['id']}/rerun", headers=admin["owner"]))
    assert "agent @rerunner was deleted at" in text and text.endswith("takes no new run; nothing was dispatched")
    assert "was deleted at" in refused(dispatch(client, admin["owner"], "lumen-kb-docs", profile="rerunner"))


def test_a_profile_skill_allowlist_without_create_exec_plan_refuses_an_author_run_dispatch(client, admin, hub_db):
    checkouts = {**CHECKOUTS, f"{PROJECT}/{HARNESS}": {"path": f"/src/{HARNESS}", "branch": "main"}}
    worker = add_worker(client, admin["owner"], "mac-mini", checkouts=checkouts)
    beat(client, worker, kinds=list(runs.RUN_KINDS), features=[runs.AGENT_PROFILES])
    profile(client, admin["owner"], "no-plans", permissions=allow(skill=["stop-slop"]))
    profile(client, admin["owner"], "planner", permissions=allow(skill=[author.AUTHOR_SKILL]))
    body = {"request": "Write a plan.", "worker_id": worker["id"]}
    path = f"/v1/projects/{PROJECT}/author-runs"
    text = refused(client.post(path, json={**body, "profile": "no-plans"}, headers=admin["owner"]))
    assert f"does not allow skill {author.AUTHOR_SKILL}, which an author run writes its plan with" in text
    accepted = client.post(path, json={**body, "profile": "planner"}, headers=admin["owner"])
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["profile"] == "planner"


# Claiming


def test_a_worker_claims_a_profile_run_only_when_its_worker_axis_names_it_or_a_label(client, admin, hub_db):
    mac, linux = worker_of(client, admin["owner"], "mac-mini"), worker_of(client, admin["owner"], "linux-box")
    label(client, admin["owner"], linux, "gpu")
    profile(client, admin["owner"], "gpu-only", permissions=allow(worker=["label:gpu"]))
    run = dispatched(client, admin["owner"], profile="gpu-only")
    assert claim(client, mac) is None
    spec = claim(client, linux)
    assert spec["id"] == run["id"] and spec["profile"]["handle"] == "gpu-only"


def test_a_profile_claim_of_a_run_asking_any_runtime_takes_a_runtime_the_profile_allows(client, admin, hub_db):
    claude_only = worker_of(client, admin["owner"], "claude-box")
    both = add_worker(client, admin["owner"], "both-box", runtimes=BOTH, checkouts=CHECKOUTS)
    profile(client, admin["owner"], "codex-only", permissions=allow(runtime=["codex"]))
    run = dispatched(client, admin["owner"], profile="codex-only")
    assert run["requested_runtime"] == "any"
    assert claim(client, claude_only) is None
    spec = claim(client, both)
    assert (spec["id"], spec["runtime"]) == (run["id"], "codex")
    # with every runtime allowed, the claim takes the worker's first, as before profiles
    plain = dispatched(client, admin["owner"], "lumen-kb-docs")
    assert claim(client, claude_only)["id"] == plain["id"]
    assert of_run(hub_db, plain["id"], "runtime") == [("claude-code",)]


def test_a_profile_that_limits_tools_is_claimed_only_by_a_daemon_that_applies_profiles(client, admin, hub_db):
    worker = worker_of(client, admin["owner"], "mac-mini")
    tools = allow(tool=["read", "mcp:github"])
    profile(client, admin["owner"], "reader-bot", instructions="Read; change nothing.", permissions=tools)
    text = refused(dispatch(client, admin["owner"], worker=worker, profile="reader-bot"), 409)
    assert "does not say it applies agent profiles" in text
    run = dispatched(client, admin["owner"], profile="reader-bot")
    assert claim(client, worker) is None
    beat(client, worker, features=[runs.AGENT_PROFILES])
    spec = claim(client, worker)
    assert spec["id"] == run["id"]
    assert spec["profile"] == {
        "handle": "reader-bot",
        "version": 1,
        "name": "reader-bot",
        "instructions": "Read; change nothing.",
        "permissions": {**{axis: {"mode": "all"} for axis in profiles.AXES}, **tools},
    }
    # a run under the project's default profile carries it too, every axis all
    plain = dispatched(client, admin["owner"], "lumen-kb-docs")
    assert report(client, worker, run["id"], "failed", error="done").status_code == 200
    plain_spec = claim(client, worker)
    assert plain_spec["id"] == plain["id"] and plain_spec["profile"]["handle"] == PROJECT
    assert plain_spec["profile"]["instructions"] == ""


# Leasing


def test_a_profile_lease_is_the_owners_secrets_cut_to_its_credential_allowlist(client, admin, hub_db):
    worker = worker_of(client, admin["owner"], "mac-mini")
    for name in ("claude-a", "claude-b"):
        env_secret(client, admin["owner"], name, VAR, sample(f"{name}-"))
    git_secret(client, admin["owner"], "gitlab-kb", GITLAB_KB, sample("glpat-"))
    git_secret(client, admin["owner"], "gitlab-other", GITLAB_KB, sample("glpat-"))
    made = profile(client, admin["owner"], "narrow", permissions=allow(credential=["claude-b", "gitlab-kb"]))
    run = dispatched(client, admin["owner"], "lumen-kb-docs", worker=worker, profile="narrow")
    assert claim(client, worker)["id"] == run["id"]

    answer = leased(client, worker, run["id"])
    assert sorted(by_name(answer)) == ["claude-b", "gitlab-kb"]
    # an allowlist never grows by itself: a secret added later stays out
    env_secret(client, admin["owner"], "claude-c", "ANTHROPIC_API_KEY", sample("claude-c-"))
    assert sorted(by_name(leased(client, worker, run["id"]))) == ["claude-b", "gitlab-kb"]

    cl = tables.credential_leases
    rows = sql(hub_db, select(cl.c.profile_id, cl.c.profile_version).where(cl.c.run_id == run["id"]))
    profile_id = of_run(hub_db, run["id"], "profile_id")[0][0]
    assert rows and set(rows) == {(profile_id, made["version"])}
    target = audit_rows(hub_db, "credential.lease")[-1][0]
    assert target.endswith(" repos=lumen-kb-docs profile=@narrow:v1") and "claude-b" not in target
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}/credentials", headers=admin["owner"]).json()
    assert {(lease["name"], lease["profile"], lease["profile_version"]) for lease in shown} == {
        ("claude-b", "narrow", 1),
        ("gitlab-kb", "narrow", 1),
    }


def test_a_profile_lease_gets_the_github_app_token_only_when_it_allows_github_app(client, admin, hub_db, github):
    install(github, "evo-agents")
    worker = worker_of(client, admin["owner"], "mac-mini")
    profile(client, admin["owner"], "no-app", permissions=allow(credential=[]))
    profile(client, admin["owner"], "app", permissions=allow(credential=["github-app"]))

    first = dispatched(client, admin["owner"], "evo-agents", worker=worker, profile="no-app")
    assert claim(client, worker)["id"] == first["id"]
    answer = leased(client, worker, first["id"])
    assert answer["leases"] == []
    origin, reason = missing(answer)["evo-agents"]
    assert origin == DISPATCHED["evo-agents"]
    assert reason.startswith("agent @no-app allows no token of the hub's GitHub App (github-app)")
    assert report(client, worker, first["id"], "failed", error="no token").status_code == 200

    second = dispatched(client, admin["owner"], "evo-agents", worker=worker, profile="app")
    assert claim(client, worker)["id"] == second["id"]
    tokens = by_name(leased(client, worker, second["id"]))
    assert list(tokens) == [f"github-app:{MINE}"] and tokens[f"github-app:{MINE}"]["provider"] == "github-app"


def test_a_run_without_a_profile_from_before_0030_is_claimed_and_leased_as_before(client, admin, hub_db):
    worker = worker_of(client, admin["owner"], "mac-mini")
    env_secret(client, admin["owner"], "claude-a", VAR, sample("claude-a-"))
    run = dispatched(client, admin["owner"], "lumen-kb-docs", worker=worker)
    r = tables.runs
    sql(hub_db, update(r).values(profile_id=None, profile_version=None).where(r.c.id == run["id"]))
    spec = claim(client, worker)
    assert spec["id"] == run["id"] and spec["profile"] is None
    assert sorted(by_name(leased(client, worker, run["id"]))) == ["claude-a"]
    assert audit_rows(hub_db, "credential.lease")[-1][0].endswith(" repos=lumen-kb-docs")
