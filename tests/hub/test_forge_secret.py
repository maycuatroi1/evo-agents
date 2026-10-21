"""Secrets of kind forge: the GitLab token of a project that only the hub's own process opens, to drive the project's
merge requests, and that no run ever holds (``evo_agents.hub.server.secrets``, ``evo_agents.hub.server.forge``).

The checks step 3 of plan gitlab-merge-queue names (criterion a3): a forge secret has a url_prefix as a git secret has,
an end, and its value from the owner once; it is never leased to a run, not even one whose origin it covers, and no
answer to an agent or a worker carries it; the hub opens the one whose url_prefix covers an origin the longest and
drives GitLab with it; a project without one gets no token, so its merge requests stay open for a person. Around them:
writing one needs the admin role on each project, and an owner who loses that role, a secret past its end and one
deleted give no token; no log record, table or audit row holds a value.
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.hub import tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.forge import forge_token
from evo_agents.hub.server.gitlab import GitLabProject
from evo_agents.hub.server.sealing import KEY_BYTES, Sealer
from tests.hub import test_credentials_api as leases_api
from tests.hub.fake_gitlab import FakeGitLab
from tests.hub.live import sql, table_dump
from tests.hub.test_credentials_api import GITLAB_KB, ORIGINS, OWNER
from tests.hub.test_runs import PROJECT

KB_HOST = "gitlab.lumenops.example"
GROUP = f"https://{KB_HOST}/fir-gb-lumen"  # covers the origin of lumen-kb-docs, git@gitlab.lumenops.example:...
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
WRITER_GRANT = {"role": "writer", "max_level": "internal"}


def sample(prefix: str = "glpat-") -> str:
    """A value drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


def in_days(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


@pytest.fixture
def config(hub_db, tmp_path, github):
    changes = live.web_changes(github)
    return live.hub_config(hub_db, tmp_path, github, secrets_key=secrets.token_bytes(KEY_BYTES), **changes)


@pytest.fixture
def client(config):
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """Project evo-agents with the repos of ``tests.hub.test_credentials_api`` and its plan; owner and someone-else
    are writers."""
    return leases_api.members(client, github)


def grant(client, hub, login: str, body: dict) -> None:
    response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=body, headers=hub["admin"])
    assert response.status_code == 200, response.text


def forge(url_prefix: str, value: str, **extra) -> dict:
    return {
        "kind": "forge",
        "url_prefix": url_prefix,
        "projects": [PROJECT],
        "expires_at": in_days(90),
        "value": value,
        **extra,
    }


def put(client, headers, name: str, body: dict):
    return client.put(f"/v1/secrets/{name}", json=body, headers=headers)


def project_id(db, name: str = PROJECT) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == name))[0][0]


def token_for(client, project: int, origin: str, sealer="hub"):
    """What ``forge_token`` gives the hub's process for ``origin`` of ``project``, in a transaction of its own."""
    engine = client.app.state.engine
    opener = client.app.state.sealer if sealer == "hub" else sealer

    async def main():
        async with engine.begin() as conn:
            return await forge_token(conn, opener, project, origin)

    return client.portal.call(main)


# Writing one


def test_a_forge_secret_needs_the_admin_role_on_each_project_and_is_listed_without_its_value(client, hub, hub_db):
    value = sample()
    refused = put(client, hub["owner"], "gitlab-merge", forge(GROUP, value))
    assert refused.status_code == 403, refused.text
    assert refused.json()["message"] == (
        f"a secret of kind forge for project {PROJECT} needs the admin role on it; you hold writer"
    )
    grant(client, hub, OWNER, ADMIN_GRANT)
    written = put(client, hub["owner"], "gitlab-merge", forge(GROUP + "/", value))
    assert written.status_code == 200, written.text
    body = written.json()
    assert (body["kind"], body["url_prefix"], body["username"], body["env_var"]) == ("forge", GROUP, None, None)
    assert (body["projects"], body["workers"], body["created"]) == ([PROJECT], [], True)
    assert body["expires_at"] is not None and value not in written.text

    listed = client.get("/v1/secrets", headers=hub["owner"])
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    (secret,) = listed.json()
    assert (secret["name"], secret["kind"], secret["url_prefix"]) == ("gitlab-merge", "forge", GROUP)
    assert "value" not in secret and value not in listed.text
    # a writer may still write the git and env secrets of the project, as before
    git = {"kind": "git", "url_prefix": GROUP, "projects": [PROJECT], "value": sample()}
    assert put(client, hub["other"], "gitlab-push", git).status_code == 200


@pytest.mark.parametrize(
    "change, problem",
    [
        ({"expires_at": None}, "a secret of kind forge needs expires_at"),
        ({"username": "oauth2"}, "a secret of kind forge takes no username"),
        ({"workers": ["mac-mini"]}, "a secret of kind forge binds to no worker"),
        ({"url_prefix": None}, "a secret of kind forge needs url_prefix"),
        ({"url_prefix": "https://github.com/northwind"}, "a secret of kind forge is for GitLab"),
        ({"url_prefix": "https://user:pw@gitlab.example.org/northwind"}, "url_prefix may not hold a user"),
        ({"env_var": "GITLAB_TOKEN"}, "env_var belongs to a secret of kind env, not forge"),
        ({"value": "glpat-first\nglpat-second"}, "the value of a secret of kind forge is one line"),
        ({"expires_at": in_days(-1)}, "expires_at is past already"),
    ],
)
def test_a_forge_secret_needs_an_end_and_a_gitlab_url_prefix_and_takes_no_username_worker_or_second_line(
    client, hub, hub_db, change, problem
):
    grant(client, hub, OWNER, ADMIN_GRANT)
    body = {key: value for key, value in {**forge(GROUP, sample()), **change}.items() if value is not None}
    refused = put(client, hub["owner"], "gitlab-merge", body)
    assert refused.status_code == 422, refused.text
    assert problem in refused.json()["message"]
    assert body["value"] not in refused.text
    assert sql(hub_db, select(tables.secrets.c.id)) == []


# Never a run's


def test_a_run_is_never_leased_a_forge_secret_even_one_covering_its_origin(client, hub, hub_db):
    grant(client, hub, OWNER, ADMIN_GRANT)
    merge_token, oauth, push_token = sample(), sample("oauth-"), sample("glpat-push-")
    assert put(client, hub["owner"], "gitlab-merge", forge(GROUP, merge_token)).status_code == 200
    env = {"kind": "env", "env_var": "CLAUDE_CODE_OAUTH_TOKEN", "projects": [PROJECT], "value": oauth}
    assert put(client, hub["owner"], "claude-oauth", env).status_code == 200
    worker = leases_api.worker_of(client, hub["owner"], "mac-mini")
    run_id = leases_api.step_run(client, hub["owner"], "lumen-kb-docs", worker)

    asked = leases_api.ask(client, worker["headers"], run_id)
    assert asked.status_code == 200, asked.text
    answer = asked.json()
    assert set(leases_api.by_name(answer)) == {"claude-oauth"}
    assert leases_api.missing(answer) == {
        "lumen-kb-docs": (
            ORIGINS["lumen-kb-docs"],
            f"no git secret of {OWNER} bound to project {PROJECT} covers {ORIGINS['lumen-kb-docs']}",
        )
    }
    assert merge_token not in asked.text

    # with a git secret for the same origin the run gets that one, and still not the forge secret
    git = {"kind": "git", "url_prefix": GITLAB_KB, "projects": [PROJECT], "value": push_token}
    assert put(client, hub["owner"], "gitlab-push", git).status_code == 200
    again = leases_api.ask(client, worker["headers"], run_id)
    assert set(leases_api.by_name(again.json())) == {"claude-oauth", "gitlab-push"}
    assert merge_token not in again.text
    leases = tables.credential_leases
    forge_id = sql(hub_db, select(tables.secrets.c.id).where(tables.secrets.c.name == "gitlab-merge"))[0][0]
    assert sql(hub_db, select(leases.c.id).where(leases.c.secret_id == forge_id)) == []
    view = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/credentials", headers=hub["owner"])
    assert view.status_code == 200, view.text
    assert {lease["name"] for lease in view.json()} == {"claude-oauth", "gitlab-push"}


# The hub's own


def test_the_hub_opens_the_forge_secret_covering_an_origin_the_longest_prefix_first(client, hub, hub_db, caplog):
    grant(client, hub, OWNER, ADMIN_GRANT)
    group, repo, other = sample("glpat-group-"), sample("glpat-repo-"), sample("glpat-other-")
    for name, prefix, value in (
        ("forge-group", GROUP, group),
        ("forge-repo", GITLAB_KB, repo),
        ("forge-other", "https://gitlab.example.org/northwind", other),
    ):
        assert put(client, hub["owner"], name, forge(prefix, value)).status_code == 200
    project = project_id(hub_db)
    kb = ORIGINS["lumen-kb-docs"]

    token = token_for(client, project, kb)
    assert (token.name, token.url_prefix, token.value) == ("forge-repo", GITLAB_KB, repo)
    assert token.project == GitLabProject(f"https://{KB_HOST}/api/v4", "fir-gb-lumen/lumen-kb-docs")
    assert repo not in repr(token) and repo not in str(token)
    wiki = token_for(client, project, f"https://{KB_HOST}/fir-gb-lumen/lumen-wiki.git")
    assert (wiki.name, wiki.value) == ("forge-group", group)
    assert token_for(client, project, f"https://{KB_HOST}/fir-gb-lumenops/x") is None  # not a whole leading part
    assert token_for(client, project, ORIGINS["evo-agents"]) is None

    # A project without a forge secret for an origin, or a hub without its key, has no token: the merge requests
    # there stay open for a person.
    assert token_for(client, live.add_project(hub_db, "docs"), kb) is None
    assert token_for(client, project, kb, sealer=None) is None
    caplog.set_level(logging.WARNING)
    assert token_for(client, project, kb, sealer=Sealer(secrets.token_bytes(KEY_BYTES))) is None
    assert {r.getMessage() for r in caplog.records} >= {"a forge secret does not open; it is passed over"}

    # Deleted, past its end, or its owner no longer an admin of the project: the next one, then none.
    assert client.delete("/v1/secrets/forge-repo", headers=hub["owner"]).status_code == 204
    assert token_for(client, project, kb).name == "forge-group"
    s = tables.secrets
    sql(hub_db, update(s).where(s.c.name == "forge-group").values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    assert token_for(client, project, kb) is None
    assert token_for(client, project, "https://gitlab.example.org/northwind/lumen-app").name == "forge-other"
    grant(client, hub, OWNER, WRITER_GRANT)
    assert token_for(client, project, "https://gitlab.example.org/northwind/lumen-app") is None


def test_the_hub_drives_gitlab_with_the_opened_forge_secret(client, hub, hub_db):
    grant(client, hub, OWNER, ADMIN_GRANT)
    value = sample()
    assert put(client, hub["owner"], "gitlab-merge", forge(GROUP, value)).status_code == 200
    fake = FakeGitLab(value, host=KB_HOST)
    kb = fake.add_project("fir-gb-lumen/lumen-kb-docs")
    mr = fake.add_merge_request(kb, "plan/kb-index", labels=["evo-merge"])
    token = token_for(client, project_id(hub_db), ORIGINS["lumen-kb-docs"])

    async def work():
        async with token.gitlab(transport=fake.transport()) as gl:
            found = await gl.merge_request(token.project.path, mr.iid)
            return found, await gl.merge(token.project.path, mr.iid, found.sha)

    found, merged = client.portal.call(work)
    assert (found.source_branch, found.labels, merged.state) == ("plan/kb-index", ("evo-merge",), "merged")
    assert [str(r.url).partition("?")[0] for r in fake.requests] == [
        f"https://{KB_HOST}/api/v4/projects/fir-gb-lumen%2Flumen-kb-docs/merge_requests/1",
        f"https://{KB_HOST}/api/v4/projects/fir-gb-lumen%2Flumen-kb-docs/merge_requests/1/merge",
    ]
    assert all(r.headers["private-token"] == value for r in fake.requests)


def test_neither_a_log_record_nor_a_table_nor_the_audit_holds_a_forge_value(client, hub, hub_db, caplog):
    caplog.set_level(logging.DEBUG)
    grant(client, hub, OWNER, ADMIN_GRANT)
    first, second = sample(), sample()
    assert put(client, hub["owner"], "gitlab-merge", forge(GROUP, first)).status_code == 200
    assert put(client, hub["owner"], "gitlab-merge", forge(GROUP, second)).status_code == 200  # replaced whole
    fake = FakeGitLab(second, host=KB_HOST)
    fake.add_merge_request(fake.add_project("fir-gb-lumen/lumen-kb-docs"), "plan/kb-index")
    token = token_for(client, project_id(hub_db), ORIGINS["lumen-kb-docs"])
    assert token.value == second

    async def work():
        async with token.gitlab(transport=fake.transport()) as gl:
            await gl.merge_request(token.project.path, 1)
            fake.queue(500)
            try:
                await gl.merge_request(token.project.path, 1)
            except Exception as exc:  # GitLabUnavailable
                return str(exc)

    shown = client.portal.call(work)
    assert "GitLab answered 500" in shown
    trail = client.get("/v1/admin/audit", params={"action": "secret.put"}, headers=hub["admin"])
    assert trail.status_code == 200, trail.text
    raw = caplog.text + "\n".join(f"{r.getMessage()} {r.args!r} {vars(r)!r}" for r in caplog.records)
    dump = table_dump(hub_db) + trail.text + shown
    for value in (first, second):
        assert value not in raw, "a forge secret's value reached a log record"
        assert value not in dump, "a forge secret's value reached a table, the audit or an error"
