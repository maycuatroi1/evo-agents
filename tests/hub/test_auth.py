"""Signing in with GitHub, machine tokens and roles, against a fake GitHub: the device flow's token is traded for
a hub token kept only as its SHA-256, revoked and expired tokens get the same 401 naming ``evo-agents hub login``,
a token writes its expiry at most once a day, only an admin grants, every change leaves an audit row, GitHub being
down is a clear 502, and the CLI signs in, keeps its files private and signs out."""

import asyncio
import hashlib
import json
import os
import stat
import time

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub import client as hub_client
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.security import authenticate
from tests.hub.contract_keys import assert_json_keys
from tests.hub.fake_github import Account
from tests.hub.live import ADMIN, add_project, bearer, sign_in, sql

HUB_TABLES = {
    "users",
    "tokens",
    "projects",
    "project_repos",
    "project_sinks",
    "grants",
    "memories",
    "memory_revisions",
    "plans",
    "plan_revisions",
    "skills",
    "skill_versions",
    "kg_ingests",
    "audit",
}
HUB_TABLES |= pg.BLOB_TABLES | pg.QUEUE_TABLES  # migration 0004
HUB_TABLES |= pg.KG_TABLES  # migration 0006
HUB_TABLES |= pg.RETENTION_TABLES  # migration 0008


@pytest.fixture
def config(hub_db, tmp_path, github):
    return live.hub_config(hub_db, tmp_path, github)


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as client:
        yield client


def audit_rows(db) -> list[tuple]:
    return sql(
        db,
        "SELECT u.login, a.token_id, a.action, a.target FROM audit a LEFT JOIN users u ON u.id = a.actor_id "
        "ORDER BY a.id",
    )


def assert_login_hint(response) -> None:
    assert response.status_code == 401, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"] == "unauthorized" and "evo-agents hub login" in body["message"]
    assert response.headers["www-authenticate"].startswith("Bearer")


# Server: signing in and tokens


def test_signing_in_trades_the_github_token_for_a_hub_token_kept_only_as_its_hash(client, github, hub_db):
    github_token = github.issue_token(Account("octo", 101))
    response = client.post("/v1/auth/github", json={"github_token": github_token, "host": "laptop"})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    signed = response.json()
    token = signed["token"]
    assert token.startswith("evh_") and len(token) == 4 + 43  # 32 random bytes in base64url
    assert (signed["login"], signed["admin"]) == ("octo", False)

    (row,) = sql(hub_db, "SELECT id, kind, host, token_hash, last_used_at FROM tokens")
    assert row == (signed["token_id"], "machine", "laptop", hashlib.sha256(token.encode()).hexdigest(), None)
    stored = live.table_dump(hub_db)
    assert token not in stored and token[4:] not in stored and github_token not in stored
    # The GitHub token went to GitHub in a header, never in a URL.
    (call,) = github.calls("/user")
    assert call.headers["authorization"] == f"Bearer {github_token}" and call.query == {}

    me = client.get("/v1/auth/whoami", headers=bearer(token))
    assert me.status_code == 200, me.text
    assert me.json()["login"] == "octo" and me.json()["token"]["kind"] == "machine"
    assert me.json()["token"]["host"] == "laptop" and me.json()["grants"] == []
    assert audit_rows(hub_db) == [("octo", signed["token_id"], "auth.login", "machine:laptop")]


def test_a_missing_or_unknown_credential_gets_401_naming_hub_login(client):
    for headers in (
        {},
        {"Authorization": "Basic b2N0bzpwdw=="},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer evh_short"},
        {"Authorization": "Bearer evh_" + "A" * 43},  # well formed, but no such token
        {"Authorization": "Bearer evs_" + "A" * 43},  # the shape of a web session
    ):
        assert_login_hint(client.get("/v1/auth/whoami", headers=headers))
    # Fail closed: a path no route serves is refused before routing, and the public ones stay open.
    assert_login_hint(client.get("/v1/no-such-route"))
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/auth/config").json()["github_client_id"] == "Iv1.fake0client0id"


def test_a_revoked_token_gets_401(client, github, hub_db):
    first = sign_in(client, github, "octo", 101, host="laptop")
    second = sign_in(client, github, "octo", 101, host="desktop")
    other = sign_in(client, github, "hubot", 202)

    listed = client.get("/v1/tokens", headers=bearer(first["token"])).json()
    assert [(t["id"], t["kind"], t["host"], t["current"]) for t in listed] == [
        (second["token_id"], "machine", "desktop", False),
        (first["token_id"], "machine", "laptop", True),
    ]
    assert all(t["expires_at"] and t["state"] == "active" for t in listed)

    # Another user's token is unknown to you, and keeps working.
    refused = client.delete(f"/v1/tokens/{other['token_id']}", headers=bearer(first["token"]))
    assert refused.status_code == 404 and refused.json()["error"] == "not_found"
    assert client.get("/v1/auth/whoami", headers=bearer(other["token"])).status_code == 200

    assert client.delete(f"/v1/tokens/{second['token_id']}", headers=bearer(first["token"])).status_code == 204
    assert_login_hint(client.get("/v1/auth/whoami", headers=bearer(second["token"])))
    assert client.delete(f"/v1/tokens/{second['token_id']}", headers=bearer(first["token"])).status_code == 404

    assert client.post("/v1/auth/logout", headers=bearer(first["token"])).status_code == 204
    assert_login_hint(client.get("/v1/tokens", headers=bearer(first["token"])))
    assert_login_hint(client.post("/v1/auth/logout", headers=bearer(first["token"])))

    states = sql(hub_db, "SELECT id, revoked_at IS NOT NULL FROM tokens ORDER BY id")
    assert states == [(first["token_id"], True), (second["token_id"], True), (other["token_id"], False)]
    assert audit_rows(hub_db)[3:] == [
        ("octo", first["token_id"], "token.revoke", f"token:{second['token_id']}"),
        ("octo", first["token_id"], "auth.logout", f"token:{first['token_id']}"),
    ]


def test_a_token_unused_for_90_days_gets_401(client, github, hub_db):
    stale = sign_in(client, github, "octo", 101)
    fresh = sign_in(client, github, "octo", 101, host="desktop")
    # 91 days without use: its expiry, pushed 90 days past its last use, has passed.
    sql(
        hub_db,
        "UPDATE tokens SET created_at = now() - interval '100 days', last_used_at = now() - interval '91 days', "
        "expires_at = now() - interval '1 day' WHERE id = %s",
        (stale["token_id"],),
    )
    # 89 days without use: still valid, and this use gives it 90 days again.
    sql(
        hub_db,
        "UPDATE tokens SET created_at = now() - interval '100 days', last_used_at = now() - interval '89 days', "
        "expires_at = now() + interval '1 day' WHERE id = %s",
        (fresh["token_id"],),
    )
    assert_login_hint(client.get("/v1/auth/whoami", headers=bearer(stale["token"])))
    assert client.get("/v1/auth/whoami", headers=bearer(fresh["token"])).status_code == 200
    (left,) = sql(hub_db, "SELECT expires_at - now() FROM tokens WHERE id = %s", (fresh["token_id"],))[0]
    assert left.days == 89 or left.days == 90  # now() + 90 days, read a moment later
    listed = client.get("/v1/tokens?all=true", headers=bearer(fresh["token"])).json()
    assert {t["id"]: t["state"] for t in listed} == {stale["token_id"]: "expired", fresh["token_id"]: "active"}


COUNT_EXPIRY_WRITES = """
CREATE TABLE expiry_writes (token_id bigint, at timestamptz DEFAULT clock_timestamp());
CREATE FUNCTION count_expiry_write() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        INSERT INTO expiry_writes (token_id) VALUES (NEW.id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER count_expiry_write AFTER UPDATE ON tokens FOR EACH ROW EXECUTE FUNCTION count_expiry_write();
"""


def expiry_writes(db, token_id: int) -> int:
    return sql(db, "SELECT count(*) FROM expiry_writes WHERE token_id = %s", (token_id,))[0][0]


def test_two_uses_in_one_day_write_the_expiry_once(client, github, hub_db):
    sql(hub_db, COUNT_EXPIRY_WRITES)
    signed = sign_in(client, github, "octo", 101)
    token_id = signed["token_id"]
    (issued,) = sql(hub_db, "SELECT expires_at FROM tokens WHERE id = %s", (token_id,))[0]

    assert client.get("/v1/auth/whoami", headers=bearer(signed["token"])).status_code == 200
    (after_first,) = sql(hub_db, "SELECT expires_at FROM tokens WHERE id = %s", (token_id,))[0]
    assert client.get("/v1/tokens", headers=bearer(signed["token"])).status_code == 200
    (after_second,) = sql(hub_db, "SELECT expires_at FROM tokens WHERE id = %s", (token_id,))[0]
    assert expiry_writes(hub_db, token_id) == 1
    assert after_first > issued and after_second == after_first

    # A day later the next use writes again, once.
    sql(hub_db, "UPDATE tokens SET last_used_at = now() - interval '25 hours' WHERE id = %s", (token_id,))
    for _ in range(3):
        assert client.get("/v1/auth/whoami", headers=bearer(signed["token"])).status_code == 200
    assert expiry_writes(hub_db, token_id) == 2
    (seen,) = sql(hub_db, "SELECT last_seen_at FROM users WHERE login = 'octo'")[0]
    assert seen is not None


def test_requests_racing_over_a_stale_token_write_once(client, config, github, hub_db):
    sql(hub_db, COUNT_EXPIRY_WRITES)
    signed = sign_in(client, github, "octo", 101)
    sql(hub_db, "UPDATE tokens SET last_used_at = now() - interval '2 days' WHERE id = %s", (signed["token_id"],))

    async def race():
        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(hub_db.dsn, min_size=8, max_size=8, open=False) as pool:
            await pool.wait()
            return await asyncio.gather(*(authenticate(pool, signed["token"], "machine", config) for _ in range(8)))

    principals = asyncio.run(race())
    assert {p.token_id for p in principals} == {signed["token_id"]}
    assert expiry_writes(hub_db, signed["token_id"]) == 1


# Server: roles


def test_only_an_admin_can_grant(client, github, hub_db):
    add_project(hub_db, "demo")
    member = sign_in(client, github, "member", 301)
    admin = sign_in(client, github, "Octo-Admin", 302)  # EVO_HUB_ADMINS matches logins case-insensitively
    assert admin["admin"] is True and member["admin"] is False
    grant = "/v1/admin/projects/demo/grants/member"
    body = {"role": "reader", "max_level": "internal"}

    for method, path in (
        ("PUT", grant),
        ("DELETE", grant),
        ("GET", "/v1/admin/users"),
        ("GET", "/v1/admin/stats"),
    ):
        refused = client.request(method, path, json=body if method == "PUT" else None, headers=bearer(member["token"]))
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"] == "forbidden" and "EVO_HUB_ADMINS" in refused.json()["message"]
    assert sql(hub_db, "SELECT count(*) FROM grants")[0][0] == 0
    assert [row[2] for row in audit_rows(hub_db)] == ["auth.login", "auth.login"]

    created = client.put(grant, json=body, headers=bearer(admin["token"]))
    assert created.status_code == 200, created.text
    assert created.json() == {
        "project": "demo",
        "login": "member",
        "role": "reader",
        "max_level": "internal",
        "created": True,
    }
    changed = client.put(grant, json={"role": "writer", "max_level": "customer"}, headers=bearer(admin["token"]))
    assert changed.json()["created"] is False
    me = client.get("/v1/auth/whoami", headers=bearer(member["token"])).json()
    assert me["grants"] == [{"project": "demo", "role": "writer", "max_level": "customer"}]

    assert client.delete(grant, headers=bearer(admin["token"])).status_code == 204
    assert client.delete(grant, headers=bearer(admin["token"])).status_code == 404
    assert audit_rows(hub_db)[2:] == [
        ("Octo-Admin", admin["token_id"], "grant.put", "demo/member role=reader max_level=internal"),
        ("Octo-Admin", admin["token_id"], "grant.put", "demo/member role=writer max_level=customer"),
        ("Octo-Admin", admin["token_id"], "grant.delete", "demo/member"),
    ]


def test_a_grant_needs_a_registered_project_and_a_level_of_its_ladder(client, github, hub_db):
    add_project(hub_db, "custom", levels=["open", "closed"])
    admin = sign_in(client, github, ADMIN, 302)
    headers = bearer(admin["token"])

    reader = {"role": "reader", "max_level": "open"}
    missing = client.put("/v1/admin/projects/nope/grants/member", json=reader, headers=headers)
    assert missing.status_code == 404 and "evo-agents hub project register" in missing.json()["message"]
    wrong = client.put(
        "/v1/admin/projects/custom/grants/member", json={"role": "reader", "max_level": "internal"}, headers=headers
    )
    assert wrong.status_code == 422 and "open, closed" in wrong.json()["message"]
    bad_role = client.put(
        "/v1/admin/projects/custom/grants/member", json={"role": "owner", "max_level": "open"}, headers=headers
    )
    assert bad_role.status_code == 422
    bad_login = client.put("/v1/admin/projects/custom/grants/-x", json=reader, headers=headers)
    assert bad_login.status_code == 422
    assert sql(hub_db, "SELECT count(*) FROM grants")[0][0] == 0

    ok = client.put(
        "/v1/admin/projects/custom/grants/newbie", json={"role": "reader", "max_level": "closed"}, headers=headers
    )
    assert ok.status_code == 200, ok.text
    # The login had not signed in yet; its first sign-in claims the row and the grant.
    users = {u["login"]: u for u in client.get("/v1/admin/users", headers=headers).json()}
    assert users["newbie"]["signed_in"] is False and users["newbie"]["active_tokens"] == 0
    newbie = sign_in(client, github, "NewBie", 303)
    me = client.get("/v1/auth/whoami", headers=bearer(newbie["token"])).json()
    assert me["login"] == "NewBie" and me["grants"] == [{"project": "custom", "role": "reader", "max_level": "closed"}]
    users = {u["login"]: u for u in client.get("/v1/admin/users", headers=headers).json()}
    assert "newbie" not in users and users["NewBie"]["signed_in"] is True and users["NewBie"]["active_tokens"] == 1
    assert users[ADMIN]["admin"] is True and users["NewBie"]["admin"] is False


def test_a_renamed_github_account_keeps_its_row_and_its_grants(client, github, hub_db):
    add_project(hub_db, "demo")
    add_project(hub_db, "other")
    old = sign_in(client, github, "alice", 401)
    admin = bearer(sign_in(client, github, ADMIN, 302)["token"])
    put = {"role": "writer", "max_level": "public"}
    assert client.put("/v1/admin/projects/demo/grants/alice", json=put, headers=admin).status_code == 200

    # alice renames her account to alicia, and someone else takes the login alice before she signs in again:
    # the newcomer gets a row of their own, without alice's grants.
    newcomer = sign_in(client, github, "alice", 999)
    assert client.get("/v1/auth/whoami", headers=bearer(newcomer["token"])).json()["grants"] == []
    assert sql(hub_db, "SELECT login FROM users WHERE github_id = 401") == [("alice_401",)]

    # An admin grants her new login before she signs in with it; signing in merges that grant into her row.
    put = {"role": "reader", "max_level": "internal"}
    assert client.put("/v1/admin/projects/other/grants/alicia", json=put, headers=admin).status_code == 200
    renamed = sign_in(client, github, "alicia", 401)
    for token in (old["token"], renamed["token"]):
        me = client.get("/v1/auth/whoami", headers=bearer(token)).json()
        assert me["login"] == "alicia"
        assert me["grants"] == [
            {"project": "demo", "role": "writer", "max_level": "public"},
            {"project": "other", "role": "reader", "max_level": "internal"},
        ]
    logins = sql(hub_db, "SELECT login, github_id FROM users WHERE login NOT LIKE '%%-owner' ORDER BY login")
    assert logins == [("alice", 999), ("alicia", 401), (ADMIN, 302)]


def test_admin_stats_counts_the_rows_of_every_table(client, github, hub_db):
    add_project(hub_db, "demo")
    admin = sign_in(client, github, ADMIN, 302)
    stats = client.get("/v1/admin/stats", headers=bearer(admin["token"])).json()
    assert set(stats) == HUB_TABLES
    assert stats["users"] == 2 and stats["tokens"] == 1 and stats["projects"] == 1 and stats["audit"] == 1
    assert stats["memories"] == 0


# Server: GitHub's side


def test_github_down_or_slow_is_a_502_naming_github(client, github):
    github_token = github.issue_token(Account("octo", 101))
    body = {"github_token": github_token, "host": "laptop"}

    github.status = 503
    down = client.post("/v1/auth/github", json=body)
    assert down.status_code == 502 and down.json()["error"] == "bad_gateway"
    assert "GitHub answered 503" in down.json()["message"]

    github.status, github.delay = None, 3.0
    started = time.monotonic()
    slow = client.post("/v1/auth/github", json=body)
    assert time.monotonic() - started < 2.9  # the configured timeout is 2 s
    assert slow.status_code == 502 and "GitHub did not answer within 2s" in slow.json()["message"]

    github.delay = 0
    unknown = client.post("/v1/auth/github", json={"github_token": "gho_" + "x" * 36, "host": "laptop"})
    assert_login_hint(unknown)
    assert "GitHub did not accept the token" in unknown.json()["message"]

    github.stop()
    gone = client.post("/v1/auth/github", json=body)
    assert gone.status_code == 502 and "cannot reach GitHub" in gone.json()["message"]


def test_a_token_of_another_app_cannot_sign_in_when_the_secret_is_known(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, **live.web_changes(github))
    with TestClient(create_app(config)) as client:
        foreign = github.issue_token(Account("octo", 101), app=False)
        refused = client.post("/v1/auth/github", json={"github_token": foreign, "host": "laptop"})
        assert_login_hint(refused)
        assert "not issued to this hub's OAuth App" in refused.json()["message"]
        assert github.calls("/user") == []  # refused before asking whose it is
        own = github.issue_token(Account("octo", 101))
        assert client.post("/v1/auth/github", json={"github_token": own, "host": "laptop"}).status_code == 201
    assert sql(hub_db, "SELECT count(*) FROM tokens")[0][0] == 1
    check = github.calls(f"/applications/{github.client_id}/token")[-1]
    assert check.headers["authorization"].startswith("Basic ") and json.loads(check.body) == {"access_token": own}


def test_the_login_body_is_validated_without_echoing_it(client):
    response = client.post("/v1/auth/github", json={"github_token": "gho_Echo0Secret0Value0123", "host": "a\nb"})
    assert response.status_code == 422 and "gho_Echo0Secret0Value0123" not in response.text
    assert client.post("/v1/auth/github", json={"host": "laptop"}).status_code == 422


def test_without_a_client_id_nobody_can_sign_in(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, github_client_id=None)
    with TestClient(create_app(config)) as client:
        response = client.get("/v1/auth/config")
    assert response.status_code == 503 and "EVO_HUB_GITHUB_CLIENT_ID" in response.json()["message"]


# Client: the device flow, files and HTTP


def device(interval: int = 5, expires_in: int = 900) -> hub_client.DeviceCode:
    return hub_client.DeviceCode("dc", "WDJB-MJHT", "https://github.com/login/device", expires_in, interval)


class Clock:
    """A clock that ``sleep`` moves forward, so polling takes no real time."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def poll(github, steps: list[str], interval: int = 5, expires_in: int = 900, clock: Clock | None = None):
    github.device_account, github.device_steps, github.interval = Account("octo", 101), steps, interval
    started = hub_client.start_device_flow(github.url, github.client_id)
    started = hub_client.DeviceCode(
        started.device_code, started.user_code, started.verification_uri, expires_in=expires_in, interval=interval
    )
    clock = clock or Clock()
    token = hub_client.wait_for_token(github.url, github.client_id, started, sleep=clock.sleep, clock=clock)
    return token, clock


def test_device_flow_polling_honours_interval_and_slow_down(github):
    token, clock = poll(github, ["authorization_pending", "slow_down", "authorization_pending"])
    assert token in github.tokens
    assert clock.slept == [5, 5, 10, 10]  # slow_down adds 5 s for every later poll
    (code_call,) = github.calls("/login/device/code")
    assert "scope=read%3Auser" in code_call.body and f"client_id={github.client_id}" in code_call.body


@pytest.mark.parametrize(
    "steps, message",
    [
        (["access_denied"], "cancelled on GitHub"),
        (["authorization_pending", "expired_token"], "expired before it was entered"),
    ],
)
def test_device_flow_stops_on_a_final_answer(github, steps, message):
    with pytest.raises(hub_client.HubError, match=message):
        poll(github, steps)


def test_device_flow_stops_when_the_code_expires(github):
    clock = Clock()
    with pytest.raises(hub_client.HubError, match="expired before it was entered"):
        poll(github, ["authorization_pending"] * 1000, interval=5, expires_in=30, clock=clock)
    assert sum(clock.slept) <= 30


def test_device_flow_gives_up_when_github_stays_down(github):
    github.device_account = Account("octo", 101)
    started = hub_client.start_device_flow(github.url, github.client_id)
    github.status = 503
    clock = Clock()
    with pytest.raises(hub_client.HubError, match="answered HTTP 503"):
        hub_client.wait_for_token(github.url, github.client_id, started, sleep=clock.sleep, clock=clock)
    assert len(github.calls("/login/oauth/access_token")) == hub_client.POLL_FAILURES


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_credentials_are_written_atomically_with_exact_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".evo" / "hub"
    directory.mkdir(parents=True, mode=0o755)
    os.chmod(directory, 0o755)
    (directory / "token").write_text("evh_old\n")
    os.chmod(directory / "token", 0o644)
    old_inode = os.stat(directory / "token").st_ino

    previous = os.umask(0o277)  # a umask that would leave the files read-only and the directory unusable
    try:
        hub_client.save_credentials("https://hub.test", "octo", "evh_new")
    finally:
        os.umask(previous)
    assert mode(directory) == 0o700
    assert mode(directory / "token") == 0o600 and mode(directory / "config.json") == 0o600
    assert (directory / "token").read_text() == "evh_new\n"
    assert os.stat(directory / "token").st_ino != old_inode  # replaced by rename, never rewritten in place
    assert json.loads((directory / "config.json").read_text()) == {"url": "https://hub.test", "login": "octo"}
    assert sorted(p.name for p in directory.iterdir()) == ["config.json", "token"]
    assert hub_client.load_credentials() == hub_client.Credentials("https://hub.test", "octo", "evh_new")
    assert "evh_new" not in repr(hub_client.load_credentials())

    # A failure halfway leaves the old token whole and no temporary file behind.
    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(hub_client.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        hub_client.save_credentials("https://hub.test", "octo", "evh_newer")
    assert (directory / "token").read_text() == "evh_new\n"
    assert sorted(p.name for p in directory.iterdir()) == ["config.json", "token"]

    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path))
    hub_client.remove_credentials()
    with pytest.raises(hub_client.NotSignedIn, match="evo-agents hub login"):
        hub_client.load_credentials()


def test_the_client_refuses_plain_http_to_other_hosts_and_never_follows_redirects(github):
    with pytest.raises(hub_client.HubError, match="must use https"):
        hub_client.check_url("http://hub.example.org")
    with pytest.raises(hub_client.HubError, match="must look like"):
        hub_client.check_url("ftp://hub.example.org")
    assert hub_client.check_url("https://hub.example.org/") == "https://hub.example.org"
    assert hub_client.check_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"

    hub = hub_client.Hub(github.url, "evh_" + "R" * 43)
    with pytest.raises(hub_client.HubError, match="redirect, which is not followed"):
        hub.call("GET", "/moved")
    assert github.calls("/user") == []  # the token never went where the redirect pointed

    unreachable = hub_client.Hub(f"http://127.0.0.1:{pg.free_port()}", "evh_x", timeout=2)
    with pytest.raises(hub_client.HubError, match="cannot reach http://127.0.0.1"):
        unreachable.call("GET", "/v1/auth/whoami")


# Client and server together: the CLI against `hub serve`


def cli(args: list[str], home, github):
    return pg.cli(["hub", *args], env=pg.clean_env(HOME=str(home), EVO_HUB_GITHUB_URL=github.url), timeout=60)


def test_the_cli_signs_in_with_the_device_flow_and_manages_tokens_and_grants(hub_db, tmp_path, github):
    secret = live.session_secret()
    variables = {
        "EVO_HUB_LOG_LEVEL": "DEBUG",
        "EVO_HUB_ADMINS": f"{ADMIN}, someone-else",
        "EVO_HUB_GITHUB_CLIENT_ID": github.client_id,
        "EVO_HUB_GITHUB_CLIENT_SECRET": github.client_secret,
        "EVO_HUB_SESSION_SECRET": secret,
        "EVO_HUB_PUBLIC_URL": "https://hub.test",
        "EVO_HUB_GITHUB_URL": github.url,
        "EVO_HUB_GITHUB_API_URL": github.url,
    }
    admin_home, member_home = tmp_path / "admin-home", tmp_path / "member-home"
    outputs = []
    with live.running_hub(hub_db, tmp_path, **variables) as hub:
        add_project(hub_db, "demo")
        github.device_account, github.device_steps = Account(ADMIN, 7), ["authorization_pending"]
        signed = cli(["login", "--url", hub.url], admin_home, github)
        outputs.append(signed)
        assert signed.returncode == 0, signed.stderr + hub.log()
        assert "WDJB-MJHT" in signed.stdout and f"{github.url}/login/device" in signed.stdout
        assert f"as {ADMIN} (admin)" in signed.stdout

        directory = admin_home / ".evo" / "hub"
        assert mode(directory) == 0o700
        assert mode(directory / "token") == 0o600 and mode(directory / "config.json") == 0o600
        assert sorted(p.name for p in directory.iterdir()) == ["config.json", "token"]
        assert json.loads((directory / "config.json").read_text()) == {"url": hub.url, "login": ADMIN}
        token = (directory / "token").read_text().strip()
        assert token.startswith("evh_") and token not in signed.stdout + signed.stderr
        stored = sql(hub_db, "SELECT token_hash FROM tokens WHERE revoked_at IS NULL")
        assert stored == [(hashlib.sha256(token.encode()).hexdigest(),)]

        whoami = cli(["whoami"], admin_home, github)
        assert whoami.returncode == 0 and whoami.stdout.startswith(f"{ADMIN} (admin) on {hub.url}")

        # A member signs in on another machine and is refused admin commands.
        github.device_account, github.device_steps = Account("member", 8), []
        assert cli(["login", "--url", hub.url], member_home, github).returncode == 0
        grant = ["admin", "grant", "member", "demo", "--role", "reader", "--max-level"]
        refused = cli([*grant, "internal"], member_home, github)
        assert refused.returncode == 1 and "needs a hub admin" in refused.stderr

        granted = cli([*grant, "internal"], admin_home, github)
        assert granted.returncode == 0, granted.stderr
        assert "Granted member reader on demo, up to level internal" in granted.stdout
        wrong_level = cli([*grant, "top"], admin_home, github)
        assert wrong_level.returncode == 1 and "public, internal, customer, secret" in wrong_level.stderr
        member_me = cli(["whoami"], member_home, github)
        assert "grants: demo reader up to internal" in member_me.stdout

        users = json.loads(cli(["admin", "users", "--json"], admin_home, github).stdout)
        assert_json_keys("hub admin users", users)
        (grant,) = {u["login"]: u["grants"] for u in users}["member"]
        expected = {"project": "demo", "role": "reader", "max_level": "internal", "granted_by": ADMIN}
        assert grant.items() >= expected.items()  # plus granted_at
        stats = json.loads(cli(["admin", "stats", "--json"], admin_home, github).stdout)
        assert_json_keys("hub admin stats", stats)
        assert set(stats) == HUB_TABLES and stats["grants"] == 1 and stats["projects"] == 1
        assert cli(["admin", "revoke", "member", "demo"], admin_home, github).returncode == 0
        assert sql(hub_db, "SELECT count(*) FROM grants")[0][0] == 0

        listed = cli(["token", "list"], admin_home, github)
        assert listed.returncode == 0
        header, row = listed.stdout.splitlines()[:2]
        assert "KIND" in header and "EXPIRES" in header
        assert row.startswith("*") and " machine " in row and time.strftime("%Y") in row

        # Signing in again replaces this machine's token and revokes the old one.
        github.device_account = Account(ADMIN, 7)
        assert cli(["login"], admin_home, github).returncode == 0
        new_token = (directory / "token").read_text().strip()
        assert new_token != token
        assert is_revoked(hub_db, token)

        out = cli(["logout"], admin_home, github)
        assert out.returncode == 0 and "revoked and deleted" in out.stdout
        assert sorted(p.name for p in directory.iterdir()) == []
        assert is_revoked(hub_db, new_token)
        after = cli(["whoami"], admin_home, github)
        assert after.returncode == 1 and "not signed in" in after.stderr and "evo-agents hub login" in after.stderr

        # A revoked token on disk: the hub's 401 tells the person what to run.
        member_token = (member_home / ".evo" / "hub" / "token").read_text().strip()
        sql(hub_db, "UPDATE tokens SET revoked_at = now() WHERE token_hash = %s", (token_hash(member_token),))
        stale = cli(["token", "list"], member_home, github)
        assert stale.returncode == 1 and "evo-agents hub login" in stale.stderr
    log = hub.log()
    lines = pg.log_lines(log)
    assert any(line["msg"] == "github call" and line["path"] == "/user" for line in lines)
    for value in {token, new_token, member_token, secret, *github.secrets()}:
        assert value not in log, "a secret reached the hub's log"
    for result in outputs:
        assert github.client_secret not in result.stdout + result.stderr


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_revoked(db, token: str) -> bool:
    return sql(db, "SELECT revoked_at IS NOT NULL FROM tokens WHERE token_hash = %s", (token_hash(token),)) == [(True,)]


def test_client_errors_are_one_line_on_stderr(tmp_path):
    result = pg.cli(["hub", "whoami"], env=pg.clean_env(HOME=str(tmp_path)))
    assert result.returncode == 1
    assert result.stderr.strip() == "error: not signed in to a hub: run `evo-agents hub login --url URL`"
    insecure = pg.cli(["hub", "login", "--url", "http://hub.example.org"], env=pg.clean_env(HOME=str(tmp_path)))
    assert insecure.returncode == 1 and "must use https" in insecure.stderr
