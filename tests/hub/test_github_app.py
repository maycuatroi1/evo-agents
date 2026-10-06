"""The hub's GitHub App (``evo_agents.hub.server.github_app``) against the fake GitHub of ``tests.hub.fake_github``.

The checks step 5 of the worker-credentials plan names: a token covers only the repos asked for, with
``GITHUB_PERMISSIONS`` and nothing more; the App's JWT ends within 10 minutes; a repo the App is not installed on comes
back with a clear reason; and neither a token nor a JWT reaches a log record. Around them: the installation cache,
revocation, GitHub's refusals and outages, and the key the configuration gives. No Postgres is needed."""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("httpx")
pytest.importorskip("cryptography")

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from evo_agents.hub.config import ConfigError, HubConfig
from evo_agents.hub.credentials import GITHUB_PERMISSIONS
from evo_agents.hub.log import JsonFormatter
from evo_agents.hub.server import github_app
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import AppTokens, GitHubApp, Installation
from tests.hub.fake_github import _unb64

OWNER, THEIRS = "maycuatroi1", "hawkteam404"
CONFIG_VARIABLES = ("EVO_HUB_GITHUB_APP_ID", "EVO_HUB_GITHUB_APP_PRIVATE_KEY")


def private_pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def app_key():
    """The App's key pair for this module: the private PEM the hub signs with, the public one the fake checks."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem(key), public


@pytest.fixture
def fake(github, app_key):
    github.app_public_key = app_key[1]
    return github


def config(fake, app_key, **changes) -> HubConfig:
    values = {
        "dsn": "postgresql://hub@db/hub",
        "data_dir": Path("/nonexistent"),
        "github_url": fake.url,
        "github_api_url": fake.url,
        "github_timeout": 2.0,
        "github_app_id": fake.app_id,
        "github_app_private_key": app_key[0],
    }
    return HubConfig(**{**values, **changes})


def with_app(hub_config: HubConfig, body, **options):
    """``await body(app)`` with a GitHubApp of ``hub_config``, closed afterwards."""

    async def main():
        app = GitHubApp(hub_config, **options)
        try:
            return await body(app)
        finally:
            await app.aclose()

    return asyncio.run(main())


def tokens(hub_config: HubConfig, repos) -> AppTokens:
    return with_app(hub_config, lambda app: app.tokens(repos))


def jwt_parts(jwt: str) -> tuple[dict, dict]:
    head, body, _ = jwt.split(".")
    return json.loads(_unb64(head)), json.loads(_unb64(body))


def calls(fake, prefix: str) -> list:
    return [r for r in fake.requests if r.path.startswith(prefix)]


# The token


def test_a_token_covers_only_the_repos_asked_for_with_the_permissions_of_a_run(fake, app_key):
    own = fake.install(OWNER, "evo-agents", "evo-agents-harness", "evo-cli")
    other = fake.install(THEIRS, "m1-identity", "m1-secbox-frontend")
    asked = [(OWNER, "evo-agents"), (OWNER, "evo-agents-harness"), (THEIRS, "m1-identity")]
    before = datetime.now(timezone.utc)
    result = tokens(config(fake, app_key), asked)

    assert result.missing == {}
    by_account = {token.installation.account: token for token in result.tokens}
    assert set(by_account) == {OWNER, THEIRS}, "repos of two GitHub owners get two tokens"
    mine, theirs = by_account[OWNER], by_account[THEIRS]
    assert mine.installation == Installation(own, OWNER) and theirs.installation == Installation(other, THEIRS)
    assert mine.repositories == ("evo-agents", "evo-agents-harness") and theirs.repositories == ("m1-identity",)
    assert mine.name == f"github-app:{OWNER}"

    # What GitHub was asked: each installation for the run's repos on it, with GITHUB_PERMISSIONS and nothing more.
    asked_bodies = {r.path: json.loads(r.body) for r in calls(fake, "/app/installations/")}
    assert asked_bodies == {
        f"/app/installations/{own}/access_tokens": {
            "repositories": ["evo-agents", "evo-agents-harness"],
            "permissions": GITHUB_PERMISSIONS,
        },
        f"/app/installations/{other}/access_tokens": {
            "repositories": ["m1-identity"],
            "permissions": GITHUB_PERMISSIONS,
        },
    }
    assert GITHUB_PERMISSIONS == {"contents": "write", "metadata": "read"}

    # What GitHub made: tokens that open those repos alone, with those permissions, for an hour.
    assert fake.covers(mine.token, OWNER, "evo-agents") and fake.covers(mine.token, OWNER, "evo-agents-harness")
    assert not fake.covers(mine.token, OWNER, "evo-cli"), "a repo of the installation the run does not use"
    assert not fake.covers(mine.token, THEIRS, "m1-identity")
    assert fake.covers(theirs.token, THEIRS, "m1-identity")
    assert not fake.covers(theirs.token, THEIRS, "m1-secbox-frontend")
    for token in (mine, theirs):
        assert token.token.startswith("ghs_")
        assert token.permissions == GITHUB_PERMISSIONS == fake.app_tokens[token.token].permissions
        assert timedelta(minutes=59) < token.expires_at - before <= timedelta(hours=1, seconds=1)
        assert token.token not in repr(token) and token.token not in repr(result)

    # The same installation asked for no repos would give a token for all of them: naming them is what narrows it.
    with_app(config(fake, app_key), lambda app: _whole_installation(app, own))
    (whole,) = [t for t in fake.app_tokens.values() if len(t.repositories) == 3]
    assert whole.repositories == ("evo-agents", "evo-agents-harness", "evo-cli")


async def _whole_installation(app: GitHubApp, installation_id: int) -> None:
    headers = {"Authorization": f"Bearer {app.app_jwt()}"}
    async with httpx.AsyncClient() as client:
        url = f"{app.config.github_api_url}/app/installations/{installation_id}/access_tokens"
        assert (await client.post(url, headers=headers, json={})).status_code == 201


def test_a_repo_the_app_is_not_installed_on_gets_a_clear_reason(fake, app_key):
    fake.install(OWNER, "evo-agents")
    asked = [(OWNER, "evo-agents"), (THEIRS, "m1-identity"), (OWNER, "evo-cli")]
    result = tokens(config(fake, app_key), asked)

    assert result.missing == {
        f"{THEIRS}/m1-identity": f"the GitHub App is not installed on {THEIRS}/m1-identity",
        f"{OWNER}/evo-cli": f"the GitHub App is not installed on {OWNER}/evo-cli",
    }
    assert [token.repositories for token in result.tokens] == [("evo-agents",)]
    assert [json.loads(r.body)["repositories"] for r in calls(fake, "/app/installations/")] == [["evo-agents"]]
    assert with_app(config(fake, app_key), lambda app: app.installation(THEIRS, "m1-identity")) is None


# The JWT


def test_the_app_jwt_is_rs256_under_the_apps_key_and_ends_within_ten_minutes(fake, app_key):
    fake.install(OWNER, "evo-agents")
    before = int(time.time())
    result = tokens(config(fake, app_key), [(OWNER, "evo-agents")])
    after = int(time.time())
    assert result.tokens and not result.missing, "the fake took the JWT: its signature, iss, iat and exp"

    jwts = [r.headers["authorization"].removeprefix("Bearer ") for r in calls(fake, "/repos/") + calls(fake, "/app/")]
    assert len(jwts) == 2 and set(jwts) <= set(fake.app_jwts)
    public = serialization.load_pem_public_key(app_key[1].encode())
    for jwt in jwts:
        head, body, signature = jwt.split(".")
        public.verify(_unb64(signature), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        header, claims = jwt_parts(jwt)
        assert header == {"alg": "RS256", "typ": "JWT"}
        assert claims == {"iat": claims["iat"], "exp": claims["exp"], "iss": int(fake.app_id)}
        assert before - 60 <= claims["iat"] <= after - 60, "issued 60 seconds back"
        assert before + 9 * 60 <= claims["exp"] <= after + 9 * 60, "ends 9 minutes from now"
        assert claims["exp"] - before < 600 and claims["exp"] - claims["iat"] <= 600, "within GitHub's 10 minutes"


def test_github_refusing_the_app_jwt_names_the_hubs_configuration(fake, app_key, monkeypatch):
    fake.install(OWNER, "evo-agents")
    other_key = private_pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    for wrong in (config(fake, app_key, github_app_private_key=other_key), config(fake, app_key, github_app_id="7")):
        result = tokens(wrong, [(OWNER, "evo-agents")])
        assert result.tokens == [] and list(result.missing) == [f"{OWNER}/evo-agents"]
        reason = result.missing[f"{OWNER}/evo-agents"]
        assert reason.startswith("GitHub refused the hub's GitHub App while finding the GitHub App's installation on")
        assert all(name in reason for name in CONFIG_VARIABLES) and "an admin must fix" in reason
    assert calls(fake, "/app/") == [] and fake.app_tokens == {}

    # A JWT ending further than 10 minutes ahead is turned down as GitHub does; the client's ends within them.
    monkeypatch.setattr(github_app, "JWT_LIFETIME_SECONDS", 11 * 60)
    with pytest.raises(GitHubUnavailable, match="refused the hub's GitHub App"):
        with_app(config(fake, app_key), lambda app: app.installation(OWNER, "evo-agents"))
    assert fake.requests[-1].path == f"/repos/{OWNER}/evo-agents/installation"


def test_the_app_comes_from_the_configuration_and_needs_an_rsa_key(fake, app_key):
    assert GitHubApp.from_config(config(fake, app_key, github_app_id=None, github_app_private_key=None)) is None
    with pytest.raises(ValueError, match="EVO_HUB_GITHUB_APP_ID, EVO_HUB_GITHUB_APP_PRIVATE_KEY not set"):
        GitHubApp(config(fake, app_key, github_app_id=None, github_app_private_key=None))

    ec_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    broken = app_key[0].replace(app_key[0].splitlines()[3], "A" * 64)
    for pem in (ec_key.decode(), broken):
        with pytest.raises(ConfigError) as refused:
            GitHubApp.from_config(config(fake, app_key, github_app_private_key=pem))
        assert refused.value.variable == "EVO_HUB_GITHUB_APP_PRIVATE_KEY" and pem not in str(refused.value)

    # iss is the App's ID as a number, or its client ID as given; repr shows neither key nor JWT.
    for app_id, issuer in ((fake.app_id, int(fake.app_id)), ("Iv23liFakeClientId", "Iv23liFakeClientId")):
        app = GitHubApp.from_config(config(fake, app_key, github_app_id=app_id))
        try:
            assert jwt_parts(app.app_jwt())[1]["iss"] == issuer
            assert repr(app) == f"GitHubApp(issuer={issuer!r})"
        finally:
            asyncio.run(app.aclose())


# The installation cache


def test_an_installation_is_kept_ten_minutes_per_repo_and_a_missing_one_is_asked_again(fake, app_key):
    installation = fake.install(OWNER, "evo-agents")
    clock = [1000.0]
    asked = [(OWNER, "evo-agents"), (OWNER, "evo-cli")]

    def lookups(repo: str) -> int:
        return len(calls(fake, f"/repos/{OWNER}/{repo}/installation"))

    async def body(app: GitHubApp):
        first = await app.tokens(asked)
        assert list(first.missing) == [f"{OWNER}/evo-cli"]
        second = await app.tokens([(OWNER, "Evo-Agents"), (OWNER, "evo-cli")])  # owner/repo in any case
        assert [t.repositories for t in second.tokens] == [("Evo-Agents",)] and list(second.missing)
        assert (lookups("evo-agents"), lookups("Evo-Agents"), lookups("evo-cli")) == (1, 0, 2)

        fake.installations[installation].repos.add("evo-cli")  # the owner adds evo-cli to the installation
        third = await app.tokens(asked)
        assert third.missing == {} and [t.repositories for t in third.tokens] == [("evo-agents", "evo-cli")]
        assert (lookups("evo-agents"), lookups("evo-cli")) == (1, 3)

        clock[0] += github_app.INSTALLATION_CACHE_SECONDS + 1
        await app.tokens(asked)
        assert (lookups("evo-agents"), lookups("evo-cli")) == (2, 4)

        # A token GitHub refuses drops what was kept for the installation, so the next ask looks again.
        fake.installations[installation].suspended = True
        refused = await app.tokens(asked)
        assert set(refused.missing) == {f"{OWNER}/evo-agents", f"{OWNER}/evo-cli"}
        assert all("is suspended" in reason for reason in refused.missing.values())
        await app.tokens(asked)
        assert (lookups("evo-agents"), lookups("evo-cli")) == (3, 5)

    with_app(config(fake, app_key), body, clock=lambda: clock[0])


# Revocation


def test_a_token_is_revoked_with_itself(fake, app_key):
    fake.install(OWNER, "evo-agents")

    async def body(app: GitHubApp):
        (token,) = (await app.tokens([(OWNER, "evo-agents")])).tokens
        assert fake.covers(token.token, OWNER, "evo-agents")
        assert await app.revoke(token.token) is True
        assert not fake.covers(token.token, OWNER, "evo-agents") and fake.app_tokens[token.token].revoked
        assert await app.revoke(token.token) is False, "revoked already: GitHub answers 401 and nothing is left to do"
        assert await app.revoke("ghs_" + "0" * 36) is False
        return token.token

    token = with_app(config(fake, app_key), body)
    deletes = calls(fake, "/installation/token")
    assert [r.method for r in deletes] == ["DELETE"] * 3
    assert deletes[0].headers["authorization"] == f"Bearer {token}" and deletes[0].query == {} and not deletes[0].body


# GitHub refusing, failing or not answering


def test_github_refusals_and_outages_become_reasons_and_stop_the_asking(fake, app_key):
    fake.install(OWNER, "evo-agents")
    fake.install(THEIRS, "m1-identity", permissions={"contents": "read", "metadata": "read"})
    result = tokens(config(fake, app_key), [(OWNER, "evo-agents"), (THEIRS, "m1-identity")])
    assert [t.repositories for t in result.tokens] == [("evo-agents",)]
    assert result.missing == {
        f"{THEIRS}/m1-identity": f"GitHub refused a token for {THEIRS}/m1-identity: the App's installation on {THEIRS} "
        "does not cover each of them with contents: write, metadata: read"
    }

    async def refused(app: GitHubApp):
        installation = await app.installation(THEIRS, "m1-identity")
        with pytest.raises(GitHubRefused, match="does not cover each of them"):
            await app.create_token(installation, ["m1-identity"])
        with pytest.raises(GitHubRefused, match=f"no longer installed on {THEIRS}"):
            await app.create_token(Installation(9999, THEIRS), ["m1-identity"])

    with_app(config(fake, app_key), refused)

    # GitHub failing: the first repo gets the reason, the rest the same reason without being asked.
    asked = [(OWNER, "evo-agents"), (THEIRS, "m1-identity"), (OWNER, "evo-cli")]
    fake.requests.clear()
    fake.status = 503
    result = tokens(config(fake, app_key), asked)
    assert result.tokens == [] and len(fake.requests) == 1
    assert set(result.missing.values()) == {
        f"GitHub answered 503 while finding the GitHub App's installation on {OWNER}/evo-agents; try again shortly"
    }
    assert list(result.missing) == [f"{OWNER}/evo-agents", f"{THEIRS}/m1-identity", f"{OWNER}/evo-cli"]

    # GitHub not answering in time.
    fake.requests.clear()
    fake.status, fake.delay = None, 1.5
    started = time.monotonic()
    result = tokens(config(fake, app_key, github_timeout=0.3), asked)
    assert time.monotonic() - started < 1.4 and len(fake.requests) == 1
    assert set(result.missing.values()) == {
        f"GitHub did not answer within 0.3s while finding the GitHub App's installation on {OWNER}/evo-agents; "
        "try again shortly"
    }
    fake.delay = 0

    # A redirect is not followed.
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "https://elsewhere.example.org/"})

    result = tokens_with(config(fake, app_key), asked, transport=httpx.MockTransport(redirect))
    assert set(result.missing.values()) == {
        f"GitHub answered 301 with a redirect while finding the GitHub App's installation on {OWNER}/evo-agents: "
        "check the GitHub URLs"
    }


def tokens_with(hub_config: HubConfig, repos, **options) -> AppTokens:
    return with_app(hub_config, lambda app: app.tokens(repos), **options)


# The log


def test_one_log_line_per_call_and_no_token_or_jwt_in_any(fake, app_key, caplog):
    caplog.set_level(logging.DEBUG)
    fake.install(OWNER, "evo-agents", "evo-agents-harness")
    fake.install(THEIRS, "m1-identity", permissions={"contents": "read", "metadata": "read"})

    async def body(app: GitHubApp):
        result = await app.tokens(
            [(OWNER, "evo-agents"), (OWNER, "evo-agents-harness"), (THEIRS, "m1-identity"), (THEIRS, "m1-kb")]
        )
        for token in result.tokens:
            assert await app.revoke(token.token)
            assert not await app.revoke(token.token)
        return result

    result = with_app(config(fake, app_key), body)
    assert len(result.tokens) == 1 and len(result.missing) == 2
    made = {token.token for token in result.tokens}
    secrets = fake.secrets()
    assert made <= secrets and len(fake.app_jwts) == 6 and set(fake.app_jwts) <= secrets

    records = [r for r in caplog.records if r.name.startswith("evo_agents.")]
    github_calls = [r for r in records if r.getMessage() == "github call"]
    assert [(r.method, r.path) for r in github_calls] == [(r.method, r.path) for r in fake.requests], "one line a call"
    assert [r.status for r in github_calls] == [200, 200, 200, 404, 201, 422, 204, 401]

    formatter = JsonFormatter()
    everything = "\n".join(
        [repr(vars(r)) + r.getMessage() for r in caplog.records]
        + [formatter.format(r) for r in caplog.records]
        + [caplog.text, repr(result)]
    )
    for value in secrets | {app_key[0], *app_key[0].splitlines()[1:-1]}:
        assert value not in everything, "a token, a JWT or the App's key reached a log record"
