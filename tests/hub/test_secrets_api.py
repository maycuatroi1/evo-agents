"""The owner's secrets on the hub: PUT, GET and DELETE /v1/secrets (``evo_agents.hub.server.secrets``).

The checks step 4 of the worker-credentials plan names: GET never carries a value; a project where the caller is not
a writer is 403; another member's worker is 403; an env_var of DENIED_ENV, or with one of its prefixes, is 422; a hub
admin does not see another owner's secrets; without EVO_HUB_SECRETS_KEY a write is 503; and the audit holds no value.
Around them: the value is sealed under its owner, name and kind and opens to what was written; a git secret keeps its
url_prefix in the form of normalize_origin with oauth2 as the default username; PUT replaces a secret whole; DELETE
drops the sealed value and the bindings and revokes the leases; a web session writes only with X-Evo-CSRF; no log
record and no table holds a value."""

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from evo_agents.hub import tables
from evo_agents.hub.credentials import DEFAULT_GIT_USERNAME, MAX_SECRET_BYTES
from evo_agents.hub.server import secrets as secret_routes
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.sealing import KEY_BYTES, Sealed, Sealer, secret_aad
from tests.hub.fake_github import Account
from tests.hub.live import ADMIN, add_project, bearer, sql, table_dump
from tests.hub.test_run_tables import PLAN, add_plan, add_run
from tests.hub.test_web_auth import cookie, csrf_for, web_sign_in

ADMIN_ID = 302
OWNER, OWNER_ID = "owner", 501
OTHER, OTHER_ID = "someone-else", 502
HOST = {"hostname": "mac-mini.local", "os": "darwin", "arch": "arm64", "agent_version": "0.5.0"}
WRITER = {"role": "writer", "max_level": "internal"}
READER = {"role": "reader", "max_level": "internal"}
SECRET_FIELDS = {
    "name",
    "kind",
    "env_var",
    "url_prefix",
    "username",
    "projects",
    "workers",
    "expires_at",
    "created_at",
    "updated_at",
}


def sample(prefix: str = "sk-sample-") -> str:
    """A value drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


@pytest.fixture
def config(hub_db, tmp_path, github):
    return live.hub_config(
        hub_db, tmp_path, github, secrets_key=secrets.token_bytes(KEY_BYTES), **live.web_changes(github)
    )


@pytest.fixture
def client(config):
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github, hub_db):
    """Projects demo and docs, where the owner is a writer, and notes, where the owner is a reader; another member, a
    writer on demo; a hub admin who is a writer on demo and holds no grant on docs."""
    for name in ("demo", "docs", "notes"):
        add_project(hub_db, name)
    admin = bearer(live.sign_in(client, github, ADMIN, ADMIN_ID)["token"])
    owner = live.sign_in(client, github, OWNER, OWNER_ID)
    other = live.sign_in(client, github, OTHER, OTHER_ID)
    for project, login, grant in (
        ("demo", OWNER, WRITER),
        ("docs", OWNER, WRITER),
        ("notes", OWNER, READER),
        ("demo", OTHER, WRITER),
        ("demo", ADMIN, WRITER),
    ):
        response = client.put(f"/v1/admin/projects/{project}/grants/{login}", json=grant, headers=admin)
        assert response.status_code == 200, response.text
    return {"owner": bearer(owner["token"]), "other": bearer(other["token"]), "admin": admin}


def env_secret(value: str, env_var: str = "CLAUDE_CODE_OAUTH_TOKEN", projects=("demo",), **extra) -> dict:
    return {"kind": "env", "env_var": env_var, "projects": list(projects), "value": value, **extra}


def git_secret(value: str, url_prefix: str = "https://gitlab.example.org/ops", projects=("demo",), **extra) -> dict:
    return {"kind": "git", "url_prefix": url_prefix, "projects": list(projects), "value": value, **extra}


def put(client, headers, name: str, body: dict):
    return client.put(f"/v1/secrets/{name}", json=body, headers=headers)


def written(client, headers, name: str, body: dict) -> dict:
    response = put(client, headers, name, body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == SECRET_FIELDS | {"created"}
    return response.json()


def listed(client, headers) -> list[dict]:
    response = client.get("/v1/secrets", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    for secret in response.json():
        assert set(secret) == SECRET_FIELDS
    return response.json()


def register_worker(client, headers, name: str, projects=("demo",)) -> dict:
    body = {"name": name, "projects": list(projects), **HOST}
    response = client.post("/v1/workers", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def user_id(db, login: str) -> int:
    users = tables.users
    return sql(db, select(users.c.id).where(users.c.login == login))[0][0]


def stored(db, owner: str, name: str) -> list[tuple]:
    """(id, kind, env_var, url_prefix, username, sealed, nonce, key_id, deleted_at) of every row of the secret."""
    rows, users = tables.secrets, tables.users
    return sql(
        db,
        select(
            rows.c.id,
            rows.c.kind,
            rows.c.env_var,
            rows.c.url_prefix,
            rows.c.username,
            rows.c.sealed,
            rows.c.nonce,
            rows.c.key_id,
            rows.c.deleted_at,
        )
        .join_from(rows, users, users.c.id == rows.c.owner_id)
        .where(users.c.login == owner, rows.c.name == name)
        .order_by(rows.c.id),
    )


def opened(config, db, owner: str, name: str) -> str:
    """The value the live secret ``name`` of ``owner`` holds, opened as the hub opens it."""
    (row,) = [row for row in stored(db, owner, name) if row[8] is None]
    _, kind, *_, sealed, nonce, key_id, _ = row
    return Sealer(config.secrets_key).open(Sealed(sealed, nonce, key_id), secret_aad(user_id(db, owner), name, kind))


def bindings(db, name: str) -> list[tuple]:
    b, owned, projects, workers = tables.secret_bindings, tables.secrets, tables.projects, tables.workers
    return sql(
        db,
        select(projects.c.name, workers.c.name)
        .join_from(b, owned, owned.c.id == b.c.secret_id)
        .join(projects, projects.c.id == b.c.project_id)
        .outerjoin(workers, workers.c.id == b.c.worker_id)
        .where(owned.c.name == name, owned.c.deleted_at.is_(None))
        .order_by(projects.c.name, workers.c.name),
    )


def audit_rows(db, family: str = "secret.%") -> list[tuple]:
    audit, users = tables.audit, tables.users
    return sql(
        db,
        select(audit.c.action, audit.c.target, users.c.login, audit.c.project_id)
        .join_from(audit, users, users.c.id == audit.c.actor_id)
        .where(audit.c.action.like(family))
        .order_by(audit.c.id),
    )


def count(db, table, *where) -> list[tuple]:
    return sql(db, select(func.count()).select_from(table).where(*where))


# Writing and reading


def test_a_secret_is_sealed_under_its_owner_name_and_kind_and_get_never_carries_its_value(client, hub, hub_db, config):
    value = sample()
    answer = written(client, hub["owner"], "claude-oauth", env_secret(value, projects=["demo", "docs", "demo"]))
    assert answer["created"] is True
    assert (answer["name"], answer["kind"], answer["env_var"], answer["url_prefix"], answer["username"]) == (
        "claude-oauth",
        "env",
        "CLAUDE_CODE_OAUTH_TOKEN",
        None,
        None,
    )
    assert (answer["projects"], answer["workers"], answer["expires_at"]) == (["demo", "docs"], [], None)

    response = client.get("/v1/secrets", headers=hub["owner"])
    assert value not in response.text and "sealed" not in response.text and "nonce" not in response.text
    (secret,) = listed(client, hub["owner"])
    assert secret == {key: answer[key] for key in SECRET_FIELDS}

    # the row holds the value sealed: it opens to what was written, and only as this owner's secret of this name
    (row,) = stored(hub_db, OWNER, "claude-oauth")
    assert value.encode() not in bytes(row[5])
    assert len(row[6]) == 12 and row[7] == Sealer(config.secrets_key).key_id
    assert opened(config, hub_db, OWNER, "claude-oauth") == value
    assert bindings(hub_db, "claude-oauth") == [("demo", None), ("docs", None)]
    assert value not in table_dump(hub_db)


def test_a_git_secret_keeps_its_url_prefix_normalized_and_oauth2_as_the_default_username(client, hub, hub_db, config):
    value = sample("glpat-")
    answer = written(
        client, hub["owner"], "gitlab-ops", git_secret(value, url_prefix=" https://GitLab.Example.org/ops/infra.git/ ")
    )
    assert (answer["kind"], answer["env_var"]) == ("git", None)
    assert (answer["url_prefix"], answer["username"]) == ("https://gitlab.example.org/ops/infra", DEFAULT_GIT_USERNAME)
    assert DEFAULT_GIT_USERNAME == "oauth2"
    assert opened(config, hub_db, OWNER, "gitlab-ops") == value

    named = written(
        client,
        hub["owner"],
        "gitlab-host",
        git_secret(sample("glpat-"), url_prefix="https://gitlab.example.org:8443", username="deploy-bot"),
    )
    assert (named["url_prefix"], named["username"]) == ("https://gitlab.example.org:8443", "deploy-bot")

    password = sample("pw-")
    for url_prefix, says in (
        ("http://gitlab.example.org/ops", "https URL"),
        ("git@gitlab.example.org:ops/infra.git", "https URL"),
        ("ssh://git@gitlab.example.org/ops", "https URL"),
        ("gitlab.example.org/ops", "https URL"),
        (f"https://oauth2:{password}@gitlab.example.org/ops", "user or a password"),
        ("https://gitlab.example.org/ops?private_token=x", "without a query"),
        ("https://gitlab.example.org:99999/ops", "https URL"),
        ("https://gitlab example.org/ops", "https://host"),
    ):
        refused = put(client, hub["owner"], "gitlab-bad", git_secret(sample(), url_prefix=url_prefix))
        assert refused.status_code == 422, (url_prefix, refused.text)
        assert says in refused.json()["message"], (url_prefix, refused.text)
        assert password not in refused.text
    assert stored(hub_db, OWNER, "gitlab-bad") == []

    # a git value is one line, as git's credential protocol reads it; no value holds a NUL
    assert put(client, hub["owner"], "gitlab-bad", git_secret("line one\nline two")).status_code == 422
    assert put(client, hub["owner"], "nul", env_secret("a\x00b")).status_code == 422


def test_a_put_replaces_the_secret_whole(client, hub, hub_db, config):
    first = written(client, hub["owner"], "token", env_secret(sample(), projects=["demo", "docs"]))
    later = (datetime.now(UTC) + timedelta(days=90)).replace(microsecond=0)
    value = sample("glpat-")
    second = written(
        client,
        hub["owner"],
        "token",
        git_secret(value, projects=["docs"], expires_at=later.isoformat(), username="bot"),
    )
    assert second["created"] is False
    assert (second["kind"], second["env_var"], second["url_prefix"], second["username"]) == (
        "git",
        None,
        "https://gitlab.example.org/ops",
        "bot",
    )
    assert second["projects"] == ["docs"] and datetime.fromisoformat(second["expires_at"]) == later
    assert second["created_at"] == first["created_at"]
    assert datetime.fromisoformat(second["updated_at"]) >= datetime.fromisoformat(first["updated_at"])
    assert len(stored(hub_db, OWNER, "token")) == 1
    assert opened(config, hub_db, OWNER, "token") == value
    assert bindings(hub_db, "token") == [("docs", None)]
    token_id = stored(hub_db, OWNER, "token")[0][0]
    assert [(action, target) for action, target, _, _ in audit_rows(hub_db)] == [
        ("secret.put", f"secret:{token_id}"),
        ("secret.put", f"secret:{token_id}"),
    ]


def test_a_secret_needs_a_value_of_1_to_max_bytes_a_future_end_and_the_fields_of_its_kind(client, hub, hub_db):
    big = "é" * (MAX_SECRET_BYTES // 2 + 1)  # fewer characters than the limit, more bytes
    refused = put(client, hub["owner"], "big", env_secret(big))
    assert refused.status_code == 422 and f"{MAX_SECRET_BYTES} bytes" in refused.json()["message"]
    assert big not in refused.text
    too_long = sample() + "x" * MAX_SECRET_BYTES
    refused = put(client, hub["owner"], "big", env_secret(too_long))
    assert refused.status_code == 422 and too_long not in refused.text
    assert put(client, hub["owner"], "empty", env_secret("")).status_code == 422
    assert written(client, hub["owner"], "exact", env_secret("x" * MAX_SECRET_BYTES))["created"] is True

    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    refused = put(client, hub["owner"], "old", env_secret(sample(), expires_at=past))
    assert refused.status_code == 422 and "past" in refused.json()["message"]
    naive = (datetime.now() + timedelta(days=1)).replace(tzinfo=None).isoformat()  # no time zone: refused
    assert put(client, hub["owner"], "old", env_secret(sample(), expires_at=naive)).status_code == 422

    for body, says in (
        ({"kind": "env", "projects": ["demo"], "value": sample()}, "needs env_var"),
        (env_secret(sample(), url_prefix="https://gitlab.example.org/ops"), "kind git, not env"),
        (env_secret(sample(), username="bot"), "kind git, not env"),
        ({"kind": "git", "projects": ["demo"], "value": sample()}, "needs url_prefix"),
        (git_secret(sample(), env_var="TOKEN"), "kind env, not git"),
    ):
        refused = put(client, hub["owner"], "mixed", body)
        assert refused.status_code == 422 and says in refused.json()["message"], refused.text
    assert put(client, hub["owner"], "no-project", env_secret(sample(), projects=[])).status_code == 422
    assert put(client, hub["owner"], "Upper", env_secret(sample())).status_code == 422  # names are lower case
    assert put(client, hub["owner"], "kind", {**env_secret(sample()), "kind": "file"}).status_code == 422
    assert {row[0] for row in sql(hub_db, select(tables.secrets.c.name))} == {"exact"}


@pytest.mark.parametrize(
    "env_var",
    ["PATH", "HOME", "SHELL", "USER", "TMPDIR", "SSH_AUTH_SOCK", "EVO_HUB_URL", "GIT_ASKPASS", "LD_PRELOAD",
     "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "path", "1TOKEN", "MY-TOKEN"],
)  # fmt: skip
def test_an_env_var_that_steers_the_shell_git_or_the_worker_is_422(client, hub, hub_db, env_var):
    refused = put(client, hub["owner"], "steer", env_secret(sample(), env_var=env_var))
    assert refused.status_code == 422, refused.text
    assert env_var in refused.json()["message"]
    assert count(hub_db, tables.secrets) == [(0,)]


# Projects and workers


def test_a_project_without_the_writer_role_is_403_and_one_out_of_sight_404(client, hub, hub_db):
    refused = put(client, hub["owner"], "token", env_secret(sample(), projects=["demo", "notes"]))
    assert refused.status_code == 403
    assert "writer role" in refused.json()["message"] and "notes" in refused.json()["message"]
    assert put(client, hub["other"], "token", env_secret(sample(), projects=["docs"])).status_code == 404
    assert put(client, hub["owner"], "token", env_secret(sample(), projects=["nowhere"])).status_code == 404
    # a hub admin manages every project but holds no role on docs: no secret for it
    refused = put(client, hub["admin"], "token", env_secret(sample(), projects=["docs"]))
    assert refused.status_code == 403 and "you hold no role" in refused.json()["message"]
    assert count(hub_db, tables.secrets) == [(0,)]
    assert count(hub_db, tables.audit, tables.audit.c.action.like("secret.%")) == [(0,)]


def test_workers_are_the_callers_own_that_are_not_revoked_and_anyone_elses_is_403(client, hub, hub_db):
    box = register_worker(client, hub["owner"], "box")
    desk = register_worker(client, hub["owner"], "desk")
    other_box = register_worker(client, hub["other"], "other-box")

    answer = written(
        client, hub["owner"], "token", env_secret(sample(), projects=["demo", "docs"], workers=["BOX", "desk", "box"])
    )
    assert answer["workers"] == ["box", "desk"]
    assert bindings(hub_db, "token") == [("demo", "box"), ("demo", "desk"), ("docs", "box"), ("docs", "desk")]

    for headers, workers in (
        (hub["owner"], ["box", "other-box"]),  # another member's worker
        (hub["owner"], ["nowhere"]),  # no worker by that name
        (hub["admin"], ["box"]),  # a hub admin binds only to workers of their own
    ):
        refused = put(client, headers, "token", env_secret(sample(), workers=workers))
        assert refused.status_code == 403, refused.text
        assert "not a worker of yours" in refused.json()["message"]
    assert bindings(hub_db, "token") == [("demo", "box"), ("demo", "desk"), ("docs", "box"), ("docs", "desk")]

    assert client.post(f"/v1/workers/{desk['worker']['id']}/revoke", headers=hub["owner"]).status_code == 200
    refused = put(client, hub["owner"], "token", env_secret(sample(), workers=["desk"]))
    assert refused.status_code == 403 and "not revoked" in refused.json()["message"]
    assert written(client, hub["owner"], "token", env_secret(sample()))["workers"] == []  # any of the owner's
    assert bindings(hub_db, "token") == [("demo", None)]

    # a worker token is no credential for an owner's routes
    worker = bearer(other_box["token"])
    assert client.get("/v1/secrets", headers=worker).status_code == 403
    assert put(client, worker, "token", env_secret(sample())).status_code == 403
    assert box["worker"]["owner"] == OWNER


# Who sees what


def test_a_hub_admin_sees_only_their_own_secrets_and_the_stats_only_count(client, hub, hub_db):
    owners_value, admins_value = sample("owner-"), sample("admin-")
    written(client, hub["owner"], "claude-oauth", env_secret(owners_value))
    written(client, hub["owner"], "gitlab-ops", git_secret(sample()))
    written(client, hub["admin"], "admin-token", env_secret(admins_value, env_var="OPENAI_API_KEY"))

    assert [secret["name"] for secret in listed(client, hub["admin"])] == ["admin-token"]
    assert [secret["name"] for secret in listed(client, hub["owner"])] == ["claude-oauth", "gitlab-ops"]
    assert listed(client, hub["other"]) == []
    # another owner's secret of the same name is another secret; deleting someone else's is 404
    assert client.delete("/v1/secrets/claude-oauth", headers=hub["admin"]).status_code == 404
    assert client.delete("/v1/secrets/claude-oauth", headers=hub["other"]).status_code == 404
    assert written(client, hub["other"], "claude-oauth", env_secret(sample()))["created"] is True
    assert [secret["name"] for secret in listed(client, hub["owner"])] == ["claude-oauth", "gitlab-ops"]

    stats = client.get("/v1/admin/stats", headers=hub["admin"])
    assert stats.status_code == 200
    assert (stats.json()["secrets"], stats.json()["secret_bindings"]) == (4, 4)
    assert owners_value not in stats.text and admins_value not in stats.text


# Deleting


def test_a_delete_drops_the_sealed_value_and_bindings_revokes_the_leases_and_frees_the_name(
    client, hub, hub_db, config
):
    box = register_worker(client, hub["owner"], "box")
    value = sample()
    written(client, hub["owner"], "claude-oauth", env_secret(value, workers=["box"]))
    (row,) = stored(hub_db, OWNER, "claude-oauth")
    secret_id, owner_id, worker_id = row[0], user_id(hub_db, OWNER), box["worker"]["id"]
    projects, leases = tables.projects, tables.credential_leases
    with live.connect(hub_db) as conn:
        ids = {"project": sql(hub_db, select(projects.c.id).where(projects.c.name == "demo"))[0][0], "user": owner_id}
        ids["worker"] = worker_id
        add_plan(conn, ids, PLAN)
        run_id = add_run(conn, ids, state="running")
    lease = {
        "run_id": run_id,
        "worker_id": worker_id,
        "secret_id": secret_id,
        "provider": "secret",
        "target": "CLAUDE_CODE_OAUTH_TOKEN",
    }
    given_back = datetime.now(UTC) - timedelta(minutes=5)
    ((live_lease,),) = sql(hub_db, insert(leases).values(**lease, revoked_at=None).returning(leases.c.id))
    ((ended_lease,),) = sql(
        hub_db,
        insert(leases)
        .values(**lease, issued_at=given_back - timedelta(minutes=5), revoked_at=given_back)
        .returning(leases.c.id),
    )

    deleted = client.delete("/v1/secrets/claude-oauth", headers=hub["owner"])
    assert deleted.status_code == 204 and deleted.content == b""
    assert listed(client, hub["owner"]) == []
    ((_, kind, env_var, _, _, sealed, nonce, key_id, deleted_at),) = stored(hub_db, OWNER, "claude-oauth")
    assert (kind, env_var, sealed, nonce, key_id) == ("env", "CLAUDE_CODE_OAUTH_TOKEN", None, None, None)
    assert deleted_at is not None
    assert count(hub_db, tables.secret_bindings) == [(0,)]
    revoked = dict(sql(hub_db, select(leases.c.id, leases.c.revoked_at)))
    assert revoked[live_lease] is not None and revoked[ended_lease] == given_back
    assert client.delete("/v1/secrets/claude-oauth", headers=hub["owner"]).status_code == 404

    # the name is free again: a new secret, a new row
    again = written(client, hub["owner"], "claude-oauth", env_secret(sample()))
    assert again["created"] is True and again["workers"] == []
    assert len(stored(hub_db, OWNER, "claude-oauth")) == 2
    first, second = sorted(row[0] for row in stored(hub_db, OWNER, "claude-oauth"))
    assert [(action, target, login) for action, target, login, _ in audit_rows(hub_db)] == [
        ("secret.put", f"secret:{first}", OWNER),
        ("secret.delete", f"secret:{first}", OWNER),
        ("secret.put", f"secret:{second}", OWNER),
    ]
    assert value not in table_dump(hub_db)


def test_a_member_keeps_a_bounded_number_of_secrets(client, hub, hub_db, monkeypatch):
    monkeypatch.setattr(secret_routes, "MAX_SECRETS_PER_OWNER", 2)
    written(client, hub["owner"], "one", env_secret(sample()))
    written(client, hub["owner"], "two", env_secret(sample(), env_var="OTHER_TOKEN"))
    refused = put(client, hub["owner"], "three", env_secret(sample()))
    assert refused.status_code == 409 and "2 secrets" in refused.json()["message"]
    assert written(client, hub["owner"], "two", env_secret(sample()))["created"] is False  # a replacement counts once
    assert written(client, hub["other"], "three", env_secret(sample()))["created"] is True  # per member
    assert client.delete("/v1/secrets/one", headers=hub["owner"]).status_code == 204  # a deleted one does not count
    assert written(client, hub["owner"], "three", env_secret(sample()))["created"] is True


# The key


def test_without_the_secrets_key_a_write_is_503_and_keeps_nothing(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github)  # no EVO_HUB_SECRETS_KEY
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        add_project(hub_db, "demo")
        admin = bearer(live.sign_in(client, github, ADMIN, ADMIN_ID)["token"])
        owner = bearer(live.sign_in(client, github, OWNER, OWNER_ID)["token"])
        assert client.put(f"/v1/admin/projects/demo/grants/{OWNER}", json=WRITER, headers=admin).status_code == 200
        value = sample()
        refused = put(client, owner, "claude-oauth", env_secret(value))
        assert refused.status_code == 503 and refused.json()["error"] == "unavailable"
        assert "EVO_HUB_SECRETS_KEY" in refused.json()["message"] and value not in refused.text
        assert listed(client, owner) == []  # reading needs no key
        assert client.delete("/v1/secrets/claude-oauth", headers=owner).status_code == 404
        assert count(hub_db, tables.secrets) == [(0,)]
        assert count(hub_db, tables.audit, tables.audit.c.action.like("secret.%")) == [(0,)]


# The web


def test_a_web_session_writes_only_with_its_csrf_header(client, hub, github, hub_db):
    session = web_sign_in(client, github, Account(OWNER, OWNER_ID))
    value = sample()
    refused = client.put("/v1/secrets/claude-oauth", json=env_secret(value), headers=cookie(session))
    assert refused.status_code == 403 and "X-Evo-CSRF" in refused.json()["message"]
    assert count(hub_db, tables.secrets) == [(0,)]
    csrf = {"X-Evo-CSRF": csrf_for(client, session)}
    written(client, {**cookie(session), **csrf}, "claude-oauth", env_secret(value))
    assert [secret["name"] for secret in listed(client, cookie(session))] == ["claude-oauth"]
    refused = client.delete("/v1/secrets/claude-oauth", headers=cookie(session))
    assert refused.status_code == 403 and "X-Evo-CSRF" in refused.json()["message"]
    assert client.delete("/v1/secrets/claude-oauth", headers={**cookie(session), **csrf}).status_code == 204


# The audit and the logs


def test_neither_the_audit_nor_a_log_record_holds_a_value(client, hub, hub_db, caplog):
    caplog.set_level(logging.DEBUG)
    values = [sample("oauth-"), sample("oauth-"), sample("glpat-")]
    names = ("claude-oauth", "gitlab-ops")
    written(client, hub["owner"], "claude-oauth", env_secret(values[0]))
    written(client, hub["owner"], "claude-oauth", env_secret(values[1], projects=["docs"]))
    written(client, hub["owner"], "gitlab-ops", git_secret(values[2]))
    assert put(client, hub["owner"], "steer", env_secret(sample(), env_var="PATH")).status_code == 422
    assert client.delete("/v1/secrets/gitlab-ops", headers=hub["owner"]).status_code == 204

    rows = audit_rows(hub_db)
    ids = (sql(hub_db, select(tables.secrets.c.id).where(tables.secrets.c.name == name)) for name in names)
    (oauth,), (ops,) = ids
    assert [(action, target, login, project) for action, target, login, project in rows] == [
        ("secret.put", f"secret:{oauth[0]}", OWNER, None),
        ("secret.put", f"secret:{oauth[0]}", OWNER, None),
        ("secret.put", f"secret:{ops[0]}", OWNER, None),
        ("secret.delete", f"secret:{ops[0]}", OWNER, None),
    ]
    trail = client.get("/v1/admin/audit", params={"action": "secret.put"}, headers=hub["admin"])
    assert trail.status_code == 200, trail.text
    # the audit is a hub admin's to read, and a secret's name is its owner's alone: the audit names it by id
    deletes = client.get("/v1/admin/audit", params={"action": "secret.delete"}, headers=hub["admin"]).text
    for name in names:
        assert name not in trail.text and name not in deletes

    assert {"secret written", "secret deleted"} <= {record.getMessage() for record in caplog.records}
    raw = caplog.text + "\n".join(
        f"{record.getMessage()} {record.args!r} {vars(record)!r}" for record in caplog.records
    )
    dump = table_dump(hub_db) + json.dumps(rows) + trail.text
    for value in values:
        assert value not in raw, "a secret's value reached a log record"
        assert value not in dump, "a secret's value reached a table or the audit"
