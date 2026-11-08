"""Signing a Claude account in from the web page (``evo_agents.hub.server.claude_sign_in``): the hub starts a PKCE
sign-in whose verifier only it can open, the member pastes the code claude.ai shows, and the hub trades it for the
token `claude setup-token` makes and keeps it as an account, new or renewed, with no terminal.

The token endpoint is a fake one (``FakeAnthropic`` of test_account_oauth_login). What needs no hub runs without
Postgres; the rest skips without EVO_HUB_TEST_DSN."""

import base64
import hashlib
import logging
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi", reason="the hub server needs the hub-server extra, evo-ak[hub-server]")
pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")

from fastapi import HTTPException

from evo_agents.hub import claude_oauth
from evo_agents.hub.server.claude_sign_in import FLOW_TTL, open_flow, seal_flow
from evo_agents.hub.server.sealing import Sealer, new_key
from tests.hub import pg
from tests.hub.test_account_oauth_login import VAR, account_row, anthropic, sample  # noqa: F401 (a fixture)
from tests.hub.test_secrets_cli import needs_pg

if pg.DSN:
    from tests.hub.test_account_oauth_login import hub  # noqa: F401 (a fixture)
    from tests.hub.test_runs import PROJECT
    from tests.hub.test_secrets_cli import app_key  # noqa: F401 (a fixture of the hub fixture)

START = "/v1/accounts/claude-sign-in"
FINISH = "/v1/accounts/claude-sign-in/finish"
YEAR = claude_oauth.SETUP_TOKEN_SECONDS


def sealer() -> Sealer:
    key = new_key()
    return Sealer(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)))


# Without a hub


def test_the_web_sign_in_asks_claude_ai_for_a_setup_token_that_shows_its_code():
    pkce = claude_oauth.new_pkce()
    url = claude_oauth.setup_token_url(pkce)
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == claude_oauth.WEB_AUTHORIZE_URL
    assert query["redirect_uri"] == [claude_oauth.MANUAL_REDIRECT_URI] and query["code"] == ["true"]
    assert query["scope"] == ["user:inference"] and query["client_id"] == [claude_oauth.CLIENT_ID]
    digest = hashlib.sha256(pkce.verifier.encode()).digest()
    assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
    assert claude_oauth.pkce_of(pkce.verifier, pkce.state) == pkce


def test_a_flow_opens_for_its_member_alone_and_not_once_it_is_old():
    keys = sealer()
    now = datetime.now(UTC)
    pkce = claude_oauth.new_pkce()
    flow = seal_flow(keys, 7, pkce, now)
    assert pkce.verifier not in flow and pkce.state not in flow
    assert open_flow(keys, 7, flow, now + timedelta(minutes=1)) == pkce
    for user_id, at, text in (
        (8, now, flow),  # another member's
        (7, now, flow[:-4] + "AAAA"),  # altered
        (7, now, "not-a-flow"),
        (7, now + FLOW_TTL + timedelta(seconds=1), flow),  # too old
    ):
        with pytest.raises(HTTPException) as refused:
            open_flow(keys, user_id, text, at)
        assert refused.value.status_code == 400
    with pytest.raises(HTTPException, match="not yours"):
        open_flow(sealer(), 7, flow, now)  # sealed under another key


# With a hub


def give_setup_token(anthropic, code: str, email: str | None = "member@example.org") -> str:
    token = sample("sk-ant-oat01-")
    answer: dict = {"access_token": token, "expires_in": YEAR, "token_type": "Bearer"}
    if email:
        answer["account"] = {"uuid": "acc-" + code, "email_address": email}
    anthropic.next[code] = answer
    return token


def start(hub, who: str = "owner") -> tuple[str, str]:
    started = hub.client.post(START, headers=hub.headers[who])
    assert started.status_code == 200, started.text
    body = started.json()
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(body["authorize_url"]).query)["state"][0]
    return body["flow"], state


@needs_pg
def test_a_pasted_code_adds_an_account_and_a_second_sign_in_renews_it(hub, anthropic, caplog):
    caplog.set_level(logging.INFO, logger="evo_agents.hub.server.claude_sign_in")  # its log line is written
    token = give_setup_token(anthropic, "code-one")
    flow, state = start(hub)
    before = datetime.now(UTC)
    added = hub.client.post(
        FINISH,
        json={"flow": flow, "code": f"code-one#{state}", "name": "claude-web", "projects": [PROJECT]},
        headers=hub.headers["owner"],
    )
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["created"] is True and body["email"] == "member@example.org"
    assert body["env_var"] == VAR and body["projects"] == [PROJECT] and body["workers"] == [] and body["pools"] == []
    assert token not in added.text and "code-one" not in added.text
    (asked,) = anthropic.asked
    assert asked["grant_type"] == "authorization_code" and asked["expires_in"] == YEAR
    assert asked["redirect_uri"] == claude_oauth.MANUAL_REDIRECT_URI and asked["state"] == state
    ends = datetime.fromisoformat(body["expires_at"])  # a day before the year ends
    assert (
        before + timedelta(seconds=YEAR) - timedelta(days=1, seconds=5)
        <= ends
        <= datetime.now(UTC) + timedelta(seconds=YEAR) - timedelta(days=1)
    )

    account = hub.client.get("/v1/accounts/claude-web", headers=hub.headers["owner"]).json()
    assert account["auth"] == "token" and account["projects"] == [PROJECT]
    secret_id, _ = account_row(hub, "claude-web")

    # Renewing names the account alone: its projects stay, its id too.
    give_setup_token(anthropic, "code-two", email=None)
    flow, state = start(hub)
    renewed = hub.client.post(
        FINISH, json={"flow": flow, "code": "code-two", "name": "claude-web"}, headers=hub.headers["owner"]
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["created"] is False and renewed.json()["projects"] == [PROJECT]
    assert renewed.json()["email"] is None
    assert account_row(hub, "claude-web")[0] == secret_id
    lines = [record for record in caplog.records if record.getMessage() == "claude account signed in on the web"]
    assert [record.replaced for record in lines] == [False, True]
    assert all(token not in record.getMessage() for record in caplog.records)


@needs_pg
def test_a_refusal_before_the_trade_spends_no_code(hub, anthropic):
    give_setup_token(anthropic, "code-one")
    flow, state = start(hub)

    def finish(**extra):
        body = {"flow": flow, "code": f"code-one#{state}", "name": "claude-web", "projects": [PROJECT]} | extra
        return hub.client.post(FINISH, json=body, headers=hub.headers["owner"])

    assert finish(projects=[]).status_code == 422  # no project for a new account
    assert finish(projects=["nowhere"]).status_code == 404
    assert finish(workers=["a"], pools=["b"]).status_code == 422
    assert finish(code="code-one#OTHER").status_code == 422  # another sign-in's state
    other_flow, _ = start(hub, "other")
    assert finish(flow=other_flow).status_code == 400  # the other member's flow
    git = hub.client.put(
        "/v1/secrets/claude-git",
        json={"kind": "git", "url_prefix": "https://example.org", "value": "x"},
        headers=hub.headers["owner"],
    )
    assert git.status_code == 200, git.text
    assert finish(name="claude-git").status_code == 409  # a secret of another kind keeps its name
    assert anthropic.asked == []
    # The code is still good once the request is right.
    assert finish().status_code == 200


@needs_pg
def test_a_code_the_endpoint_refuses_writes_nothing(hub, anthropic):
    flow, state = start(hub)
    refused = hub.client.post(
        FINISH,
        json={"flow": flow, "code": f"wrong#{state}", "name": "claude-web", "projects": [PROJECT]},
        headers=hub.headers["owner"],
    )
    assert refused.status_code == 502 and "Sign in with Claude again" in refused.json()["message"]
    assert hub.client.get("/v1/accounts/claude-web", headers=hub.headers["owner"]).status_code == 404
