"""App sessions (plan evo-creative-workspace, step 1, criterion a1): a first-party app registered under a secret the hub
seals trades the enterprise identity of a person for an interactive Bearer session of that one member, who has no
GitHub account.

The checks the step names: a session is issued; the same app and subject find the same users row, with ``github_id``
NULL; a bad signature, an unknown app or a stale timestamp is refused; the session passes ``web_only_steering`` and a
worker's ``dispatch_from`` web; the person's own grants and autonomy apply to it; and the GitHub sign-in and the tokens
evh_, evs_, evw_ and evr_ keep their behaviour (a9). Around them: the admin routes of the apps, a login another member
holds, and the revocation of an app ending its sessions."""

import json
import secrets
import time

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from evo_agents.hub import tables
from evo_agents.hub.server import app_sessions
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.credentials.models.sealing import KEY_BYTES
from evo_agents.hub.server.security import APP, PREFIXES, RUN, WEB, WORKER
from tests.hub.fake_github import Account
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_runs import (
    GITHUB_IDS,
    OWNER,
    PROJECT,
    WEB_ONLY,
    WRITER,
    add_worker,
    claim,
    dispatch_from,
    dispatched,
    members,
)
from tests.hub.test_web_auth import cookie, csrf_for, web_sign_in

KEY = secrets.token_bytes(KEY_BYTES)
APP_NAME = "lumen-studio"
SUBJECT = "a1b2c3d4-0000-4000-8000-northwind01"
MEMBER = f"mai_{APP_NAME}"


@pytest.fixture
def client(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, secrets_key=KEY, **live.web_changes(github))
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The members of ``tests.hub.test_runs``, the owner's web session, and the app lumen-studio registered by the
    hub admin: the headers of each, and the app's secret under "secret"."""
    headers = members(client, github)
    session = web_sign_in(client, github, Account(OWNER, GITHUB_IDS[OWNER]))
    headers["owner_web"] = {**cookie(session), "X-Evo-CSRF": csrf_for(client, session)}
    headers["owner_session"] = session
    made = client.post("/v1/admin/auth-apps", json={"name": APP_NAME}, headers=headers["admin"])
    assert made.status_code == 201, made.text
    assert made.headers["cache-control"] == "no-store"
    headers["secret"] = made.json()["secret"]
    return headers


def signed(secret: str, body: dict, *, at: int | None = None) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    timestamp = str(int(time.time()) if at is None else at)
    headers = {
        "Content-Type": "application/json",
        app_sessions.TIMESTAMP_HEADER: timestamp,
        app_sessions.SIGNATURE_HEADER: app_sessions.signature(secret, timestamp, raw),
    }
    return raw, headers


def ask(client, secret: str, subject: str = SUBJECT, login: str = "mai", app: str = APP_NAME, **sign):
    raw, headers = signed(secret, {"app": app, "subject": subject, "login": login}, **sign)
    return client.post("/v1/auth/app-session", content=raw, headers=headers)


def session(client, hub, **asked) -> dict:
    response = ask(client, hub["secret"], **asked)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def user_rows(db, login: str) -> list[tuple]:
    u = tables.users
    return sql(db, select(u.c.id, u.c.github_id).where(func.lower(u.c.login) == login.lower()))


def test_an_app_session_is_issued_for_a_new_member_with_no_github_account(client, hub, hub_db):
    answer = session(client, hub)
    assert answer["token"].startswith(PREFIXES[APP]) and answer["login"] == MEMBER and answer["admin"] is False
    [(user_id, github_id)] = user_rows(hub_db, MEMBER)
    assert github_id is None
    who = client.get("/v1/auth/whoami", headers=bearer(answer["token"])).json()
    assert who["login"] == MEMBER and who["token"]["kind"] == WEB and who["token"]["host"] == f"app:{APP_NAME}"
    a = tables.audit
    logged = sql(hub_db, select(a.c.action, a.c.target).where(a.c.actor_id == user_id))
    assert logged == [("auth.login", f"app:{APP_NAME}")]
    # the secret is stored sealed, never as given
    assert hub["secret"] not in live.table_dump(hub_db)
    listed = client.get("/v1/admin/auth-apps", headers=hub["admin"]).json()
    assert [(app["name"], app["created_by"], app["members"]) for app in listed] == [(APP_NAME, ADMIN, 1)]
    assert "secret" not in listed[0]


def test_the_same_subject_finds_the_same_member_whatever_login_it_asks_for(client, hub, hub_db):
    first = session(client, hub)
    again = session(client, hub, login="mai-renamed")
    assert again["login"] == first["login"] == MEMBER and again["token"] != first["token"]
    assert len(user_rows(hub_db, MEMBER)) == 1 and user_rows(hub_db, f"mai-renamed_{APP_NAME}") == []
    # another subject asking for that login is another person: refused, nothing made
    taken = ask(client, hub["secret"], subject="someone-else")
    assert taken.status_code == 409, taken.text
    assert sql(hub_db, select(func.count()).select_from(tables.app_identities)) == [(1,)]


def test_a_bad_signature_an_unknown_app_or_a_stale_timestamp_is_refused(client, hub, hub_db):
    wrong = "evk_" + secrets.token_urlsafe(32)
    stale = int(time.time()) - app_sessions.WINDOW - 5
    for response in (
        ask(client, wrong),
        ask(client, hub["secret"], app="lumen-other"),
        ask(client, hub["secret"], at=stale),
        client.post("/v1/auth/app-session", json={"app": APP_NAME, "subject": SUBJECT, "login": "mai"}),
    ):
        assert response.status_code == 401, response.text
        assert response.json()["message"] == app_sessions.REFUSED
    # a body changed after it was signed does not hold either
    raw, headers = signed(hub["secret"], {"app": APP_NAME, "subject": SUBJECT, "login": "mai"})
    changed = raw.replace(b'"mai"', b'"eve"')
    assert client.post("/v1/auth/app-session", content=changed, headers=headers).status_code == 401
    assert user_rows(hub_db, MEMBER) == []


def test_a_revoked_app_signs_nobody_in_and_its_sessions_end(client, hub, hub_db):
    token = session(client, hub)["token"]
    assert client.delete(f"/v1/admin/auth-apps/{APP_NAME}", headers=hub["owner"]).status_code == 403
    assert client.delete(f"/v1/admin/auth-apps/{APP_NAME}", headers=hub["admin"]).status_code == 204
    assert client.get("/v1/auth/whoami", headers=bearer(token)).status_code == 401
    assert ask(client, hub["secret"]).status_code == 401
    assert client.delete(f"/v1/admin/auth-apps/{APP_NAME}", headers=hub["admin"]).status_code == 404
    # the owner's own web session is no app's
    assert client.get("/v1/auth/whoami", headers=hub["owner_web"]).status_code == 200


def test_an_app_session_goes_as_bearer_only_and_a_web_session_as_the_cookie_only(client, hub):
    token = session(client, hub)["token"]
    assert client.get("/v1/auth/whoami", headers=cookie(token)).status_code == 401
    assert client.get("/v1/auth/whoami", headers=bearer(hub["owner_session"])).status_code == 401
    # it is no cookie: it needs no CSRF header, and gets none
    assert client.get("/v1/auth/web/csrf", headers=bearer(token)).status_code == 400


def test_the_members_own_grants_and_autonomy_apply_to_the_app_session(client, hub):
    # granted before the first sign-in, under the login the app's members get
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{MEMBER}", json=WRITER, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    member = bearer(session(client, hub)["token"])
    who = client.get("/v1/auth/whoami", headers=member).json()
    assert [(grant["project"], grant["role"]) for grant in who["grants"]] == [(PROJECT, "writer")]
    assert client.get(f"/v1/projects/{PROJECT}/plans", headers=member).status_code == 200
    stranger = bearer(session(client, hub, subject="northwind-02", login="lan")["token"])
    assert client.get(f"/v1/projects/{PROJECT}/plans", headers=stranger).status_code in (403, 404)

    written = client.put("/v1/autonomy/prefs", json={"level": "A2"}, headers=member)
    assert written.status_code == 200, written.text
    own = client.get("/v1/autonomy", headers=member).json()
    assert (own["principal"], own["level"]) == (MEMBER, "A2")
    assert client.get("/v1/autonomy", params={"login": OWNER}, headers=member).status_code == 403
    assert client.get("/v1/autonomy", params={"login": MEMBER}, headers=hub["owner"]).status_code == 403


def test_an_app_session_passes_dispatch_from_web_and_web_only_steering(client, hub, hub_db):
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{MEMBER}", json=WRITER, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    member = bearer(session(client, hub)["token"])
    machine = bearer(live.insert_token(hub_db, MEMBER))
    guarded = add_worker(client, machine, "lumen-mac")
    dispatch_from(client, {"owner_web": member}, guarded, "web")
    # the owner's web session sets it as before, and a token of the member is refused
    owned = add_worker(client, hub["owner"], "mac-mini")
    dispatch_from(client, hub, owned, "web")
    by_token = client.post(f"/v1/workers/{guarded['id']}/dispatch-from", json={"value": "any"}, headers=machine)
    assert by_token.status_code == 403, by_token.text

    by_app = dispatched(client, member, [2])[0]
    assert by_app["dispatched_via"] == "web" and by_app["dispatched_by"] == MEMBER
    assert claim(client, guarded)["id"] == by_app["id"]

    def message(headers):
        path = f"/v1/projects/{PROJECT}/runs/{by_app['id']}/messages"
        return client.post(path, json={"text": "Keep the old API."}, headers=headers)

    refused = message(machine)
    assert refused.status_code == 403 and WEB_ONLY in refused.json()["message"]
    assert message(member).status_code == 201


def test_the_github_sign_in_and_the_old_kinds_of_token_keep_their_behaviour(client, hub, hub_db):
    assert (PREFIXES[WEB], PREFIXES[WORKER], PREFIXES[RUN]) == ("evs_", "evw_", "evr_")
    assert client.get("/v1/auth/whoami", headers=hub["owner"]).json()["token"]["kind"] == "machine"
    assert client.get("/v1/auth/whoami", headers=hub["owner_web"]).json()["token"]["kind"] == WEB
    web, _ = live.web_session(hub_db, OWNER)
    assert client.get("/v1/auth/whoami", headers=bearer(web)).status_code == 401  # evs_ goes as the cookie only
    worker = add_worker(client, hub["owner"], "linux-box")
    assert client.get("/v1/auth/whoami", headers=worker["headers"]).status_code == 403
    # a GitHub login never claims an app's member, nor an app's sign-in a GitHub member
    session(client, hub)
    assert ask(client, hub["secret"], subject="northwind-03", login=OWNER).json()["login"] == f"{OWNER}_{APP_NAME}"
    assert user_rows(hub_db, OWNER)[0][1] == GITHUB_IDS[OWNER]
    assert client.post("/v1/admin/auth-apps", json={"name": APP_NAME}, headers=hub["admin"]).status_code == 409
    assert client.post("/v1/admin/auth-apps", json={"name": "lumen-x"}, headers=hub["owner"]).status_code == 403
