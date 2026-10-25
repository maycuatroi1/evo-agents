"""``evo-agents hub account oauth-login``: a Claude account from Claude's OAuth sign-in, added or renewed in one command,
and the hub keeping its tokens sealed.

The checks the claude-oauth-login plan names for its first step: the command builds the authorization URL of a PKCE
flow, takes the code the member pastes (a terminal and a browser answer on localhost instead), trades it at the token
endpoint (here a fake one) for an access token, a refresh token and a lifetime, and writes them with PUT
/v1/secrets/{name}; the account shows as OAuth, apart from an account of a pasted token. Running it again for a name
that exists replaces the tokens alone: same secret id, so weight, priority and runs stay. The hub keeps the set sealed,
for its account alone, and no response, listing, row dump or output shows a token.

What needs no hub runs without Postgres; everything that needs one skips without EVO_HUB_TEST_DSN."""

import base64
import hashlib
import io
import json
import secrets
import threading
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import pytest

from evo_agents.hub import claude_oauth
from evo_agents.hub.claude_oauth import OAuthError, parse_code, tokens_from
from evo_agents.hub.contract import commands
from tests.hub import pg
from tests.hub.test_run_cli import as_json
from tests.hub.test_secrets_cli import needs_pg, nowhere, run_cli

VAR = "CLAUDE_CODE_OAUTH_TOKEN"
LIFETIME = 28800


def sample(prefix: str) -> str:
    return prefix + secrets.token_hex(24)


# Without a hub


def test_the_authorization_url_is_a_pkce_request_for_the_claude_code_client():
    pkce = claude_oauth.new_pkce()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(claude_oauth.authorize_url(pkce)).query)
    assert urllib.parse.urlsplit(claude_oauth.authorize_url(pkce)).netloc == "claude.ai"
    assert query["client_id"] == [claude_oauth.CLIENT_ID] and query["response_type"] == ["code"]
    assert query["redirect_uri"] == ["http://localhost:54545/callback"] and query["state"] == [pkce.state]
    assert query["code_challenge_method"] == ["S256"] and "user:inference" in query["scope"][0]
    digest = hashlib.sha256(pkce.verifier.encode()).digest()
    assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
    assert claude_oauth.new_pkce().verifier != pkce.verifier


@pytest.mark.parametrize(
    "pasted",
    ["abc123", " abc123\n", "abc123#STATE", "http://localhost:54545/callback?code=abc123&state=STATE"],
    ids=["bare", "spaces", "code#state", "callback address"],
)
def test_a_pasted_answer_gives_its_code(pasted):
    assert parse_code(pasted, "STATE") == "abc123"


@pytest.mark.parametrize(
    ("pasted", "problem"),
    [
        ("", "no code"),
        ("abc123#OTHER", "not this sign-in's"),
        ("http://localhost:54545/callback?code=abc&state=OTHER", "not this sign-in's"),
        ("http://localhost:54545/callback?error=access_denied", "no authorization code"),
        ("two words", "no authorization code"),
    ],
)
def test_a_pasted_answer_of_another_sign_in_or_without_a_code_is_refused(pasted, problem):
    with pytest.raises(OAuthError, match=problem):
        parse_code(pasted, "STATE")


def test_the_token_endpoints_answer_becomes_tokens_that_do_not_show_in_repr():
    now = datetime(2026, 10, 20, tzinfo=UTC)
    access, refresh = sample("sk-ant-oat01-"), sample("sk-ant-ort01-")
    answer = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": LIFETIME,
        "account": {"uuid": "acc-1", "email_address": "me@example.org"},
        "organization": {"uuid": "org-1"},
    }
    tokens = tokens_from(answer, now)
    assert (tokens.access_token, tokens.refresh_token) == (access, refresh)
    assert tokens.expires_at == now + timedelta(seconds=LIFETIME)
    assert (tokens.account_uuid, tokens.organization_uuid, tokens.email) == ("acc-1", "org-1", "me@example.org")
    assert access not in repr(tokens) and refresh not in repr(tokens)
    for broken in ({}, {**answer, "refresh_token": ""}, {**answer, "expires_in": 0}, {**answer, "expires_in": "8h"}):
        with pytest.raises(OAuthError):
            tokens_from(broken, now)


def free_port() -> int:
    with HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as probe:
        return probe.server_address[1]


def test_the_browsers_answer_on_localhost_gives_the_code():
    port = free_port()
    seen = {}

    def browser():
        for _ in range(100):
            try:
                url = f"http://127.0.0.1:{port}/callback?code=abc123&state=STATE"
                with urllib.request.urlopen(url, timeout=5) as page:  # noqa: S310
                    seen["page"] = page.read().decode()
                return
            except OSError:
                threading.Event().wait(0.05)

    thread = threading.Thread(target=browser)
    thread.start()
    assert claude_oauth.wait_for_callback("STATE", timeout=10, port=port) == "abc123"
    thread.join()
    assert "close this tab" in seen["page"]


def test_an_answer_of_another_state_or_none_in_time_is_refused():
    port = free_port()

    def browser():
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=abc&state=OTHER", timeout=5).read()  # noqa: S310
                return
            except OSError:
                threading.Event().wait(0.05)

    thread = threading.Thread(target=browser)
    thread.start()
    with pytest.raises(OAuthError, match="not this sign-in's"):
        claude_oauth.wait_for_callback("STATE", timeout=10, port=port)
    thread.join()
    with pytest.raises(OAuthError, match="no answer"):
        claude_oauth.wait_for_callback("STATE", timeout=0.3, port=free_port())


def test_a_port_that_is_taken_says_the_code_can_be_pasted():
    with HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as taken:
        with pytest.raises(claude_oauth.ListenError, match="cannot listen"):
            claude_oauth.wait_for_callback("STATE", timeout=0.2, port=taken.server_address[1])


def test_the_command_is_in_the_contract_with_the_keys_of_an_account():
    command = commands()["hub account oauth-login"]
    assert [entry["name"] for entry in command["positionals"]] == ["name"]
    flags = {option["flags"][0] for option in command["options"]}
    assert flags == {"--project", "--worker", "--pool", "--no-browser", "--paste", "--json"}
    assert "auth" in command["json"]["keys"] and command["json"]["schema"] == "Account"


# Against a hub

if pg.DSN:
    import httpx
    from sqlalchemy import select

    from evo_agents.hub import tables
    from evo_agents.hub.server.account_oauth import oauth_aad, open_tokens
    from evo_agents.hub.server.app import create_app
    from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable
    from tests.hub import test_credentials_api as leases_api
    from tests.hub.live import sql
    from tests.hub.test_credentials_api import OTHER, OWNER
    from tests.hub.test_run_cli import write_credentials
    from tests.hub.test_run_stream import serving
    from tests.hub.test_runs import PROJECT
    from tests.hub.test_secrets_cli import app_key  # noqa: F401 (a fixture of this module)


class FakeAnthropic(ThreadingMixIn, HTTPServer):
    """The token endpoint: it gives the tokens set in ``next`` for each code and keeps what it was asked."""

    daemon_threads = True

    def __init__(self):
        outer = self
        self.asked: list[dict] = []
        self.next: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.asked.append(body)
                answer = outer.next.get(body["code"])
                data = json.dumps(answer or {"error": "invalid_grant"}).encode()
                self.send_response(200 if answer else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1/oauth/token"

    def give(self, code: str) -> tuple[str, str]:
        access, refresh = sample("sk-ant-oat01-"), sample("sk-ant-ort01-")
        self.next[code] = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": LIFETIME,
            "account": {"uuid": "acc-" + code, "email_address": "member@example.org"},
            "organization": {"uuid": "org-" + code},
        }
        return access, refresh


@pytest.fixture
def anthropic(monkeypatch):
    server = FakeAnthropic()
    monkeypatch.setenv("EVO_CLAUDE_OAUTH_TOKEN_URL", server.url)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def hub(hub_db, tmp_path, github, app_key):  # noqa: F811
    """The hub of test_secrets_cli, with the sealer it holds."""
    config = leases_api.hub_config(hub_db, tmp_path, github, app_key)
    with serving(create_app(config)) as url, httpx.Client(base_url=url, timeout=10) as client:
        headers = leases_api.members(client, github)
        homes = {
            who: write_credentials(tmp_path / who, url, login, headers[who]["Authorization"].removeprefix("Bearer "))
            for who, login in (("owner", OWNER), ("other", OTHER))
        }
        sealer = Sealer(config.secrets_key)
        yield type(
            "Hub",
            (),
            {"url": url, "client": client, "headers": headers, "homes": homes, "db": hub_db, "sealer": sealer},
        )


def oauth_login(hub, monkeypatch, capsys, code: str, *args: str):
    """``hub account oauth-login`` as the owner, the member pasting ``code`` (a pipe, not a terminal)."""
    return run_cli(
        monkeypatch,
        capsys,
        hub.homes["owner"],
        "hub",
        "account",
        "oauth-login",
        *args,
        stdin=io.StringIO(code + "\n"),
    )


def account_row(hub, name: str):
    s, a = tables.secrets, tables.accounts
    ((secret_id,),) = sql(hub.db, select(s.c.id).where(s.c.name == name, s.c.deleted_at.is_(None)))
    row = sql(
        hub.db,
        select(a.c.weight, a.c.priority, a.c.oauth_sealed, a.c.oauth_nonce, a.c.oauth_key_id).where(
            a.c.secret_id == secret_id
        ),
    )
    return secret_id, row


def sealed_set(hub, secret_id: int) -> dict:
    a = tables.accounts
    ((ciphertext, nonce, key_id),) = sql(
        hub.db, select(a.c.oauth_sealed, a.c.oauth_nonce, a.c.oauth_key_id).where(a.c.secret_id == secret_id)
    )
    return open_tokens(hub.sealer, secret_id, Sealed(bytes(ciphertext), bytes(nonce), key_id))


@needs_pg
def test_oauth_login_adds_an_account_that_shows_as_oauth_and_renews_it_keeping_the_account(
    hub, anthropic, monkeypatch, capsys
):
    access1, refresh1 = anthropic.give("code-one")
    access2, refresh2 = anthropic.give("code-two")
    before = datetime.now(UTC)

    added = oauth_login(hub, monkeypatch, capsys, "code-one", "claude-o", "--project", PROJECT)
    assert added.code == 0, added.err
    nowhere(added, access1, refresh1, "code-one")
    assert "Created account claude-o (OAuth)" in added.out and "Signed in with OAuth" in added.out
    # the authorization URL asked for was printed, and the code was traded with the verifier of its challenge
    url = next(line for line in added.err.splitlines() if line.startswith("https://claude.ai/oauth/authorize"))
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    (asked,) = anthropic.asked
    assert asked["grant_type"] == "authorization_code" and asked["code"] == "code-one"
    digest = hashlib.sha256(asked["code_verifier"].encode()).digest()
    assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
    assert asked["state"] == query["state"][0]

    shown = as_json(run_cli(monkeypatch, capsys, hub.homes["owner"], "hub", "account", "show", "claude-o", "--json"))
    assert shown["auth"] == "oauth" and shown["env_var"] == VAR and shown["projects"] == [PROJECT]
    ends = datetime.fromisoformat(shown["expires_at"])
    assert before + timedelta(seconds=LIFETIME - 5) <= ends <= datetime.now(UTC) + timedelta(seconds=LIFETIME)
    listed = run_cli(monkeypatch, capsys, hub.homes["owner"], "hub", "account", "list")
    assert "AUTH" in listed.out and "oauth" in listed.out
    assert "renew" not in listed.out  # the end of an access token is not a reminder
    nowhere(listed, access1, refresh1)

    secret_id, row = account_row(hub, "claude-o")
    assert row[0][2] is not None
    first = sealed_set(hub, secret_id)
    assert first["access_token"] == access1 and first["refresh_token"] == refresh1
    assert (first["account_uuid"], first["organization_uuid"]) == ("acc-code-one", "org-code-one")
    assert datetime.fromisoformat(first["expires_at"]) == ends

    # the member tunes the account, then renews it: the tokens change, the account stays
    tuned = run_cli(monkeypatch, capsys, hub.homes["owner"], "hub", "account", "set", "claude-o", "--weight", "7")
    assert tuned.code == 0, tuned.err
    renewed = oauth_login(hub, monkeypatch, capsys, "code-two#" + query["state"][0], "claude-o")
    assert renewed.code == 2 and "not this sign-in's" in renewed.err  # an answer of another sign-in's state
    assert account_row(hub, "claude-o")[0] == secret_id
    renewed = oauth_login(hub, monkeypatch, capsys, "code-two", "claude-o")
    assert renewed.code == 0, renewed.err
    assert "Replaced the tokens of account claude-o" in renewed.out
    nowhere(renewed, access2, refresh2)
    same_id, row = account_row(hub, "claude-o")
    assert same_id == secret_id and row[0][0] == 7
    second = sealed_set(hub, secret_id)
    assert (second["access_token"], second["refresh_token"]) == (access2, refresh2)
    # the projects it had stayed, with no flag to say them again
    kept = as_json(run_cli(monkeypatch, capsys, hub.homes["owner"], "hub", "account", "show", "claude-o", "--json"))
    assert kept["projects"] == [PROJECT] and kept["weight"] == 7 and kept["auth"] == "oauth"


@needs_pg
def test_a_refused_code_or_a_missing_code_writes_nothing(hub, anthropic, monkeypatch, capsys):
    refused = oauth_login(hub, monkeypatch, capsys, "wrong-code", "claude-o", "--project", PROJECT)
    assert refused.code == 2 and "the token endpoint refused the code (HTTP 400)" in refused.err
    empty = oauth_login(hub, monkeypatch, capsys, "", "claude-o", "--project", PROJECT)
    assert empty.code == 2 and "no code" in empty.err
    missing = oauth_login(hub, monkeypatch, capsys, "x", "claude-o")
    assert missing.code == 2 and "a new account needs --project" in missing.err
    assert as_json(run_cli(monkeypatch, capsys, hub.homes["owner"], "hub", "account", "list", "--json")) == []


@needs_pg
def test_the_sealed_set_is_in_no_response_and_no_row_of_the_database_in_the_clear(hub, anthropic, monkeypatch, capsys):
    access, refresh = anthropic.give("code-seal")
    assert oauth_login(hub, monkeypatch, capsys, "code-seal", "claude-o", "--project", PROJECT).code == 0
    owner = hub.headers["owner"]
    for path in ("/v1/accounts", "/v1/accounts/claude-o", "/v1/secrets"):
        body = hub.client.get(path, headers=owner).text
        assert access not in body and refresh not in body and "refresh_token" not in body and "oauth_sealed" not in body

    # no row of the tables that hold it carries a token in the clear, nor in hex or base64
    secret_id, _ = account_row(hub, "claude-o")
    rows, raw = "", b""
    for table in (tables.accounts, tables.secrets, tables.audit):
        for row in sql(hub.db, select(table)):
            rows += repr(row)
            raw += b"".join(bytes(cell) for cell in row if isinstance(cell, bytes | memoryview))
    for value in (access, refresh):
        assert value not in rows and value.encode() not in raw
        assert value.encode().hex() not in rows
        assert base64.b64encode(value.encode()).decode() not in rows
    assert raw  # the sealed set is there

    # it opens as this account's and nowhere else
    a = tables.accounts
    ((ciphertext, nonce, key_id),) = sql(
        hub.db, select(a.c.oauth_sealed, a.c.oauth_nonce, a.c.oauth_key_id).where(a.c.secret_id == secret_id)
    )
    sealed = Sealed(bytes(ciphertext), bytes(nonce), key_id)
    assert json.loads(hub.sealer.open(sealed, oauth_aad(secret_id)))["refresh_token"] == refresh
    with pytest.raises(Unsealable):
        hub.sealer.open(sealed, oauth_aad(secret_id + 1))
    # the member's other hub members see none of it
    assert hub.client.get("/v1/accounts", headers=hub.headers["other"]).json() == []


@needs_pg
def test_a_secret_written_whole_again_or_deleted_drops_the_sealed_oauth_set(hub, anthropic, monkeypatch, capsys):
    anthropic.give("code-a")
    assert oauth_login(hub, monkeypatch, capsys, "code-a", "claude-o", "--project", PROJECT).code == 0
    secret_id, row = account_row(hub, "claude-o")
    assert row[0][2] is not None
    owner = hub.headers["owner"]
    put = hub.client.put(
        "/v1/secrets/claude-o",
        headers=owner,
        json={"kind": "env", "env_var": VAR, "projects": [PROJECT], "value": sample("sk-ant-oat01-")},
    )
    assert put.status_code == 200, put.text
    assert (
        as_json(run_cli(monkeypatch, capsys, hub.homes["owner"], "hub", "account", "show", "claude-o", "--json"))[
            "auth"
        ]
        == "token"
    )
    assert account_row(hub, "claude-o") == (secret_id, [(row[0][0], row[0][1], None, None, None)])

    anthropic.give("code-b")
    assert oauth_login(hub, monkeypatch, capsys, "code-b", "claude-o").code == 0
    assert account_row(hub, "claude-o")[1][0][2] is not None
    assert hub.client.delete("/v1/secrets/claude-o", headers=owner).status_code == 204
    a = tables.accounts
    assert sql(hub.db, select(a.c.oauth_sealed).where(a.c.secret_id == secret_id)) == [(None,)]


@needs_pg
@pytest.mark.parametrize(
    "secret",
    [
        {"kind": "env", "env_var": "OTHER_TOKEN"},
        {"kind": "git", "url_prefix": "https://gitlab.example.org/ops"},
    ],
    ids=["another variable", "a git secret"],
)
def test_oauth_belongs_to_the_claude_variable_alone(hub, secret):
    oauth = {"refresh_token": sample("r"), "access_expires_at": (datetime.now(UTC) + timedelta(hours=8)).isoformat()}
    put = hub.client.put(
        "/v1/secrets/nope",
        headers=hub.headers["owner"],
        json={**secret, "projects": [PROJECT], "value": sample("v"), "oauth": oauth},
    )
    assert put.status_code == 422 and "oauth belongs" in put.text
    assert oauth["refresh_token"] not in put.text


@needs_pg
def test_expires_at_of_an_oauth_secret_is_its_access_tokens_end_or_left_out(hub):
    ends = datetime.now(UTC) + timedelta(hours=8)
    oauth = {"refresh_token": sample("r"), "access_expires_at": ends.isoformat()}
    body = {"kind": "env", "env_var": VAR, "projects": [PROJECT], "value": sample("v"), "oauth": oauth}
    other = hub.client.put(
        "/v1/secrets/o1",
        headers=hub.headers["owner"],
        json={**body, "expires_at": (ends + timedelta(days=1)).isoformat()},
    )
    assert other.status_code == 422 and "end of its access token" in other.text
    assert hub.client.put("/v1/secrets/o1", headers=hub.headers["owner"], json=body).status_code == 200
    assert (
        datetime.fromisoformat(hub.client.get("/v1/secrets", headers=hub.headers["owner"]).json()[0]["expires_at"])
        == ends
    )
    past = {**oauth, "access_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    assert (
        hub.client.put("/v1/secrets/o1", headers=hub.headers["owner"], json={**body, "oauth": past}).status_code == 422
    )
