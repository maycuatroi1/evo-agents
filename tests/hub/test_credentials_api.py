"""The leases of a run: POST and DELETE /v1/worker/runs/{id}/credentials, and the hub giving them back by itself when
a run leaves the held states, when the reaper ends it and when its worker is revoked
(``evo_agents.hub.server.credentials``).

The checks step 6 of the worker-credentials plan names: another worker's run and a run that ended are refused; a
machine token and a web session get 403; a secret bound to a worker goes to that worker only; an SSH origin on GitLab
is covered by an https url_prefix; a run that ends and a run the reaper ends make the fake GitHub receive DELETE
/installation/token; revoking a worker revokes every lease still out; and no audit row holds a value. Around them: the
token covers the run's repos alone, asking again keeps the leases until the GitHub token nears its end, the worker
gives its leases back, a GitHub outage leaves the revocation to the reaper, the hub without its key or its App says
why, and the pruning drops the sealed tokens past their end."""

import functools
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from evo_agents.hub.credentials import GITHUB_TOKEN_REFRESH_SECONDS, Lease
from evo_agents.hub.server import run_state
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.credentials import GITHUB_GIT_USERNAME
from evo_agents.hub.server.sealing import KEY_BYTES, Sealed, Sealer, lease_aad
from tests.hub.fake_github import Account
from tests.hub.live import ADMIN, bearer, sql, table_dump
from tests.hub.test_plans import registration as plans_registration
from tests.hub.test_runs import PROJECT, PROTOCOL, add_worker, claim, expire, moved, report, state_of
from tests.hub.test_web_auth import cookie, csrf_for, web_sign_in

PLAN = "credentials-smoke"
ADMIN_ID, OWNER, OWNER_ID, OTHER, OTHER_ID = 302, "owner", 501, "someone-else", 502
MINE, THEIRS = "maycuatroi1", "hawkteam404"
ORIGINS = {
    "evo-agents": f"https://github.com/{MINE}/evo-agents.git",
    "m1-identity": f"git@github.com:{THEIRS}/m1-identity.git",
    "m1-kb-docs": "git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git",
    "notes": None,
}
STEPS = {"evo-agents": 1, "m1-kb-docs": 2, "m1-identity": 3, "notes": 4}
CHECKOUTS = {f"{PROJECT}/{name}": {"path": f"/src/{name}", "branch": "main"} for name in ORIGINS}
WRITER = {"role": "writer", "max_level": "internal"}
GITLAB_KB = "https://gitlab.m1ops.com/fis-gb-m1/m1-kb-docs"


def sample(prefix: str = "sk-sample-") -> str:
    """A value drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


@pytest.fixture(scope="module")
def app_key():
    """The GitHub App's key pair for this module: the private PEM the hub signs with, the public one the fake checks."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public.decode()


def hub_config(hub_db, tmp_path, github, app_key, *, key: bool = True, app: bool = True):
    github.app_public_key = app_key[1]
    changes = live.web_changes(github)
    if key:
        changes["secrets_key"] = secrets.token_bytes(KEY_BYTES)
    if app:
        changes |= {"github_app_id": github.app_id, "github_app_private_key": app_key[0]}
    return live.hub_config(hub_db, tmp_path, github, **changes)


@pytest.fixture
def config(hub_db, tmp_path, github, app_key):
    return hub_config(hub_db, tmp_path, github, app_key)


@pytest.fixture
def client(config):
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def plan_body() -> dict:
    return {
        "id": PLAN,
        "goal": "Lease each run what it needs, and take it back.",
        "repos": [{"repo": name, "branch": "main", "status": "pending"} for name in ORIGINS],
        "steps": [
            {"id": key, "title": f"Push {repo}", "repo": repo, "what": f"a commit on {repo}", "status": "pending"}
            for repo, key in STEPS.items()
        ],
    }


def registration(origins: dict | None = None) -> dict:
    body = plans_registration()
    body["repos"] = [
        {"name": name, "path": name, **({"origin": origin} if origin else {})}
        for name, origin in (origins or ORIGINS).items()
    ]
    return body


def members(client, github) -> dict:
    """The project with its four repos, where owner and someone-else are writers, and the plan pushed by owner: the
    headers of the owner, the other member and a hub admin."""
    headers = {
        "admin": bearer(live.sign_in(client, github, ADMIN, ADMIN_ID)["token"]),
        "owner": bearer(live.sign_in(client, github, OWNER, OWNER_ID)["token"]),
        "other": bearer(live.sign_in(client, github, OTHER, OTHER_ID)["token"]),
    }
    assert client.put(f"/v1/projects/{PROJECT}", json=registration(), headers=headers["admin"]).status_code == 200
    for login in (OWNER, OTHER):
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=WRITER, headers=headers["admin"])
        assert response.status_code == 200, response.text
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": plan_body()}, headers=headers["owner"])
    assert pushed.status_code == 200, pushed.text
    return headers


@pytest.fixture
def hub(client, github) -> dict:
    return members(client, github)


def install(github, *repos: str) -> int:
    """The App installed on MINE for ``repos``, each of which owner may push to on GitHub."""
    installation = github.install(MINE, *repos)
    for repo in repos:
        github.collaborate(MINE, repo, Account(OWNER, OWNER_ID), "write")
    return installation


def worker_of(client, headers, name: str, slots: int = 1) -> dict:
    return add_worker(client, headers, name, slots=slots, checkouts=CHECKOUTS)


def put_secret(client, headers, name: str, body: dict) -> None:
    response = client.put(f"/v1/secrets/{name}", json={"projects": [PROJECT], **body}, headers=headers)
    assert response.status_code == 200, response.text


def env_secret(client, headers, name: str, env_var: str, value: str, **extra) -> None:
    put_secret(client, headers, name, {"kind": "env", "env_var": env_var, "value": value, **extra})


def git_secret(client, headers, name: str, url_prefix: str, value: str, **extra) -> None:
    put_secret(client, headers, name, {"kind": "git", "url_prefix": url_prefix, "value": value, **extra})


def step_run(client, headers, repo: str, worker: dict, **extra) -> int:
    """A run of the step on ``repo``, pinned to ``worker`` and claimed by it."""
    body = {"plan_id": PLAN, "steps": [STEPS[repo]], "worker_id": worker["id"], **extra}
    response = client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    run_id = response.json()[0]["id"]
    assert claim(client, worker)["id"] == run_id
    return run_id


def plan_run(client, headers, worker: dict) -> int:
    """A plan run over the four repos, pinned to ``worker`` and claimed by it."""
    body = {"plan_id": PLAN, "worker_id": worker["id"]}
    response = client.post(f"/v1/projects/{PROJECT}/plan-runs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    run = claim(client, worker)
    assert run["id"] == response.json()["id"] and run["kind"] == "plan"
    return run["id"]


def ask(client, headers: dict, run_id: int):
    return client.post(f"/v1/worker/runs/{run_id}/credentials", headers=headers)


def leased(client, worker: dict, run_id: int) -> dict:
    response = ask(client, worker["headers"], run_id)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def give_back(client, headers: dict, run_id: int):
    return client.delete(f"/v1/worker/runs/{run_id}/credentials", headers=headers)


def by_name(answer: dict) -> dict:
    return {lease["name"]: lease for lease in answer["leases"]}


def missing(answer: dict) -> dict:
    return {item["repo"]: (item["origin"], item["reason"]) for item in answer["missing"]}


def revoked_at_github(github) -> list[str]:
    """The tokens the fake GitHub was asked to revoke, in order."""
    return [r.headers["authorization"].partition(" ")[2] for r in github.calls("/installation/token")]


def leases_of(db, run_id: int) -> list[tuple]:
    """(id, provider, secret name, target, revoked, sealed, external_id) of each lease of the run."""
    return sql(
        db,
        "SELECT l.id, l.provider, s.name, l.target, l.revoked_at IS NOT NULL, l.sealed_value IS NOT NULL, "
        "l.external_id FROM credential_leases l LEFT JOIN secrets s ON s.id = l.secret_id WHERE l.run_id = %s "
        "ORDER BY l.id",
        (run_id,),
    )


def secret_ids(db, *names: str, owner: str = OWNER) -> str:
    """The ids of ``owner``'s live secrets ``names``, as an audit row lists them: ``12,15``."""
    rows = sql(
        db,
        "SELECT s.id FROM secrets s JOIN users u ON u.id = s.owner_id "
        "WHERE u.login = %s AND s.name = ANY(%s) AND s.deleted_at IS NULL ORDER BY s.id",
        (owner, list(names)),
    )
    assert len(rows) == len(names), (names, rows)
    return ",".join(str(row[0]) for row in rows)


def audit_rows(db, action: str) -> list[tuple]:
    return sql(
        db,
        "SELECT a.target, u.login, p.name, a.token_id IS NOT NULL FROM audit a LEFT JOIN users u ON u.id = a.actor_id "
        "LEFT JOIN projects p ON p.id = a.project_id WHERE a.action = %s ORDER BY a.id",
        (action,),
    )


def reap(client) -> dict:
    state = client.app.state
    reaper = functools.partial(run_state.recover_runs, state.pool, sealer=state.sealer, github_app=state.github_app)
    return client.portal.call(reaper)


# What a run gets


def test_a_run_gets_its_owners_secrets_and_an_app_token_for_its_own_github_repos(client, hub, github, hub_db, config):
    install(github, "evo-agents", "agent-skills")
    oauth, glpat, theirs = sample("oauth-"), sample("glpat-"), sample("other-")
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", oauth)
    git_secret(client, hub["owner"], "gitlab-kb", GITLAB_KB, glpat)
    env_secret(client, hub["other"], "openai", "OPENAI_API_KEY", theirs)  # another member's, for the same project
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = plan_run(client, hub["owner"], worker)

    answer = leased(client, worker, run_id)
    leases = by_name(answer)
    assert set(leases) == {"claude-oauth", "gitlab-kb", f"github-app:{MINE}"}
    env, git, app = leases["claude-oauth"], leases["gitlab-kb"], leases[f"github-app:{MINE}"]
    assert (env["kind"], env["provider"], env["env_var"], env["value"]) == (
        "env",
        "secret",
        "CLAUDE_CODE_OAUTH_TOKEN",
        oauth,
    )
    assert (env["url_prefix"], env["username"], env["expires_at"]) == (None, None, None)
    assert (git["kind"], git["url_prefix"], git["username"], git["value"]) == ("git", GITLAB_KB, "oauth2", glpat)
    assert (app["kind"], app["provider"], app["url_prefix"], app["username"]) == (
        "git",
        "github-app",
        f"https://github.com/{MINE}",
        GITHUB_GIT_USERNAME,
    )
    expires = datetime.fromisoformat(app["expires_at"])
    assert timedelta(minutes=55) < expires - datetime.now(timezone.utc) <= timedelta(hours=1)
    # the token opens the run's repo of that owner, and not another repo the App is installed on
    assert github.covers(app["value"], MINE, "evo-agents")
    assert not github.covers(app["value"], MINE, "agent-skills")
    (asked,) = github.calls("/app/installations/1001/access_tokens")
    assert json.loads(asked.body)["repositories"] == ["evo-agents"]
    assert theirs not in json.dumps(answer)

    # each repo nothing covers comes back with its origin and why
    assert missing(answer) == {
        "m1-identity": (ORIGINS["m1-identity"], f"the GitHub App is not installed on {THEIRS}/m1-identity"),
        "notes": (None, f"project {PROJECT} lists no origin for notes"),
    }
    # the daemon reads the leases with the shared model, whose repr leaves the value out
    for lease in answer["leases"]:
        read = Lease.from_json(lease)
        assert read.value == lease["value"] and lease["value"] not in repr(read)

    rows = {row[2] or row[1]: row for row in leases_of(hub_db, run_id)}
    assert rows["claude-oauth"][1:] == ("secret", "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", False, False, None)
    assert rows["gitlab-kb"][1:] == ("secret", "gitlab-kb", GITLAB_KB, False, False, None)
    lease_id = rows["github-app"][0]
    target = f"https://github.com/{MINE}/evo-agents"
    assert rows["github-app"][1:] == ("github-app", None, target, False, True, "1001")
    # the token is kept sealed under its lease's id, for the revocation
    ((sealed, nonce, key_id),) = sql(
        hub_db, "SELECT sealed_value, nonce, key_id FROM credential_leases WHERE id = %s", (lease_id,)
    )
    assert Sealer(config.secrets_key).open(Sealed(sealed, nonce, key_id), lease_aad(lease_id)) == app["value"]

    repos = "evo-agents,m1-identity,m1-kb-docs,notes"
    secrets = secret_ids(hub_db, "claude-oauth", "gitlab-kb")  # by id: their names are the owner's
    target = f"{PROJECT}/{PLAN} run:{run_id} secrets={secrets} github-app={MINE} repos={repos}"
    assert audit_rows(hub_db, "credential.lease") == [(target, OWNER, PROJECT, True)]


def test_a_repo_of_an_account_the_owner_cannot_push_to_gets_no_app_token(client, hub, github, hub_db):
    """A project's admin can register any origin: one naming another GitHub account, on which the App is installed,
    gets no token unless the run's owner may push to that repo on GitHub."""
    github.install(THEIRS, "m1-identity")
    github.accounts[OWNER] = OWNER_ID
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = step_run(client, hub["owner"], "m1-identity", worker)

    answer = leased(client, worker, run_id)
    assert answer["leases"] == []
    assert missing(answer) == {
        "m1-identity": (
            ORIGINS["m1-identity"],
            f"{OWNER} cannot push to {THEIRS}/m1-identity on GitHub: their role there is none, and pushing needs write",
        )
    }
    assert leases_of(hub_db, run_id) == []
    # the token made to ask GitHub opened the repo: it was revoked, and is in no lease
    (made,) = github.app_tokens
    assert revoked_at_github(github) == [made] and not github.covers(made, THEIRS, "m1-identity")
    (target, *_), *_ = audit_rows(hub_db, "credential.lease")
    assert target.endswith(" github-app=- repos=m1-identity")

    for role, covered in (("read", False), ("triage", False), ("write", True)):
        github.collaborate(THEIRS, "m1-identity", Account(OWNER, OWNER_ID), role)
        answer = leased(client, worker, run_id)
        assert (f"github-app:{THEIRS}" in by_name(answer)) is covered, role
    token = by_name(answer)[f"github-app:{THEIRS}"]["value"]
    assert github.covers(token, THEIRS, "m1-identity")


def test_an_ssh_origin_on_gitlab_is_covered_by_an_https_url_prefix_the_longest_first(client, hub, hub_db, github):
    group = sample("glpat-")
    git_secret(client, hub["owner"], "gitlab-host", "https://gitlab.m1ops.com", sample("glpat-"))
    git_secret(client, hub["owner"], "gitlab-group", "https://gitlab.m1ops.com/fis-gb-m1", group)
    git_secret(client, hub["owner"], "gitlab-near", "https://gitlab.m1ops.com/fis-gb", sample("glpat-"))
    git_secret(client, hub["owner"], "gitlab-agents", "https://gitlab.m1ops.com/fis-gb-m1/m1-cloud-agents", sample())
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = step_run(client, hub["owner"], "m1-kb-docs", worker)

    answer = leased(client, worker, run_id)
    assert list(by_name(answer)) == ["gitlab-group"] and answer["missing"] == []
    assert by_name(answer)["gitlab-group"]["value"] == group
    assert [row[2:4] for row in leases_of(hub_db, run_id)] == [("gitlab-group", GITLAB_KB)]

    # the other SSH form, with a port of SSH, is the same origin; a host the secrets do not name is not
    for origin, covered in (
        ("ssh://git@gitlab.m1ops.com:2222/fis-gb-m1/m1-kb-docs.git", True),
        ("https://GitLab.m1ops.com/fis-gb-m1/m1-kb-docs.git/", True),
        ("git@gitlab.example.org:fis-gb-m1/m1-kb-docs.git", False),
    ):
        body = registration({**ORIGINS, "m1-kb-docs": origin})
        assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=hub["admin"]).status_code == 200
        answer = leased(client, worker, run_id)
        assert ("gitlab-group" in by_name(answer)) is covered, origin
        if not covered:
            assert missing(answer) == {
                "m1-kb-docs": (origin, f"no git secret of {OWNER} bound to project {PROJECT} covers {origin}")
            }
    assert not github.calls("/app/installations/1001/access_tokens")  # nothing here is on github.com


def test_a_secret_bound_to_a_worker_goes_to_that_worker_alone(client, hub, hub_db):
    box, desk = worker_of(client, hub["owner"], "box"), worker_of(client, hub["owner"], "desk")
    values = {name: sample(f"{name}-") for name in ("everywhere", "box-api", "box-only", "stale", "theirs")}
    env_secret(client, hub["owner"], "everywhere", "API_TOKEN", values["everywhere"])
    env_secret(client, hub["owner"], "box-api", "API_TOKEN", values["box-api"], workers=["box"])
    env_secret(client, hub["owner"], "box-only", "BOX_TOKEN", values["box-only"], workers=["box"])
    env_secret(client, hub["owner"], "stale", "STALE_TOKEN", values["stale"])
    sql(hub_db, "UPDATE secrets SET expires_at = now() - interval '1 minute' WHERE name = 'stale'")
    env_secret(client, hub["other"], "theirs", "THEIR_TOKEN", values["theirs"])
    on_box = step_run(client, hub["owner"], "notes", box)
    on_desk = step_run(client, hub["owner"], "m1-kb-docs", desk)

    # on its own worker a secret wins over one of the same variable bound to any worker
    box_leases = by_name(leased(client, box, on_box))
    assert {name: lease["env_var"] for name, lease in box_leases.items()} == {
        "box-api": "API_TOKEN",
        "box-only": "BOX_TOKEN",
    }
    assert box_leases["box-api"]["value"] == values["box-api"]
    desk_leases = by_name(leased(client, desk, on_desk))
    assert {name: lease["value"] for name, lease in desk_leases.items()} == {"everywhere": values["everywhere"]}


# Who may ask


def test_another_workers_run_and_a_run_that_ended_are_refused(client, hub, hub_db):
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    box, desk = worker_of(client, hub["owner"], "box"), worker_of(client, hub["owner"], "desk")
    run_id = step_run(client, hub["owner"], "notes", box)
    for response in (ask(client, desk["headers"], run_id), give_back(client, desk["headers"], run_id)):
        assert response.status_code == 404, response.text
    queued = client.post(
        f"/v1/projects/{PROJECT}/runs", json={"plan_id": PLAN, "steps": [2]}, headers=hub["owner"]
    ).json()[0]["id"]
    for unheld in (queued, 999_999):
        refused = ask(client, box["headers"], unheld)
        assert refused.status_code == 404 and "does not hold run" in refused.json()["message"]
    assert leases_of(hub_db, run_id) == []

    assert len(leased(client, box, run_id)["leases"]) == 1
    assert report(client, box, run_id, "failed", error="the agent gave up").status_code == 200
    refused = ask(client, box["headers"], run_id)
    assert refused.status_code == 404 and "does not hold run" in refused.json()["message"]
    # giving back is the worker's to do whatever the run's state; nothing is out any more
    given = give_back(client, box["headers"], run_id)
    assert given.status_code == 200 and given.json() == {"revoked": 0}
    assert [row[4] for row in leases_of(hub_db, run_id)] == [True]
    assert len(audit_rows(hub_db, "credential.lease")) == 1


def test_a_member_whose_writer_grant_goes_gets_no_lease_and_loses_the_ones_out(client, hub, github, hub_db):
    install(github, "evo-agents")
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    env_secret(client, hub["other"], "their-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    run_id = step_run(client, hub["owner"], "evo-agents", worker)
    token = by_name(leased(client, worker, run_id))[f"github-app:{MINE}"]["value"]
    theirs = worker_of(client, hub["other"], "their-box")
    their_run = step_run(client, hub["other"], "notes", theirs)
    assert len(leased(client, theirs, their_run)["leases"]) == 1

    # The admin takes owner's grant away: the leases of owner's runs in the project go back, the token is revoked.
    grant = f"/v1/admin/projects/{PROJECT}/grants/{OWNER}"
    assert client.delete(grant, headers=hub["admin"]).status_code == 204
    assert [(row[4], row[5]) for row in leases_of(hub_db, run_id)] == [(True, False), (True, False)]
    assert revoked_at_github(github) == [token] and not github.covers(token, MINE, "evo-agents")
    secrets = secret_ids(hub_db, "claude-oauth", owner=OWNER)
    target = f"{PROJECT}/{PLAN}#1 run:{run_id} leases=2 secrets={secrets} github-app=1 by=grant-deleted"
    assert audit_rows(hub_db, "credential.revoke") == [(target, ADMIN, PROJECT, True)]
    assert [row[4] for row in leases_of(hub_db, their_run)] == [False], "another member's run keeps its leases"

    # The run is still held by the worker, which asks again: refused while owner holds no writer.
    refused = ask(client, worker["headers"], run_id)
    assert refused.status_code == 403, refused.text
    assert refused.json()["message"] == (
        f"{OWNER}, who dispatched run {run_id}, no longer holds the writer role on project {PROJECT}: the run gets "
        "no credentials"
    )
    assert len(leases_of(hub_db, run_id)) == 2 and len(github.calls("/app/installations/1001/access_tokens")) == 1
    assert give_back(client, worker["headers"], run_id).json() == {"revoked": 0}, "giving back is never refused"

    # Back as writer, then lowered to reader: the same.
    put = {"role": "writer", "max_level": "internal"}
    assert client.put(grant, json=put, headers=hub["admin"]).status_code == 200
    again = by_name(leased(client, worker, run_id))
    assert set(again) == {"claude-oauth", f"github-app:{MINE}"}
    assert client.put(grant, json={**put, "role": "reader"}, headers=hub["admin"]).status_code == 200
    assert [row[4] for row in leases_of(hub_db, run_id)] == [True] * 4
    assert audit_rows(hub_db, "credential.revoke")[-1][0].endswith(" by=grant-reader")
    assert ask(client, worker["headers"], run_id).status_code == 403
    assert revoked_at_github(github)[-1] == again[f"github-app:{MINE}"]["value"]
    # raising a grant takes nothing back
    assert client.put(grant, json=put, headers=hub["admin"]).status_code == 200
    assert len(audit_rows(hub_db, "credential.revoke")) == 2


def test_machine_tokens_and_web_sessions_get_403_and_a_daemon_without_the_protocol_426(client, hub, github, hub_db):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = step_run(client, hub["owner"], "notes", worker)
    session = web_sign_in(client, github, Account(OWNER, OWNER_ID))
    web = {**cookie(session), "X-Evo-CSRF": csrf_for(client, session)}
    for headers in (hub["owner"], hub["admin"], web):
        credential = {**headers, **PROTOCOL}
        for response in (ask(client, credential, run_id), give_back(client, credential, run_id)):
            assert response.status_code == 403, response.text
            assert "worker token" in response.json()["message"]
    without = {key: value for key, value in worker["headers"].items() if key != "X-Evo-Worker-Protocol"}
    assert ask(client, without, run_id).status_code == 426
    assert leases_of(hub_db, run_id) == [] and audit_rows(hub_db, "credential.lease") == []


# Asking again, and giving back


def test_asking_again_keeps_the_leases_until_the_github_token_nears_its_end(client, hub, github, hub_db):
    install(github, "evo-agents")
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = step_run(client, hub["owner"], "evo-agents", worker)
    first = by_name(leased(client, worker, run_id))
    again = by_name(leased(client, worker, run_id))
    assert {name: (lease["id"], lease["value"]) for name, lease in again.items()} == {
        name: (lease["id"], lease["value"]) for name, lease in first.items()
    }
    assert len(github.calls("/app/installations/1001/access_tokens")) == 1

    # less than GITHUB_TOKEN_REFRESH_SECONDS left: a new token in a new lease; the old one works until its end
    left = timedelta(seconds=GITHUB_TOKEN_REFRESH_SECONDS - 60)
    sql(hub_db, "UPDATE credential_leases SET expires_at = now() + %s WHERE provider = 'github-app'", (left,))
    renewed = by_name(leased(client, worker, run_id))
    old, new = first[f"github-app:{MINE}"], renewed[f"github-app:{MINE}"]
    assert new["id"] != old["id"] and new["value"] != old["value"]
    assert renewed["claude-oauth"]["id"] == first["claude-oauth"]["id"]
    assert github.covers(old["value"], MINE, "evo-agents") and github.covers(new["value"], MINE, "evo-agents")
    assert len(github.calls("/app/installations/1001/access_tokens")) == 2
    assert [row[4] for row in leases_of(hub_db, run_id)] == [False, False, False]
    assert len(audit_rows(hub_db, "credential.lease")) == 3


def test_the_worker_gives_back_its_leases_and_github_revokes_the_token(client, hub, github, hub_db):
    install(github, "evo-agents")
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    git_secret(client, hub["owner"], "gitlab-kb", GITLAB_KB, sample("glpat-"))
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = plan_run(client, hub["owner"], worker)
    token = by_name(leased(client, worker, run_id))[f"github-app:{MINE}"]["value"]

    given = give_back(client, worker["headers"], run_id)
    assert given.status_code == 200 and given.json() == {"revoked": 3}
    assert revoked_at_github(github) == [token]
    assert not github.covers(token, MINE, "evo-agents")
    assert [(row[4], row[5]) for row in leases_of(hub_db, run_id)] == [(True, False)] * 3
    secrets = secret_ids(hub_db, "claude-oauth", "gitlab-kb")
    target = f"{PROJECT}/{PLAN} run:{run_id} leases=3 secrets={secrets} github-app=1 by=worker"
    assert audit_rows(hub_db, "credential.revoke") == [(target, OWNER, PROJECT, True)]

    # again: nothing is out, nothing is revoked or audited
    assert give_back(client, worker["headers"], run_id).json() == {"revoked": 0}
    assert len(revoked_at_github(github)) == 1 and len(audit_rows(hub_db, "credential.revoke")) == 1
    # the run is still held: asking again leases anew, with a new token
    fresh = by_name(leased(client, worker, run_id))
    assert fresh[f"github-app:{MINE}"]["value"] != token
    assert len(leases_of(hub_db, run_id)) == 6


# The hub giving them back by itself


def test_a_run_that_ends_or_waits_in_review_gives_back_its_leases_and_github_revokes_the_token(
    client, hub, github, hub_db
):
    install(github, "evo-agents")
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    worker = worker_of(client, hub["owner"], "mac-mini")
    tokens = []
    for end in ("failed", "review"):
        run_id = step_run(client, hub["owner"], "evo-agents", worker)
        tokens.append(by_name(leased(client, worker, run_id))[f"github-app:{MINE}"]["value"])
        moved(client, worker, run_id, "running")
        assert [row[4] for row in leases_of(hub_db, run_id)] == [False, False]  # still held: still leased
        if end == "failed":
            assert report(client, worker, run_id, "failed", error="verify failed").status_code == 200
        else:
            moved(client, worker, run_id, "verifying", "review", commit_sha="a" * 40)
        assert state_of(hub_db, run_id) == end
        assert revoked_at_github(github) == tokens
        assert not github.covers(tokens[-1], MINE, "evo-agents")
        assert [(row[4], row[5]) for row in leases_of(hub_db, run_id)] == [(True, False), (True, False)]
        secrets = secret_ids(hub_db, "claude-oauth")
        target = f"{PROJECT}/{PLAN}#1 run:{run_id} leases=2 secrets={secrets} github-app=1 by=run-{end}"
        assert audit_rows(hub_db, "credential.revoke")[-1] == (target, None, PROJECT, False)  # the hub's own


def test_the_reaper_revokes_the_token_of_a_run_whose_lease_ran_out_and_what_github_failed_to_take(
    client, hub, github, hub_db
):
    install(github, "evo-agents")
    worker = worker_of(client, hub["owner"], "mac-mini")
    lost = step_run(client, hub["owner"], "evo-agents", worker)
    token = by_name(leased(client, worker, lost))[f"github-app:{MINE}"]["value"]
    expire(hub_db, lost)
    assert reap(client)["lost"] == 1
    assert state_of(hub_db, lost) == "lost"
    assert revoked_at_github(github) == [token] and not github.covers(token, MINE, "evo-agents")
    assert [(row[4], row[5]) for row in leases_of(hub_db, lost)] == [(True, False)]
    target = f"{PROJECT}/{PLAN}#1 run:{lost} leases=1 secrets=- github-app=1 by=run-lost"
    assert audit_rows(hub_db, "credential.revoke") == [(target, None, PROJECT, False)]

    # GitHub failing when the run ends leaves the token sealed for the reaper's next pass
    retry = claim(client, worker)  # the next attempt of the lost run, pinned to the same worker
    assert retry["attempt"] == 2
    failed = retry["id"]
    token = by_name(leased(client, worker, failed))[f"github-app:{MINE}"]["value"]
    github.status = 503
    assert report(client, worker, failed, "failed", error="the agent gave up").status_code == 200
    assert [(row[4], row[5]) for row in leases_of(hub_db, failed)] == [(True, True)]
    github.status = None
    assert github.covers(token, MINE, "evo-agents")
    assert reap(client) == {"lost": 0, "failed": 0, "cancelled": 0, "parked": 0}
    assert revoked_at_github(github)[-1] == token and not github.covers(token, MINE, "evo-agents")
    assert [(row[4], row[5]) for row in leases_of(hub_db, failed)] == [(True, False)]
    reap(client)  # nothing left: GitHub is not asked again
    assert revoked_at_github(github).count(token) == 2  # the 503 answer, then the revocation


def test_revoking_a_worker_revokes_every_lease_still_out(client, hub, github, hub_db):
    install(github, "evo-agents")
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
    git_secret(client, hub["owner"], "gitlab-kb", GITLAB_KB, sample("glpat-"))
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    on_github = step_run(client, hub["owner"], "evo-agents", worker)
    on_gitlab = step_run(client, hub["owner"], "m1-kb-docs", worker)
    token = by_name(leased(client, worker, on_github))[f"github-app:{MINE}"]["value"]
    assert len(leased(client, worker, on_gitlab)["leases"]) == 2
    # a run that left the worker without a move, as an older hub could leave one, keeps its leases out
    sql(hub_db, "UPDATE runs SET state = 'done', finished_at = now() WHERE id = %s", (on_github,))

    revoked = client.post(f"/v1/workers/{worker['id']}/revoke", headers=hub["owner"])
    assert revoked.status_code == 200, revoked.text
    assert state_of(hub_db, on_gitlab) == "failed"  # pinned to the worker revoked
    assert sql(hub_db, "SELECT count(*) FROM credential_leases WHERE revoked_at IS NULL") == [(0,)]
    assert sql(hub_db, "SELECT count(*) FROM credential_leases WHERE sealed_value IS NOT NULL") == [(0,)]
    assert revoked_at_github(github) == [token] and not github.covers(token, MINE, "evo-agents")
    both, oauth = secret_ids(hub_db, "claude-oauth", "gitlab-kb"), secret_ids(hub_db, "claude-oauth")
    assert [row[:2] for row in audit_rows(hub_db, "credential.revoke")] == [
        (f"{PROJECT}/{PLAN}#2 run:{on_gitlab} leases=2 secrets={both} github-app=0 by=run-failed", None),
        (f"{PROJECT}/{PLAN}#1 run:{on_github} leases=2 secrets={oauth} github-app=1 by=worker-revoked", OWNER),
    ]

    # revoking a worker's token revokes its worker, and its leases, the same way
    other = worker_of(client, hub["owner"], "desk")
    run_id = step_run(client, hub["owner"], "evo-agents", other)
    token = by_name(leased(client, other, run_id))[f"github-app:{MINE}"]["value"]
    assert client.delete(f"/v1/tokens/{other['token_id']}", headers=hub["owner"]).status_code == 204
    assert [(row[4], row[5]) for row in leases_of(hub_db, run_id)] == [(True, False), (True, False)]
    assert revoked_at_github(github)[-1] == token and not github.covers(token, MINE, "evo-agents")


def test_the_pruning_drops_the_sealed_tokens_past_their_end(client, hub, github, hub_db):
    install(github, "evo-agents")
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = step_run(client, hub["owner"], "evo-agents", worker)
    leased(client, worker, run_id)
    sql(
        hub_db,
        "UPDATE credential_leases SET issued_at = now() - interval '2 hours', expires_at = now() - interval '1 hour'",
    )
    report_ = client.portal.call(run_state.prune_run_events, client.app.state.pool, 30)
    assert report_ == {"deleted": 0, "days": 30, "tokens_dropped": 1}
    assert [(row[4], row[5]) for row in leases_of(hub_db, run_id)] == [(False, False)]
    assert report(client, worker, run_id, "failed", error="the agent gave up").status_code == 200
    assert revoked_at_github(github) == []  # a token past its end is not GitHub's to revoke any more


# A hub without its key or its App


def test_without_the_secrets_key_nothing_is_leased_and_every_repo_says_why(hub_db, tmp_path, github, app_key):
    config = hub_config(hub_db, tmp_path, github, app_key, key=False)
    install(github, "evo-agents")
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        hub = members(client, github)
        worker = worker_of(client, hub["owner"], "mac-mini")
        run_id = plan_run(client, hub["owner"], worker)
        answer = leased(client, worker, run_id)
    assert answer["leases"] == []
    assert set(missing(answer)) == set(ORIGINS)
    assert all("EVO_HUB_SECRETS_KEY" in reason for _, reason in missing(answer).values())
    assert not github.calls("/app/installations/1001/access_tokens") and leases_of(hub_db, run_id) == []
    assert len(audit_rows(hub_db, "credential.lease")) == 1


def test_without_the_github_app_the_github_repos_are_missing_with_its_variables(hub_db, tmp_path, github, app_key):
    config = hub_config(hub_db, tmp_path, github, app_key, app=False)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        assert client.app.state.github_app is None
        hub = members(client, github)
        env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", sample())
        worker = worker_of(client, hub["owner"], "mac-mini")
        run_id = step_run(client, hub["owner"], "evo-agents", worker)
        answer = leased(client, worker, run_id)
    assert list(by_name(answer)) == ["claude-oauth"]
    ((origin, reason),) = missing(answer).values()
    assert origin == ORIGINS["evo-agents"]
    assert "no GitHub App" in reason and "EVO_HUB_GITHUB_APP_ID, EVO_HUB_GITHUB_APP_PRIVATE_KEY" in reason


# The audit and the logs


def test_neither_the_audit_nor_a_log_record_nor_a_table_holds_a_value(client, hub, github, hub_db, caplog):
    caplog.set_level(logging.DEBUG)
    install(github, "evo-agents")
    values = [sample("oauth-"), sample("glpat-")]
    env_secret(client, hub["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", values[0])
    git_secret(client, hub["owner"], "gitlab-kb", GITLAB_KB, values[1])
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = plan_run(client, hub["owner"], worker)
    answer = leased(client, worker, run_id)
    values.append(by_name(answer)[f"github-app:{MINE}"]["value"])
    leased(client, worker, run_id)
    assert give_back(client, worker["headers"], run_id).status_code == 200
    trail = client.get("/v1/admin/audit", params={"action": "credential.lease"}, headers=hub["admin"])
    assert trail.status_code == 200, trail.text
    revokes = client.get("/v1/admin/audit", params={"action": "credential.revoke"}, headers=hub["admin"])
    assert revokes.status_code == 200, revokes.text
    # a hub admin reads the audit, and a secret's name is its owner's alone, as GET .../runs/{id}/credentials says
    for name in ("claude-oauth", "gitlab-kb"):
        assert name not in trail.text and name not in revokes.text, "the audit names a secret by its name"
    assert f"secrets={secret_ids(hub_db, 'claude-oauth', 'gitlab-kb')} " in trail.text

    assert {"credentials leased", "credentials given back"} <= {record.getMessage() for record in caplog.records}
    logged = caplog.text + "\n".join(f"{record.getMessage()} {vars(record)!r}" for record in caplog.records)
    stored = table_dump(hub_db) + trail.text
    for value in values:
        assert value not in logged, "a leased value reached a log record"
        assert value not in stored, "a leased value reached a table or the audit"
