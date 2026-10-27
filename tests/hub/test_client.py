"""The credentials of ``evo-agents hub ...`` (``evo_agents.hub.client``): EVO_HUB_TOKEN and EVO_HUB_URL, both set, come
before the login of ~/.evo/hub and of $EVO_HUB_HOME, as the worker daemon sets them for the agent of a run that runs as
its owner (plan owner-run-access, step 2, criterion a1); one of them alone, or a blank one, leaves the login on disk as
it was. The pair never reaches /mcp inside a run, where the run keeps its worker token's scope, and ``hub logout``
leaves the pair and the login on disk alone. No hub runs here: requests go to a stand-in for ``client._send``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.cli import main
from evo_agents.hub import cli_client, mcp_proxy
from evo_agents.hub import client as hub_client
from evo_agents.hub.client import (
    TOKEN_VARIABLE,
    URL_VARIABLE,
    HubError,
    NotSignedIn,
    env_credentials,
    load_credentials,
    saved_credentials,
)

RUN_TOKEN = "evr_" + "R" * 43
MACHINE_TOKEN = "evh_" + "M" * 43
OTHER_TOKEN = "evh_" + "O" * 43
OWNER = "northwind-owner"


def sign_in(directory: Path, url: str, login: str, token: str) -> Path:
    """A login directory as `hub login` leaves it."""
    directory.mkdir(parents=True, mode=0o700)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")
    for name in ("token", "config.json"):
        os.chmod(directory / name, 0o600)
    return directory


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A HOME whose ~/.evo/hub holds the login of another member, on another hub."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sign_in(tmp_path / "home" / ".evo" / "hub", "https://hub.lumen.example", "lumen-dev", MACHINE_TOKEN)
    return tmp_path / "home"


def pair(monkeypatch, token: str = RUN_TOKEN, url: str = "https://hub.northwind.example/") -> None:
    monkeypatch.setenv(TOKEN_VARIABLE, token)
    monkeypatch.setenv(URL_VARIABLE, url)


class StandIn:
    """``client._send`` answering GET /v1/auth/whoami as the hub answers a run's token, noting each request."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, data, timeout):
        self.requests.append((method, url, dict(headers)))
        if url.endswith("/v1/auth/whoami"):
            token = {"id": 12, "kind": "run", "host": "run:320", "expires_at": "2026-10-28T09:00:00+00:00"}
            body = {"login": OWNER, "admin": False, "token": token, "grants": []}
            return 200, json.dumps(body).encode()
        return 404, json.dumps({"error": "not_found", "message": "no such route"}).encode()


def cli(capsys, *args: str) -> SimpleNamespace:
    code = main(list(args))
    out, err = capsys.readouterr()
    return SimpleNamespace(code=code, out=out, err=err)


def test_the_pair_comes_before_the_login_of_home_and_of_evo_hub_home(home, tmp_path, monkeypatch):
    pair(monkeypatch)
    credentials = load_credentials()
    assert (credentials.url, credentials.login, credentials.token) == ("https://hub.northwind.example", "", RUN_TOKEN)
    assert credentials.from_env and credentials.run is None
    assert saved_credentials().token == MACHINE_TOKEN, "the login on disk is still there, untouched"
    other = sign_in(tmp_path / "hub-other", "https://hub.other.example", "other-dev", OTHER_TOKEN)
    monkeypatch.setenv("EVO_HUB_HOME", str(other))
    assert load_credentials().token == RUN_TOKEN, "before $EVO_HUB_HOME too"
    assert saved_credentials().token == OTHER_TOKEN
    assert RUN_TOKEN not in repr(credentials) and TOKEN_VARIABLE in repr(credentials)


@pytest.mark.parametrize(
    ("token", "url"),
    [
        (RUN_TOKEN, None),
        (None, "https://hub.northwind.example"),
        ("  ", "https://hub.northwind.example"),
        (RUN_TOKEN, ""),
    ],
)
def test_one_of_the_pair_or_a_blank_one_leaves_the_login_on_disk(home, monkeypatch, token, url):
    for name, value in ((TOKEN_VARIABLE, token), (URL_VARIABLE, url)):
        if value is not None:
            monkeypatch.setenv(name, value)
    assert env_credentials() is None
    credentials = load_credentials()
    assert (credentials.token, credentials.login, credentials.from_env) == (MACHINE_TOKEN, "lumen-dev", False)


def test_without_the_pair_or_a_login_a_command_is_not_signed_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nobody"))
    with pytest.raises(NotSignedIn):
        load_credentials()
    pair(monkeypatch)
    assert load_credentials().token == RUN_TOKEN, "the pair needs no login on disk at all"


def test_the_pairs_url_must_be_https_or_loopback(home, monkeypatch):
    pair(monkeypatch, url="http://hub.northwind.example")
    with pytest.raises(HubError, match=f"{URL_VARIABLE} must use https"):
        load_credentials()
    pair(monkeypatch, url="http://127.0.0.1:8000")
    assert load_credentials().url == "http://127.0.0.1:8000"


def test_hub_whoami_with_the_pair_sends_its_token_to_its_hub_and_prints_the_owner(home, monkeypatch, capsys):
    stand_in = StandIn()
    monkeypatch.setattr(hub_client, "_send", stand_in)
    pair(monkeypatch)
    shown = cli(capsys, "hub", "whoami")
    assert shown.code == 0, shown.err
    assert shown.out.splitlines()[:2] == [
        f"{OWNER} on https://hub.northwind.example",
        "token 12: run on run:320, expires 2026-10-28 09:00",
    ]
    ((method, url, headers),) = stand_in.requests
    assert (method, url) == ("GET", "https://hub.northwind.example/v1/auth/whoami")
    assert headers["Authorization"] == f"Bearer {RUN_TOKEN}"
    printed = cli(capsys, "hub", "whoami", "--json")
    assert json.loads(printed.out)["url"] == "https://hub.northwind.example"
    assert RUN_TOKEN not in printed.out + shown.out


def test_logout_with_the_pair_touches_neither_the_pair_nor_the_login_on_disk(home, monkeypatch, capsys):
    stand_in = StandIn()
    monkeypatch.setattr(hub_client, "_send", stand_in)
    pair(monkeypatch)
    refused = cli(capsys, "hub", "logout", "--force")
    assert refused.code == 1
    assert f"{TOKEN_VARIABLE} and {URL_VARIABLE}" in refused.err and "unset them" in refused.err
    assert stand_in.requests == [], "nothing was sent: a run's token may not sign out"
    assert (home / ".evo" / "hub" / "token").read_text(encoding="utf-8").strip() == MACHINE_TOKEN


def test_login_with_the_pair_set_says_that_commands_go_on_using_it(home, monkeypatch, capsys):
    monkeypatch.setattr(cli_client, "login", lambda url: {"login": "lumen-dev", "admin": False})
    pair(monkeypatch)
    signed = cli(capsys, "hub", "login", "--url", "https://hub.lumen.example")
    assert signed.code == 0, signed.err
    assert f"note: {TOKEN_VARIABLE} and {URL_VARIABLE} are set" in signed.out


def test_inside_a_run_mcp_never_takes_the_pair_and_outside_it_does(home, tmp_path, monkeypatch):
    pair(monkeypatch)
    monkeypatch.setenv("EVO_WORKER_HOME", str(tmp_path / "no-worker-here"))
    monkeypatch.setenv("EVO_RUN_ID", "320")
    inside = mcp_proxy.session_credentials()
    assert (inside.token, inside.from_env) == (MACHINE_TOKEN, False), "/mcp keeps the run's scope, never its token"
    monkeypatch.delenv("EVO_RUN_ID")
    assert mcp_proxy.session_credentials().token == RUN_TOKEN
