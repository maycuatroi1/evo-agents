"""The web sign-in against a fake GitHub: the login redirect carries a state and a PKCE S256 challenge kept in a
signed cookie, a callback whose state does not match is 400 and creates no session, the session cookie is
HttpOnly, Secure and SameSite=Lax, a write made with the cookie needs the session's X-Evo-CSRF header, a cookie
is 401 once its session is logged out or expired, the web routes answer 503 without the client secret while the
device flow keeps working, and no log line holds the client secret, a code or a token."""

import base64
import hashlib
import io
import json
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import httpx
from fastapi.testclient import TestClient

from evo_agents.hub import log as hub_log
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.security import SESSION_COOKIE, TOKEN_TTL, hash_token, sign
from evo_agents.hub.server.web_auth import LOGIN_COOKIE, LOGIN_PURPOSE
from tests.hub.fake_github import Account
from tests.hub.live import bearer, sql

OCTO = Account("octo", 101)
CALLBACK = "/v1/auth/web/callback"


@pytest.fixture
def config(hub_db, tmp_path, github):
    return live.hub_config(hub_db, tmp_path, github, **live.web_changes(github))


@pytest.fixture
def client(config):
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def set_cookie(response, name: str) -> dict | None:
    """The Set-Cookie of ``name`` in ``response`` as {value, attribute: value}, attribute names lowercased."""
    for header in response.headers.get_list("set-cookie"):
        first, *attributes = [part.strip() for part in header.split(";")]
        key, _, value = first.partition("=")
        if key == name:
            parsed = {"value": value.strip('"')}
            for attribute in attributes:
                attr, _, attr_value = attribute.partition("=")
                parsed[attr.lower()] = attr_value or True
            return parsed
    return None


def is_deleted(cookie: dict | None) -> bool:
    return cookie is not None and cookie["value"] == "" and cookie.get("max-age") == "0"


def cookie(value: str, name: str = SESSION_COOKIE) -> dict:
    return {"Cookie": f"{name}={value}"}


def start_login(client):
    response = client.get("/v1/auth/web/login", follow_redirects=False)
    assert response.status_code == 302, response.text
    return response, dict(parse_qsl(urlsplit(response.headers["location"]).query))


def authorize(github, query: dict) -> dict:
    """What a browser does at GitHub's authorize page: the query GitHub sends it back to the callback with."""
    answer = httpx.get(f"{github.url}/login/oauth/authorize?{urlencode(query)}", follow_redirects=False)
    assert answer.status_code == 302, answer.text
    assert answer.headers["location"].startswith(query["redirect_uri"] + "?")
    return dict(parse_qsl(urlsplit(answer.headers["location"]).query))


def web_sign_in(client, github, account: Account = OCTO) -> str:
    """A full web sign-in; the session cookie's value. The client's cookie jar is left empty."""
    github.browser_account = account
    _, query = start_login(client)
    response = client.get(CALLBACK, params=authorize(github, query), follow_redirects=False)
    assert response.status_code == 303, response.text
    client.cookies.clear()
    return set_cookie(response, SESSION_COOKIE)["value"]


def csrf_for(client, session: str) -> str:
    response = client.get("/v1/auth/web/csrf", headers=cookie(session))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["header"] == "X-Evo-CSRF"
    return response.json()["csrf"]


def token_id_of(client, session: str) -> int:
    return client.get("/v1/auth/whoami", headers=cookie(session)).json()["token"]["id"]


def test_web_login_redirects_to_github_with_state_and_pkce_in_a_signed_cookie(client, github, hub_db):
    response, query = start_login(client)
    assert response.headers["location"].startswith(f"{github.url}/login/oauth/authorize?")
    assert response.headers["cache-control"] == "no-store"
    assert query["client_id"] == github.client_id
    assert query["redirect_uri"] == "https://hub.test/v1/auth/web/callback"
    assert (query["scope"], query["code_challenge_method"]) == ("read:user", "S256")
    assert len(query["state"]) >= 43 and len(query["code_challenge"]) == 43
    assert github.client_secret not in response.headers["location"]

    login_cookie = set_cookie(response, LOGIN_COOKIE)
    assert login_cookie["httponly"] is True and login_cookie["secure"] is True
    assert login_cookie["samesite"].lower() == "lax" and login_cookie["path"] == CALLBACK
    assert login_cookie["max-age"] == "600"
    assert set_cookie(response, SESSION_COOKIE) is None
    _, again = start_login(client)
    assert again["state"] != query["state"] and again["code_challenge"] != query["code_challenge"]
    assert sql(hub_db, "SELECT count(*) FROM tokens")[0][0] == 0


def test_a_web_sign_in_sets_a_secure_session_cookie_that_works(client, github, hub_db):
    github.browser_account = OCTO
    _, query = start_login(client)
    back = authorize(github, query)
    response = client.get(CALLBACK, params=back, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/" and response.headers["cache-control"] == "no-store"

    session_cookie = set_cookie(response, SESSION_COOKIE)
    session = session_cookie["value"]
    assert session.startswith("evs_") and len(session) == 4 + 43
    assert session_cookie["httponly"] is True and session_cookie["secure"] is True
    assert session_cookie["samesite"].lower() == "lax" and session_cookie["path"] == "/"
    assert session_cookie["max-age"] == str(int(TOKEN_TTL.total_seconds()))
    assert is_deleted(set_cookie(response, LOGIN_COOKIE))  # spent

    # GitHub saw the client secret and the PKCE verifier in the POST body of the exchange, nowhere else.
    (exchange,) = github.calls("/login/oauth/access_token")
    form = dict(parse_qsl(exchange.body))
    assert form["client_secret"] == github.client_secret and form["code"] == back["code"]
    assert form["redirect_uri"] == "https://hub.test/v1/auth/web/callback"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
    assert challenge.decode() == query["code_challenge"]
    assert all(github.client_secret not in json.dumps(r.query) for r in github.requests)

    (row,) = sql(hub_db, "SELECT id, kind, host, token_hash FROM tokens")
    assert row[1:] == ("web", None, hash_token(session))
    stored = live.table_dump(hub_db)
    assert session not in stored and not any(token in stored for token in github.tokens)

    client.cookies.clear()
    me = client.get("/v1/auth/whoami", headers=cookie(session))
    assert me.status_code == 200, me.text
    assert me.json()["login"] == "octo" and me.json()["token"]["kind"] == "web"
    assert sql(hub_db, "SELECT action, target, token_id FROM audit") == [("auth.login", "web", row[0])]

    # The callback cannot be replayed: the code is spent at GitHub and the login cookie in the browser.
    replay = client.get(CALLBACK, params=back, follow_redirects=False)
    assert replay.status_code == 400 and set_cookie(replay, SESSION_COOKIE) is None


def test_a_callback_with_a_wrong_state_is_400_and_creates_no_session(client, github, hub_db, config):
    github.browser_account = OCTO
    secret = config.session_secret

    def refused(params: dict, headers: dict | None = None, reason: str = "state does not match"):
        response = client.get(CALLBACK, params=params, headers=headers, follow_redirects=False)
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "bad_request" and reason in response.json()["message"]
        assert set_cookie(response, SESSION_COOKIE) is None
        assert is_deleted(set_cookie(response, LOGIN_COOKIE))
        client.cookies.clear()

    _, query = start_login(client)
    back = authorize(github, query)
    refused({"code": back["code"], "state": "x" * 43})  # a state of another sign-in
    refused({"code": back["code"]})  # no state at all

    _, query = start_login(client)
    back = authorize(github, query)
    client.cookies.clear()
    refused(back)  # the right state, but no login cookie to compare it with

    login_response, _ = start_login(client)
    body, _, mac = set_cookie(login_response, LOGIN_COOKIE)["value"].partition(".")
    tampered = f"{body}.{mac[:-1]}{'A' if mac[-1] != 'A' else 'B'}"
    state = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["state"]
    client.cookies.clear()
    refused({"code": "c", "state": state}, cookie(tampered, LOGIN_COOKIE))

    payload = {"state": state, "verifier": "v" * 43}
    for forged in (
        sign(secret, LOGIN_PURPOSE, payload, ttl=-5),  # expired
        sign("another-secret-" + "x" * 32, LOGIN_PURPOSE, payload, ttl=600),  # signed with another key
        sign(secret, "csrf", payload, ttl=600),  # signed for another purpose
    ):
        refused({"code": "c", "state": state}, cookie(forged, LOGIN_COOKIE))

    _, query = start_login(client)
    refused({"error": "access_denied", "state": query["state"]}, reason="GitHub did not authorize the sign-in")
    assert github.calls("/login/oauth/access_token") == []  # nothing above reached the code exchange

    _, query = start_login(client)
    refused({"code": "not-a-code", "state": query["state"]}, reason="GitHub refused the sign-in code")
    assert sql(hub_db, "SELECT count(*) FROM tokens")[0][0] == 0
    assert sql(hub_db, "SELECT count(*) FROM users")[0][0] == 0


def test_writes_with_the_session_cookie_need_the_csrf_header(client, github, hub_db):
    session = web_sign_in(client, github)
    machine = live.sign_in(client, github, "octo", 101)
    revoke_machine = f"/v1/tokens/{machine['token_id']}"

    for headers in ({}, {"X-Evo-CSRF": "nope"}, {"X-Evo-CSRF": ""}):
        response = client.delete(revoke_machine, headers={**cookie(session), **headers})
        assert response.status_code == 403, response.text
        assert response.json()["error"] == "forbidden" and "X-Evo-CSRF" in response.json()["message"]
    other_session = web_sign_in(client, github)
    response = client.delete(revoke_machine, headers={**cookie(session), "X-Evo-CSRF": csrf_for(client, other_session)})
    assert response.status_code == 403  # the token is bound to its session
    admin_write = client.put(
        "/v1/admin/projects/demo/grants/x", json={"role": "reader", "max_level": "public"}, headers=cookie(session)
    )
    assert admin_write.status_code == 403 and "X-Evo-CSRF" in admin_write.json()["message"]
    assert client.get("/v1/auth/whoami", headers=bearer(machine["token"])).status_code == 200
    assert client.get("/v1/auth/whoami", headers=cookie(session)).status_code == 200  # reads need no header

    token = csrf_for(client, session)
    assert csrf_for(client, session) == token
    response = client.delete(revoke_machine, headers={**cookie(session), "X-Evo-CSRF": token})
    assert response.status_code == 204, response.text
    assert client.get("/v1/auth/whoami", headers=bearer(machine["token"])).status_code == 401

    # A Bearer request needs no CSRF header, and gets no CSRF token.
    second = live.sign_in(client, github, "octo", 101)
    other_id = token_id_of(client, other_session)
    assert client.delete(f"/v1/tokens/{other_id}", headers=bearer(second["token"])).status_code == 204
    assert client.get("/v1/auth/whoami", headers=cookie(other_session)).status_code == 401
    assert client.get("/v1/auth/web/csrf", headers=bearer(second["token"])).status_code == 400


def test_the_session_cookie_is_401_after_logout(client, github, hub_db):
    session = web_sign_in(client, github)
    token_id = token_id_of(client, session)
    token = csrf_for(client, session)
    assert client.post("/v1/auth/web/logout", headers=cookie(session)).status_code == 403

    response = client.post("/v1/auth/web/logout", headers={**cookie(session), "X-Evo-CSRF": token})
    assert response.status_code == 204, response.text
    deleted = set_cookie(response, SESSION_COOKIE)
    assert is_deleted(deleted) and deleted["path"] == "/" and deleted["secure"] is True

    after = client.get("/v1/auth/whoami", headers=cookie(session))
    assert after.status_code == 401 and "evo-agents hub login" in after.json()["message"]
    assert is_deleted(set_cookie(after, SESSION_COOKIE))  # the browser is told to drop it
    again = client.post("/v1/auth/web/logout", headers={**cookie(session), "X-Evo-CSRF": token})
    assert again.status_code == 401
    assert sql(hub_db, "SELECT revoked_at IS NOT NULL FROM tokens WHERE id = %s", (token_id,)) == [(True,)]
    assert sql(hub_db, "SELECT action, target FROM audit ORDER BY id")[-1] == ("auth.logout", f"token:{token_id}")

    # Expiry works for web sessions as for machine tokens.
    expiring = web_sign_in(client, github)
    sql(
        hub_db,
        "UPDATE tokens SET created_at = now() - interval '100 days', expires_at = now() - interval '1 day' "
        "WHERE token_hash = %s",
        (hash_token(expiring),),
    )
    assert client.get("/v1/auth/whoami", headers=cookie(expiring)).status_code == 401


def test_a_session_and_a_machine_token_each_work_only_through_their_own_channel(client, github):
    session = web_sign_in(client, github)
    machine = live.sign_in(client, github, "octo", 101)
    assert client.get("/v1/auth/whoami", headers=bearer(session)).status_code == 401
    assert client.get("/v1/auth/whoami", headers=cookie(machine["token"])).status_code == 401
    assert client.get("/v1/auth/whoami", headers=cookie(session)).status_code == 200


@pytest.mark.parametrize(
    "missing, variable",
    [
        ("github_client_secret", "EVO_HUB_GITHUB_CLIENT_SECRET"),
        ("session_secret", "EVO_HUB_SESSION_SECRET"),
        ("public_url", "EVO_HUB_PUBLIC_URL"),
    ],
)
def test_without_its_configuration_the_web_sign_in_answers_503_and_the_device_flow_still_works(
    hub_db, tmp_path, github, missing, variable
):
    config = live.hub_config(hub_db, tmp_path, github, **{**live.web_changes(github), missing: None})
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        for path in ("/v1/auth/web/login", f"{CALLBACK}?code=c&state=s"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 503, response.text
            assert response.json()["error"] == "unavailable" and variable in response.json()["message"]
            assert response.headers.get_list("set-cookie") == []
            assert github.client_secret not in response.text
        assert client.get("/v1/auth/config").json() == {"github_client_id": github.client_id, "web_login": False}

        signed = live.sign_in(client, github, "octo", 101)  # what the CLI does once the device flow ends
        assert client.get("/v1/auth/whoami", headers=bearer(signed["token"])).status_code == 200
        assert client.get("/v1/auth/web/csrf", headers=bearer(signed["token"])).status_code == 503
    assert github.calls("/login/oauth/access_token") == []


@pytest.fixture
def hub_logs():
    """Every record the hub logs at DEBUG, as the JSON lines ``hub serve`` writes and as raw records."""
    stream, records = io.StringIO(), []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    root = logging.getLogger()
    saved = root.level, {name: logging.getLogger(name).level for name in hub_log.QUIET_LOGGERS}
    hub_log.configure_logging("DEBUG", stream)
    keep = Keep(logging.DEBUG)
    root.addHandler(keep)
    try:
        yield stream, records
    finally:
        for handler in list(root.handlers):
            if handler is keep or isinstance(handler, hub_log._HubHandler):
                root.removeHandler(handler)
        root.setLevel(saved[0])
        for name, level in saved[1].items():
            logging.getLogger(name).setLevel(level)
        logging.captureWarnings(False)


def test_logs_hold_no_client_secret_code_or_token(client, github, config, hub_logs):
    stream, records = hub_logs
    session = web_sign_in(client, github)
    token = csrf_for(client, session)
    machine = live.sign_in(client, github, "octo", 101)
    foreign = github.issue_token(OCTO, app=False)
    assert client.post("/v1/auth/github", json={"github_token": foreign, "host": "laptop"}).status_code == 401
    assert client.delete(f"/v1/tokens/{machine['token_id']}", headers=cookie(session)).status_code == 403
    response = client.delete(f"/v1/tokens/{machine['token_id']}", headers={**cookie(session), "X-Evo-CSRF": token})
    assert response.status_code == 204

    github.browser_account = OCTO
    login_response, query = start_login(client)
    login_cookie = set_cookie(login_response, LOGIN_COOKIE)["value"]
    body = login_cookie.split(".")[0]
    verifier = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["verifier"]
    back = authorize(github, query)
    assert client.get(CALLBACK, params={**back, "state": "wrong"}, follow_redirects=False).status_code == 400
    _, query = start_login(client)
    bad_code = {"code": "Bad-Code-Never-Logged", "state": query["state"]}
    assert client.get(CALLBACK, params=bad_code, follow_redirects=False).status_code == 400
    assert client.post("/v1/auth/web/logout", headers={**cookie(session), "X-Evo-CSRF": token}).status_code == 204
    assert client.get("/v1/auth/whoami", headers=cookie(session)).status_code == 401

    text = stream.getvalue()
    lines = pg.log_lines(text)
    messages = {line["msg"] for line in lines}
    assert {"github call", "signed in", "signed out", "request"} <= messages
    raw = "\n".join(f"{record.getMessage()} {record.args!r} {vars(record)!r}" for record in records)
    values = {
        config.github_client_secret,
        config.session_secret,
        session,
        token,
        machine["token"],
        login_cookie,
        verifier,
        "Bad-Code-Never-Logged",
        *github.secrets(),  # every GitHub token and code the fake handed out, and the client secret
    }
    assert back["code"] in values and len(github.tokens) >= 3
    for value in values:
        assert value not in text, "a secret reached a log line"
        assert value not in raw, "a secret reached a log record"
