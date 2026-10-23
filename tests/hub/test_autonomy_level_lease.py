"""K7 at the lease (``evo_agents.hub.server.lease_autonomy``), as criterion a6 of plan autonomy-levels names it: a run is
leased only the credentials K7 of its principal allows, the runtime's sign-in aside. Under deny a GitHub token that reads
for each repo alone and no git or env secret; under auto:run_branch a token that writes only for a repo whose ruleset
keeps the App off its default branch, a token that reads elsewhere, and a git secret only for an origin whose default
branch forbids a push; under auto:test_secrets the env secrets labelled test too. Each withholding writes the audit row
``autonomy.refused`` naming the class, the mode and the source; a run at A5 is leased as before."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import runs
from tests.hub.fake_github import Account
from tests.hub.test_credentials_api import (  # noqa: F401 (app_key, client, config and hub are fixtures)
    GITLAB_KB,
    MINE,
    OWNER,
    OWNER_ID,
    PROJECT,
    THEIRS,
    app_key,
    audit_rows,
    by_name,
    client,
    config,
    env_secret,
    git_secret,
    hub,
    install,
    leased,
    missing,
    plan_run,
    put_secret,
    sample,
    worker_of,
)

FEATURES = [runs.AUTONOMY, runs.AGENT_PROFILES]
KEEPS_OFF = {"id": 21, "enforcement": "active", "rules": ["update"], "bypass": set()}


def pref(client, headers, **classes) -> None:
    response = client.put("/v1/autonomy/prefs", json={"level": "A5", "classes": classes}, headers=headers)
    assert response.status_code == 200, response.text


def autonomy_worker(client, headers) -> dict:
    """A worker of ``headers`` whose daemon says ``autonomy``, so it may claim a restricted run."""
    worker = worker_of(client, headers, "mac-mini")
    body = {"runtimes": worker["runtimes"], "checkouts": worker["checkouts"], "free_slots": 1, "features": FEATURES}
    assert client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"]).status_code == 200
    return worker


def github_repos(github) -> None:
    """evo-agents of MINE behind a ruleset no App may bypass; lumen-identity of THEIRS behind none. The workers' App is
    installed on both, and the owner may push to both."""
    install(github, "evo-agents")
    github.add_repo(MINE, "evo-agents", rulesets=[dict(KEEPS_OFF)])
    github.install(THEIRS, "lumen-identity")
    github.collaborate(THEIRS, "lumen-identity", Account(OWNER, OWNER_ID), "write")
    github.add_repo(THEIRS, "lumen-identity")


def secrets_of(client, hub) -> dict:
    values = {
        "claude": sample("oauth-"),
        "stripe": sample("stripe-"),
        "stripe-test": sample("stripe-test-"),
        "kb": sample("glpat-"),
    }
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", values["claude"])
    env_secret(client, hub["owner"], "stripe", "STRIPE_API_KEY", values["stripe"])
    env_secret(client, hub["owner"], "stripe-test", "STRIPE_TEST_KEY", values["stripe-test"], environment="test")
    git_secret(client, hub["owner"], "gitlab-kb", GITLAB_KB, values["kb"])
    return values


def permissions(github, lease: dict) -> dict:
    return github.app_tokens[lease["value"]].permissions


def test_autonomy_level_lease_a_run_at_a5_is_leased_as_before(client, hub, github):
    github_repos(github)
    secrets_of(client, hub)
    run_id = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    leases = by_name(leased(client, worker, run_id))
    assert {"claude-oauth", "stripe", "stripe-test", "gitlab-kb", f"github-app:{MINE}"} <= set(leases)
    assert permissions(github, leases[f"github-app:{MINE}"])["contents"] == "write"
    assert leases[f"github-app:{MINE}"]["url_prefix"] == f"https://github.com/{MINE}"


def test_autonomy_level_lease_under_deny_gives_tokens_that_read_and_no_secret_but_the_runtimes(
    client, hub, github, hub_db
):
    github_repos(github)
    values = secrets_of(client, hub)
    pref(client, hub["owner"], K7="deny")
    run_id = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    answer = leased(client, worker, run_id)
    leases = by_name(answer)
    assert set(leases) == {"claude-oauth", f"github-app:{MINE}", f"github-app:{THEIRS}"}
    assert leases["claude-oauth"]["value"] == values["claude"]
    for owner, name in ((MINE, "evo-agents"), (THEIRS, "lumen-identity")):
        token = leases[f"github-app:{owner}"]
        assert permissions(github, token)["contents"] == "read"
        assert token["url_prefix"] == f"https://github.com/{owner}/{name}"  # that repo alone
    assert values["stripe"] not in str(answer) and values["kb"] not in str(answer)
    reason = missing(answer)["lumen-kb-docs"][1]
    assert "K7 (use a credential that writes or reaches a real environment) is deny" in reason
    assert f"set by pref:{OWNER}" in reason
    ((target, login, _, _),) = audit_rows(hub_db, "autonomy.refused")
    assert target.startswith(f"run:{run_id} K7 lease ") and f"mode=deny source=pref:{OWNER}" in target
    assert "env:stripe" in target and "env:stripe-test" in target and "git:lumen-kb-docs" in target
    assert f"github:{MINE}/evo-agents" in target and login == OWNER


def test_autonomy_level_lease_under_run_branch_writes_only_where_a_ruleset_keeps_the_app_off(
    client, hub, github, hub_db
):
    github_repos(github)
    values = secrets_of(client, hub)
    pref(client, hub["owner"], K7="auto:run_branch")
    run_id = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    answer = leased(client, worker, run_id)
    leases = by_name(answer)
    assert set(leases) == {"claude-oauth", f"github-app:{MINE}", f"github-app:{THEIRS}"}
    mine, theirs = leases[f"github-app:{MINE}"], leases[f"github-app:{THEIRS}"]
    assert permissions(github, mine)["contents"] == "write" and mine["url_prefix"] == f"https://github.com/{MINE}"
    assert permissions(github, theirs)["contents"] == "read"
    assert theirs["url_prefix"] == f"https://github.com/{THEIRS}/lumen-identity"
    # a GitLab origin whose default branch the hub cannot see forbids a push gets no git secret, with the reason
    reason = missing(answer)["lumen-kb-docs"][1]
    assert "no forge secret on GitLab" in reason and "K7 (" in reason and "auto:run_branch" in reason
    assert values["kb"] not in str(answer) and values["stripe-test"] not in str(answer)
    ((target, *_),) = audit_rows(hub_db, "autonomy.refused")
    assert f"github:{THEIRS}/lumen-identity" in target and f"github:{MINE}/evo-agents" not in target
    assert "mode=auto:run_branch" in target


def test_autonomy_level_lease_under_test_secrets_adds_the_env_secrets_labelled_test(client, hub, github):
    github_repos(github)
    values = secrets_of(client, hub)
    pref(client, hub["owner"], K7="auto:test_secrets")
    run_id = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    answer = leased(client, worker, run_id)
    leases = by_name(answer)
    assert {"claude-oauth", "stripe-test"} <= set(leases) and "stripe" not in leases
    assert leases["stripe-test"]["value"] == values["stripe-test"]
    assert permissions(github, leases[f"github-app:{THEIRS}"])["contents"] == "read"


def test_autonomy_level_lease_secret_environment_is_a_label_of_env_secrets_alone(client, hub):
    listed = client.get("/v1/secrets", headers=hub["owner"])
    assert listed.status_code == 200
    put_secret(
        client, hub["owner"], "labelled", {"kind": "env", "env_var": "X_KEY", "value": "v", "environment": "test"}
    )
    (shown,) = [item for item in client.get("/v1/secrets", headers=hub["owner"]).json() if item["name"] == "labelled"]
    assert shown["environment"] == "test"
    body = {"kind": "git", "url_prefix": GITLAB_KB, "value": "v", "environment": "test", "projects": [PROJECT]}
    refused = client.put("/v1/secrets/git-labelled", json=body, headers=hub["owner"])
    assert refused.status_code == 422 and "environment" in refused.text
    other = {"kind": "env", "env_var": "X_KEY", "value": "v", "environment": "prod", "projects": [PROJECT]}
    assert client.put("/v1/secrets/prod", json=other, headers=hub["owner"]).status_code == 422
