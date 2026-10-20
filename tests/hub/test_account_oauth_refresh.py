"""The refresh of an OAuth account's access token when a run is leased its credentials
(``evo_agents.hub.server.account_refresh``).

The checks the claude-oauth-login plan names for its second step, against a fake token endpoint: an access token with
time left is leased as it is and the endpoint is not called; one near its end (or past it) is traded for a new one with
the refresh token, the rotated set (access token, refresh token, end) is stored, sealed, together with the secret's
value, and the run gets the new access token as CLAUDE_CODE_OAUTH_TOKEN; runs of one account that lease at the same
time refresh once; a refresh the endpoint refuses leaves the set as it was and leases no token that has ended. Without
EVO_HUB_TEST_DSN they skip."""

import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select, update

from evo_agents.hub import tables
from evo_agents.hub.server.account_oauth import open_tokens
from evo_agents.hub.server.sealing import Sealed, Sealer, secret_aad
from tests.hub.live import sql
from tests.hub.test_credentials_api import (  # noqa: F401 (fixtures of this module)
    app_key,
    ask,
    client,
    config,
    hub,
    leased,
    plan_run,
    step_run,
    worker_of,
)
from tests.hub.test_runs import PROJECT

VAR = "CLAUDE_CODE_OAUTH_TOKEN"
LIFETIME = 28800


def sample(prefix: str) -> str:
    return prefix + secrets.token_hex(24)


class FakeAnthropic(ThreadingMixIn, HTTPServer):
    """The token endpoint, for the refresh grant: each refresh token it knows buys one new set, and is spent."""

    daemon_threads = True

    def __init__(self):
        outer = self
        self.asked: list[dict] = []
        self.valid: dict[str, bool] = {}
        self.down = False
        self.delay = 0.0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.asked.append(body)
                threading.Event().wait(outer.delay)
                ok = body.get("grant_type") == "refresh_token" and outer.valid.pop(body.get("refresh_token"), False)
                answer = (
                    {
                        "access_token": sample("sk-ant-oat01-"),
                        "refresh_token": sample("sk-ant-ort01-"),
                        "expires_in": LIFETIME,
                    }
                    if ok
                    else {"error": "invalid_grant"}
                )
                data = json.dumps(answer).encode()
                self.send_response(503 if outer.down else 200 if ok else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                outer.last = answer

            def log_message(self, *args):
                return

        super().__init__(("127.0.0.1", 0), Handler)
        self.last: dict = {}
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1/oauth/token"


@pytest.fixture
def anthropic(monkeypatch):
    server = FakeAnthropic()
    monkeypatch.setenv("EVO_CLAUDE_OAUTH_TOKEN_URL", server.url)
    yield server
    server.shutdown()
    server.server_close()


def add_account(client, hub, anthropic, left: timedelta, name: str = "claude-oauth") -> tuple[str, str]:
    """An OAuth account whose access token ends in ``left``; (its access token, its refresh token)."""
    access, refresh = sample("sk-ant-oat01-"), sample("sk-ant-ort01-")
    anthropic.valid[refresh] = True
    body = {
        "kind": "env",
        "env_var": VAR,
        "value": access,
        "projects": [PROJECT],
        "oauth": {
            "refresh_token": refresh,
            "access_expires_at": (datetime.now(UTC) + left).isoformat(),
            "account_uuid": "acc-1",
        },
    }
    put = client.put(f"/v1/secrets/{name}", json=body, headers=hub["owner"])
    assert put.status_code == 200, put.text
    return access, refresh


def stored(hub_db, client, name: str = "claude-oauth") -> tuple[dict, str, datetime]:
    """(the sealed set, the secret's value, the secret's end) as the database holds them now."""
    sealer: Sealer = client.app.state.sealer
    s, a = tables.secrets, tables.accounts
    ((secret_id, sealed, nonce, key_id, expires_at, oauth, oauth_nonce, oauth_key),) = sql(
        hub_db,
        select(
            s.c.id,
            s.c.sealed,
            s.c.nonce,
            s.c.key_id,
            s.c.expires_at,
            a.c.oauth_sealed,
            a.c.oauth_nonce,
            a.c.oauth_key_id,
        )
        .join(a, a.c.secret_id == s.c.id)
        .where(s.c.name == name, s.c.deleted_at.is_(None)),
    )
    held = open_tokens(sealer, secret_id, Sealed(bytes(oauth), bytes(oauth_nonce), oauth_key))
    owner_id = sql(hub_db, select(s.c.owner_id).where(s.c.id == secret_id))[0][0]
    value = sealer.open(Sealed(bytes(sealed), bytes(nonce), key_id), secret_aad(owner_id, name, "env"))
    return held, value, expires_at


def run_of(client, hub):
    worker = worker_of(client, hub["owner"], "mac-mini", slots=3)
    return worker, plan_run(client, hub["owner"], worker)


def test_an_access_token_with_time_left_is_leased_as_it_is_and_the_endpoint_is_not_called(
    client, hub, hub_db, anthropic
):
    access, _ = add_account(client, hub, anthropic, timedelta(hours=6))
    worker, run_id = run_of(client, hub)
    lease = {item["name"]: item for item in leased(client, worker, run_id)["leases"]}["claude-oauth"]
    assert (lease["env_var"], lease["value"]) == (VAR, access)
    assert anthropic.asked == []


@pytest.mark.parametrize("left", [timedelta(minutes=1), timedelta(seconds=2)], ids=["near its end", "about to end"])
def test_an_access_token_near_its_end_is_refreshed_and_the_rotated_set_is_stored_and_leased(
    client, hub, hub_db, anthropic, left
):
    access, refresh = add_account(client, hub, anthropic, left)
    worker, run_id = run_of(client, hub)
    answer = leased(client, worker, run_id)
    lease = {item["name"]: item for item in answer["leases"]}["claude-oauth"]

    assert len(anthropic.asked) == 1
    assert anthropic.asked[0]["refresh_token"] == refresh and anthropic.asked[0]["grant_type"] == "refresh_token"
    new_access, new_refresh = anthropic.last["access_token"], anthropic.last["refresh_token"]
    assert lease["value"] == new_access != access and lease["env_var"] == VAR
    held, value, ends = stored(hub_db, client)
    # the rotated refresh token is the one kept, with the value of the secret and the set agreeing on the new token
    assert held["refresh_token"] == new_refresh != refresh
    assert held["access_token"] == value == new_access
    assert held["account_uuid"] == "acc-1"
    assert datetime.fromisoformat(held["expires_at"]) == ends
    assert ends > datetime.now(UTC) + timedelta(hours=7)
    assert datetime.fromisoformat(lease["expires_at"]) == ends

    # asking again finds a good token: no second call
    again = {item["name"]: item for item in leased(client, worker, run_id)["leases"]}["claude-oauth"]
    assert again["value"] == new_access and again["id"] == lease["id"] and len(anthropic.asked) == 1


def test_runs_of_one_account_that_lease_together_refresh_once(client, hub, hub_db, anthropic):
    add_account(client, hub, anthropic, timedelta(minutes=1))
    anthropic.delay = 0.5  # the first refresh is still out when the second run asks
    worker = worker_of(client, hub["owner"], "mac-mini", slots=2)
    runs = [step_run(client, hub["owner"], repo, worker) for repo in ("evo-agents", "lumen-kb-docs")]
    with ThreadPoolExecutor(2) as pool:
        answers = list(pool.map(lambda run_id: leased(client, worker, run_id), runs))
    values = {{item["name"]: item for item in answer["leases"]}["claude-oauth"]["value"] for answer in answers}
    assert len(anthropic.asked) == 1
    assert values == {anthropic.last["access_token"]}


def test_a_refresh_the_endpoint_refuses_leaves_the_set_and_leases_no_token_that_has_ended(
    client, hub, hub_db, anthropic, caplog
):
    access, refresh = add_account(client, hub, anthropic, timedelta(minutes=1))
    anthropic.valid.clear()  # the refresh token was revoked
    worker, run_id = run_of(client, hub)
    # still good for a minute: it is leased, as it is
    first = {item["name"]: item for item in leased(client, worker, run_id)["leases"]}["claude-oauth"]
    assert first["value"] == access and len(anthropic.asked) == 1
    held_before = stored(hub_db, client)[0]
    assert held_before["refresh_token"] == refresh

    sql(hub_db, update(tables.secrets).values(expires_at=func.now() - timedelta(minutes=1)))
    answer = leased(client, worker, run_id)
    assert "claude-oauth" not in {item["name"] for item in answer["leases"]}
    assert len(anthropic.asked) == 2
    assert stored(hub_db, client)[0] == held_before
    assert access not in caplog.text and refresh not in caplog.text


def test_an_endpoint_that_is_down_is_no_reason_to_lose_the_set(client, hub, hub_db, anthropic):
    access, refresh = add_account(client, hub, anthropic, timedelta(minutes=1))
    anthropic.down = True
    worker, run_id = run_of(client, hub)
    lease = {item["name"]: item for item in leased(client, worker, run_id)["leases"]}["claude-oauth"]
    assert lease["value"] == access and stored(hub_db, client)[0]["refresh_token"] == refresh
