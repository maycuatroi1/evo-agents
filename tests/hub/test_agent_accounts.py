"""The accounts of an agent: env secrets an agent profile owns (``evo_agents.hub.server.agent_accounts``), criterion a1 of
the plan agent-accounts. An admin of the project gives an agent with a name an account, renews it (the same secret id),
changes its weight, priority, workers and end, pauses, resumes and deletes it, over PUT, PATCH and DELETE
/v1/projects/{p}/agents/{handle}/accounts/{name} and ``hub agent account``; a writer or a reader gets 403; the default
profile and a deleted one are 422; every account has at least one worker, the admin's own that is not revoked or a shared
one, that serves the project; DENIED_ENV is refused; the value is sealed for its profile and opens nowhere else; deleting
an account or its profile drops the value and bindings and revokes the leases out; each write adds an audit row naming
@handle and the secret's id; no route, log line, audit row or table holds a value."""

import io
import logging
import secrets

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from evo_agents.hub import tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.sealing import KEY_BYTES, Sealed, Sealer, Unsealable, agent_secret_aad, secret_aad
from tests.hub import test_secrets_cli as secrets_cli
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql, table_dump
from tests.hub.test_run_cli import as_json, ok
from tests.hub.test_run_tables import add_run
from tests.hub.test_runs import OTHER, OWNER, PLAN, PROJECT, add_worker, members
from tests.hub.test_secrets_cli import run_cli
from tests.hub.test_shared_workers import share

ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
KEY = secrets.token_bytes(KEY_BYTES)
ENV = "CLAUDE_CODE_OAUTH_TOKEN"

if pg.DSN:
    app_key, cli_hub = secrets_cli.app_key, secrets_cli.hub


def sample(prefix: str = "sk-agent-") -> str:
    """A value drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


@pytest.fixture
def client(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, secrets_key=KEY)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The members of ``tests.hub.test_runs``, the owner made an admin of the project, and the agent @auditor."""
    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    made = client.post(
        f"/v1/projects/{PROJECT}/agents", json={"handle": "auditor", "name": "Auditor"}, headers=headers["owner"]
    )
    assert made.status_code == 201, made.text
    return headers


def path(name: str | None = None, handle: str = "auditor") -> str:
    base = f"/v1/projects/{PROJECT}/agents/{handle}/accounts"
    return base if name is None else f"{base}/{name}"


def body(value: str, workers=("box",), env_var: str = ENV, **extra) -> dict:
    return {"env_var": env_var, "value": value, "workers": list(workers), **extra}


def put(client, headers, name: str, payload: dict, handle: str = "auditor"):
    return client.put(path(name, handle), json=payload, headers=headers)


def added(client, headers, name: str, payload: dict, handle: str = "auditor") -> dict:
    response = put(client, headers, name, payload, handle)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def rows(db, name: str | None = None) -> list:
    """(id, owner_id, profile_id, name, sealed, nonce, key_id, deleted_at) of the secrets a profile owns."""
    s = tables.secrets
    query = select(s.c.id, s.c.owner_id, s.c.profile_id, s.c.name, s.c.sealed, s.c.nonce, s.c.key_id, s.c.deleted_at)
    query = query.where(s.c.profile_id.is_not(None)).order_by(s.c.id)
    return sql(db, query if name is None else query.where(s.c.name == name))


def profile_id(db, handle: str = "auditor") -> int:
    ap = tables.agent_profiles
    return sql(db, select(ap.c.id).where(ap.c.handle == handle))[0][0]


def bindings(db, secret_id: int) -> list:
    b = tables.secret_bindings
    return sql(db, select(b.c.project_id, b.c.worker_id).where(b.c.secret_id == secret_id).order_by(b.c.worker_id))


def audit_rows(db) -> list[tuple]:
    a, u, p = tables.audit, tables.users, tables.projects
    query = (
        select(a.c.action, a.c.target, u.c.login, p.c.name)
        .join_from(a, u, u.c.id == a.c.actor_id)
        .outerjoin(p, p.c.id == a.c.project_id)
        .where(a.c.action.like("agent-account.%"))
        .order_by(a.c.id)
    )
    return sql(db, query)


def test_agent_account_is_added_renewed_changed_paused_resumed_and_deleted_by_a_project_admin(
    client, hub, hub_db, caplog
):
    caplog.set_level(logging.DEBUG)
    box = add_worker(client, hub["owner"], "box")
    value, renewed = sample(), sample()
    account = added(client, hub["owner"], "claude-a", body(value, expires_at="2099-01-01T00:00:00Z"))
    assert account["created"] is True and account["workers"] == ["box"] and account["env_var"] == ENV
    assert (account["weight"], account["priority"], account["state"], account["paused_at"]) == (1, 0, "ready", None)
    assert "value" not in account and value not in str(account)
    (row,) = rows(hub_db)
    secret_id, owner_id, pid, name, sealed, nonce, key_id, deleted_at = row
    assert (secret_id, owner_id, pid, name, deleted_at) == (account["id"], None, profile_id(hub_db), "claude-a", None)
    assert bindings(hub_db, secret_id) == [(sql(hub_db, select(tables.projects.c.id))[0][0], box["id"])]
    assert Sealer(KEY).open(Sealed(sealed, nonce, key_id), agent_secret_aad(pid, "claude-a")) == value

    # renew: a new value for the same account, with its weight, priority and workers as they were
    set_ = client.patch(path("claude-a"), json={"weight": 5, "priority": 2}, headers=hub["owner"])
    assert set_.status_code == 200 and (set_.json()["weight"], set_.json()["priority"]) == (5, 2), set_.text
    again = client.patch(path("claude-a"), json={"value": renewed}, headers=hub["owner"])
    assert again.status_code == 200, again.text
    assert (again.json()["id"], again.json()["weight"], again.json()["priority"]) == (secret_id, 5, 2)
    (row,) = rows(hub_db)
    assert Sealer(KEY).open(Sealed(row[4], row[5], row[6]), agent_secret_aad(pid, "claude-a")) == renewed
    assert row[4] != sealed

    # a PUT of the name again replaces it whole and keeps the id
    third = sample()
    replaced = added(client, hub["owner"], "claude-a", body(third))
    assert (replaced["created"], replaced["id"], replaced["weight"], replaced["expires_at"]) == (
        False,
        secret_id,
        5,
        None,
    )

    # workers, end, pause, resume
    other_box = add_worker(client, hub["owner"], "box-2")
    moved = client.patch(
        path("claude-a"),
        json={"workers": ["box-2", "box"], "expires_at": "2098-01-01T00:00:00Z", "paused": True},
        headers=hub["owner"],
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["workers"] == ["box", "box-2"] and moved.json()["state"] == "paused"
    assert moved.json()["paused_at"] is not None and moved.json()["expires_at"].startswith("2098-01-01")
    assert [w for _, w in bindings(hub_db, secret_id)] == sorted([box["id"], other_box["id"]])
    resumed = client.patch(path("claude-a"), json={"paused": False, "expires_at": None}, headers=hub["owner"])
    assert resumed.json()["state"] == "ready" and resumed.json()["expires_at"] is None

    gone = client.delete(path("claude-a"), headers=hub["owner"])
    assert gone.status_code == 204 and gone.content == b""
    (row,) = rows(hub_db)
    assert row[4:7] == (None, None, None) and row[7] is not None and bindings(hub_db, secret_id) == []
    assert client.delete(path("claude-a"), headers=hub["owner"]).status_code == 404
    assert client.patch(path("claude-a"), json={"paused": True}, headers=hub["owner"]).status_code == 404

    # one audit row for each write, naming the agent and the secret's id, never the account's name or a value
    audit = audit_rows(hub_db)
    assert [(action, login, project) for action, _, login, project in audit] == [
        ("agent-account.put", OWNER, PROJECT),
        ("agent-account.set", OWNER, PROJECT),
        ("agent-account.set", OWNER, PROJECT),
        ("agent-account.put", OWNER, PROJECT),
        ("agent-account.set", OWNER, PROJECT),
        ("agent-account.set", OWNER, PROJECT),
        ("agent-account.delete", OWNER, PROJECT),
    ]
    assert audit[0][1] == f"{PROJECT}/@auditor secret:{secret_id}"
    assert audit[2][1] == f"{PROJECT}/@auditor secret:{secret_id} fields=value"
    assert all("claude-a" not in target for _, target, _, _ in audit)
    for secret in (value, renewed, third):
        assert secret not in table_dump(hub_db)
        assert secret not in caplog.text and secret not in str(audit)


def test_agent_account_is_refused_to_a_writer_and_a_reader_and_for_the_default_and_a_deleted_agent(client, hub, hub_db):
    add_worker(client, hub["owner"], "box")
    for who in ("other", "reader"):
        for response in (
            put(client, hub[who], "claude-a", body(sample())),
            client.patch(path("claude-a"), json={"paused": True}, headers=hub[who]),
            client.delete(path("claude-a"), headers=hub[who]),
        ):
            assert response.status_code == 403 and "needs the admin role" in response.text, who
    assert put(client, hub["stranger"], "claude-a", body(sample())).status_code == 404
    assert put(client, hub["admin"], "claude-a", body(sample())).status_code == 403  # a hub admin with no grant
    assert put(client, hub["owner"], "claude-a", body(sample()), handle="nobody").status_code == 404

    refused = put(client, hub["owner"], "claude-a", body(sample()), handle=PROJECT)
    assert refused.status_code == 422 and "default agent" in refused.text
    assert client.delete(f"/v1/projects/{PROJECT}/agents/auditor", headers=hub["owner"]).status_code == 200
    refused = put(client, hub["owner"], "claude-a", body(sample()))
    assert refused.status_code == 422 and "deleted" in refused.text
    assert client.patch(path("claude-a"), json={"paused": True}, headers=hub["owner"]).status_code == 422
    assert rows(hub_db) == [] and audit_rows(hub_db) == []


@pytest.mark.parametrize(
    "env_var", ["PATH", "HOME", "EVO_HUB_TOKEN", "GIT_ASKPASS", "lower_case"], ids=lambda var: var.lower()
)
def test_agent_account_refuses_a_variable_that_steers_the_shell_git_or_the_worker(client, hub, hub_db, env_var):
    add_worker(client, hub["owner"], "box")
    value = sample()
    refused = put(client, hub["owner"], "claude-a", body(value, env_var=env_var))
    assert refused.status_code == 422 and value not in refused.text
    assert rows(hub_db) == []


def test_agent_account_needs_a_worker_of_the_admin_or_a_shared_one_that_serves_the_project(client, hub, hub_db):
    mine = add_worker(client, hub["owner"], "mine")
    theirs = add_worker(client, hub["other"], "theirs")
    away = add_worker(client, hub["owner"], "away")
    gone = add_worker(client, hub["owner"], "gone")
    with live.connect(hub_db) as conn:
        conn.execute(delete(tables.worker_projects).where(tables.worker_projects.c.worker_id == away["id"]))
        conn.execute(update(tables.workers).values(revoked_at=func.now()).where(tables.workers.c.id == gone["id"]))
    value = sample()
    assert put(client, hub["owner"], "claude-a", {**body(value), "workers": []}).status_code == 422
    assert put(client, hub["owner"], "claude-a", {"env_var": ENV, "value": value}).status_code == 422
    for name in ("theirs", "away", "gone", "nowhere"):
        refused = put(client, hub["owner"], "claude-a", body(value, workers=["mine", name]))
        assert refused.status_code == 422 and name in refused.text and value not in refused.text, name
    assert rows(hub_db) == []  # no half-written account

    assert share(client, hub["other"], theirs).status_code == 200
    account = added(client, hub["owner"], "claude-a", body(value, workers=["mine", "theirs"]))
    assert account["workers"] == ["mine", "theirs"]
    (row,) = rows(hub_db)
    assert [w for _, w in bindings(hub_db, row[0])] == sorted([mine["id"], theirs["id"]])


def test_agent_account_seals_for_its_profile_so_a_copy_into_a_members_secret_does_not_open(client, hub, hub_db):
    add_worker(client, hub["owner"], "box")
    value = sample()
    added(client, hub["owner"], "claude-a", body(value))
    (row,) = rows(hub_db)
    sealed, sealer = Sealed(row[4], row[5], row[6]), Sealer(KEY)
    member = secret_aad(1, "claude-a", "env")
    for aad in (member, agent_secret_aad(profile_id(hub_db) + 1, "claude-a"), agent_secret_aad(row[2], "claude-b")):
        with pytest.raises(Unsealable):
            sealer.open(sealed, aad)
    assert agent_secret_aad(1, "claude-a") != member and not agent_secret_aad(1, "claude-a").startswith(b"secret:")
    assert sealer.open(sealed, agent_secret_aad(row[2], "claude-a")) == value


def test_agent_account_needs_the_hubs_sealing_key(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github)
    with TestClient(create_app(config), base_url="https://hub.test") as keyless:
        headers = members(keyless, github)
        granted = keyless.put(
            f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"]
        )
        assert granted.status_code == 200
        made = keyless.post(
            f"/v1/projects/{PROJECT}/agents", json={"handle": "auditor", "name": "Auditor"}, headers=headers["owner"]
        )
        assert made.status_code == 201
        add_worker(keyless, headers["owner"], "box")
        value = sample()
        refused = put(keyless, headers["owner"], "claude-a", body(value))
        assert refused.status_code == 503 and value not in refused.text
    assert rows(hub_db) == []


def test_agent_account_is_unique_by_profile_and_name_and_a_member_secret_of_that_name_is_another_row(
    client, hub, hub_db
):
    add_worker(client, hub["owner"], "box")
    client.post(f"/v1/projects/{PROJECT}/agents", json={"handle": "second", "name": "Second"}, headers=hub["owner"])
    first = added(client, hub["owner"], "claude-a", body(sample()))
    second = added(client, hub["owner"], "claude-a", body(sample()), handle="second")
    assert first["id"] != second["id"]
    member = client.put(
        "/v1/secrets/claude-a",
        json={"kind": "env", "env_var": ENV, "projects": [PROJECT], "value": sample()},
        headers=hub["owner"],
    )
    assert member.status_code == 200, member.text
    assert [name for *_, name, _, _, _, _ in rows(hub_db)] == ["claude-a", "claude-a"]
    listed = client.get("/v1/secrets", headers=hub["owner"]).json()
    assert [secret["name"] for secret in listed] == ["claude-a"]  # a member lists their own, never an agent's
    client.delete("/v1/secrets/claude-a", headers=hub["owner"])
    assert len([row for row in rows(hub_db) if row[7] is None]) == 2  # the member's delete left both agents' alone


def test_agent_account_deletes_with_the_profile_dropping_value_bindings_and_revoking_leases(client, hub, hub_db):
    box = add_worker(client, hub["owner"], "box")
    added(client, hub["owner"], "claude-a", body(sample()))
    added(client, hub["owner"], "claude-b", body(sample()))
    (a, b) = rows(hub_db)
    user = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0][0]
    project = sql(hub_db, select(tables.projects.c.id))[0][0]
    ids = {"project": project, "user": user, "worker": box["id"]}
    leases = tables.credential_leases
    with live.connect(hub_db) as conn:
        run_id = add_run(conn, ids, state="running", kind="release")
        for secret_id in (a[0], b[0]):
            conn.execute(
                leases.insert().values(
                    run_id=run_id, worker_id=box["id"], secret_id=secret_id, provider="secret", target=ENV
                )
            )
    assert client.delete(path("claude-a"), headers=hub["owner"]).status_code == 204
    revoked = dict(sql(hub_db, select(leases.c.secret_id, leases.c.revoked_at)))
    assert revoked[a[0]] is not None and revoked[b[0]] is None

    deleted = client.delete(f"/v1/projects/{PROJECT}/agents/auditor", headers=hub["owner"])
    assert deleted.status_code == 200, deleted.text
    rows_now = rows(hub_db)
    assert all(row[4:7] == (None, None, None) and row[7] is not None for row in rows_now)
    assert bindings(hub_db, a[0]) == bindings(hub_db, b[0]) == []
    assert all(at is not None for _, at in sql(hub_db, select(leases.c.secret_id, leases.c.revoked_at)))
    assert PLAN  # the plan of the run is the one test_runs pushed


def test_agent_account_command_adds_renews_changes_pauses_and_removes_one_without_printing_a_value(
    cli_hub, monkeypatch, capsys
):
    grant = cli_hub.client.put(
        f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=cli_hub.headers["admin"]
    )
    assert grant.status_code == 200, grant.text
    made = cli_hub.client.post(
        f"/v1/projects/{PROJECT}/agents",
        json={"handle": "auditor", "name": "Auditor"},
        headers=cli_hub.headers["owner"],
    )
    assert made.status_code == 201, made.text
    add_worker(cli_hub.client, cli_hub.headers["owner"], "mac-mini")
    owner, other = cli_hub.homes["owner"], cli_hub.homes["other"]
    value, renewed = sample(), sample()

    def account(who, *args, stdin=None):
        return run_cli(monkeypatch, capsys, who, "hub", "agent", "account", *args, "--project", PROJECT, stdin=stdin)

    added_ = account(
        owner,
        "add",
        "@auditor",
        "claude-a",
        "--env-var",
        ENV,
        "--worker",
        "mac-mini",
        "--weight",
        "3",
        "--expires",
        "2099-01-31",
        "--json",
        stdin=io.StringIO(value + "\n"),
    )
    shown = as_json(added_)
    assert_json_keys("hub agent account add", shown)
    assert (shown["created"], shown["weight"], shown["workers"]) == (True, 3, ["mac-mini"])
    assert value not in added_.out and value not in added_.err
    text = ok(account(owner, "renew", "auditor", "claude-a", stdin=io.StringIO(renewed + "\n"))).out
    assert text.startswith("Renewed account claude-a (CLAUDE_CODE_OAUTH_TOKEN) of @auditor: ready, weight 3")
    assert renewed not in text
    changed = as_json(account(owner, "set", "auditor", "claude-a", "--priority", "4", "--no-expiry", "--json"))
    assert_json_keys("hub agent account set", changed)
    assert (changed["priority"], changed["expires_at"], changed["id"]) == (4, None, shown["id"])
    paused = as_json(account(owner, "pause", "auditor", "claude-a", "--json"))
    assert paused["state"] == "paused"
    assert as_json(account(owner, "resume", "auditor", "claude-a", "--json"))["state"] == "ready"

    refused = account(other, "pause", "auditor", "claude-a")
    assert refused.code == 1 and "needs the admin role" in refused.err
    nothing = account(owner, "set", "auditor", "claude-a")
    assert nothing.code == 2 and "give something to change" in nothing.err
    bad = account(
        owner, "add", "auditor", "claude-b", "--env-var", "PATH", "--worker", "mac-mini", stdin=io.StringIO("x")
    )
    assert bad.code == 2 and "PATH" in bad.err
    gone = ok(account(owner, "remove", "auditor", "claude-a")).out
    assert gone.startswith("Deleted account claude-a of @auditor")
    assert (
        sql(
            cli_hub.db, select(func.count()).select_from(tables.secrets).where(tables.secrets.c.profile_id.is_not(None))
        )[0][0]
        == 1
    )
    for text in (value, renewed):
        assert text not in table_dump(cli_hub.db)


def test_agent_account_commands_are_in_the_contract_and_take_no_value_on_their_command_line():
    from evo_agents.hub.contract import commands

    listed = commands()
    names = {"hub agent account " + verb for verb in ("add", "renew", "set", "pause", "resume", "remove")}
    assert names <= set(listed)
    for name in names:
        flags = {option["flags"][0] for option in listed[name]["options"]}
        assert "--value" not in flags and "--token" not in flags, name
    assert OTHER  # members of the plan
