"""``evo-agents hub market`` and ``hub plugin install --github`` / ``hub plugin publisher`` (``evo_agents.hub.cli_market``)
against a hub that answers from memory: what each command asks the hub and what it prints. The routes themselves are
tests/hub/test_market.py."""

from __future__ import annotations

import io
import json

import pytest

from evo_agents.cli import main
from evo_agents.hub.client import HubError

NOTES = {
    "source": 1,
    "market": "Northwind market",
    "id": "lumen-notes",
    "name": "Lumen notes",
    "description": "Meeting notes for a project.",
    "industries": ["education"],
    "categories": ["documents"],
    "repo": "lumen-labs/lumen-notes",
    "publisher": {
        "id": "lumen",
        "name": "Lumen Labs",
        "signer": "sigstore:lumen-labs/lumen-notes/.github/workflows/ci.yml",
        "identity": "sigstore:lumen-labs/lumen-notes/.github/workflows/ci.yml",
    },
    "tier": "isolated",
    "package": None,
    "price": {"kind": "free", "usd_per_hub_month": None, "seller": None},
    "icon": None,
    "homepage": None,
    "license": None,
    "requires": None,
    "status": "not_installed",
    "installed_version": None,
    "latest": {
        "tag": "v1.0.0",
        "version": "1.0.0",
        "permissions": [{"name": "runs:read", "text": "read runs"}],
        "error": None,
    },
}
LISTED = {"plugins": [NOTES], "industries": ["education"], "categories": ["documents"], "markets": []}
PLUGIN = {"name": "lumen-notes", "publisher": "lumen", "installed_version": "1.0.0"}


class Turns(list):
    """Answers to give one after the other."""


class Hub:
    """A hub answering ``answers[(method, path)]``: an answer, a HubError to raise, or ``Turns`` of them in turn."""

    url = "https://hub.northwind.test"

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple[str, str, dict | None]] = []

    def call(self, method: str, path: str, body: dict | None = None, **_):
        self.calls.append((method, path, body))
        answer = self.answers[(method, path)]
        if isinstance(answer, Turns):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def hub(monkeypatch):
    def use(answers: dict) -> Hub:
        made = Hub(answers)
        monkeypatch.setattr("evo_agents.hub.cli_market.signed_in", lambda: (made, None))
        return made

    return use


def untrusted() -> HubError:
    return HubError(
        "untrusted publisher: this hub does not trust it; nothing was installed", status=409, code="conflict"
    )


def test_market_list_filters_and_prints_status(hub, capsys):
    found = hub({("GET", "/v1/market?q=notes&industry=education"): LISTED})
    assert main(["hub", "market", "list", "--search", "notes", "--industry", "education"]) == 0
    out = capsys.readouterr().out
    assert "lumen-notes" in out and "not installed" in out and "free" in out
    assert found.calls == [("GET", "/v1/market?q=notes&industry=education", None)]


def test_market_show_finds_the_only_source_and_prints_the_permissions(hub, capsys):
    detail = {**NOTES, "trusted": False, "may_trust": True}
    hub({("GET", "/v1/market"): LISTED, ("GET", "/v1/market/1/lumen-notes"): detail})
    assert main(["hub", "market", "show", "lumen-notes"]) == 0
    out = capsys.readouterr().out
    assert "not trusted on this hub yet" in out and "asks to runs:read: read runs" in out


def test_market_install_with_trust_and_without_a_terminal(hub, capsys, monkeypatch):
    done = {"plugin": PLUGIN, "trusted_publisher": True, "upgraded": False}
    path = "/v1/market/1/lumen-notes/install"
    found = hub({(("POST", path)): Turns([done])})
    assert main(["hub", "market", "install", "lumen-notes", "--source", "1", "--trust"]) == 0
    assert found.calls == [("POST", path, {"version": None, "trust_publisher": True})]
    assert "trusts the signer of publisher lumen" in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hub({("POST", path): Turns([untrusted()])})
    assert main(["hub", "market", "install", "lumen-notes", "--source", "1"]) != 0
    assert "pass --trust" in capsys.readouterr().err


def test_market_install_asks_at_a_terminal(hub, capsys, monkeypatch):
    done = {"plugin": PLUGIN, "trusted_publisher": True, "upgraded": False}
    path = "/v1/market/1/lumen-notes/install"
    terminal = io.StringIO("")
    terminal.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", terminal)
    monkeypatch.setattr("builtins.input", lambda question: "y")
    found = hub({("POST", path): Turns([untrusted(), done])})
    assert main(["hub", "market", "install", "lumen-notes", "--source", "1"]) == 0
    assert [body["trust_publisher"] for _, _, body in found.calls] == [False, True]


def test_plugin_install_from_github(hub, capsys):
    done = {"plugin": PLUGIN, "trusted_publisher": False, "upgraded": True}
    found = hub({("POST", "/v1/plugins/install-github"): done})
    assert main(["hub", "plugin", "install", "--github", "lumen-labs/lumen-notes@v1.0.0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == done
    body = {"repo": "lumen-labs/lumen-notes", "tag": "v1.0.0", "signer": None, "trust_publisher": False}
    assert found.calls == [("POST", "/v1/plugins/install-github", body)]
    assert main(["hub", "plugin", "install", "lumen-notes", "--github", "lumen-labs/lumen-notes"]) != 0
    assert "not both" in capsys.readouterr().err


def test_market_source_add_reads_the_token_from_stdin(hub, capsys, monkeypatch):
    source = {"id": 2, "repo": "northwind/private", "plugins": 1, "error": None}
    found = hub({("POST", "/v1/market/sources"): source})
    monkeypatch.setattr("sys.stdin", io.StringIO("gho_northwind_test\n"))
    assert main(["hub", "market", "source", "add", "northwind/private", "--token-stdin"]) == 0
    body = {"repo": "northwind/private", "ref": "main", "path": "market.json", "token": "gho_northwind_test"}
    assert found.calls == [("POST", "/v1/market/sources", body)]
    assert "gho_northwind_test" not in capsys.readouterr().out


def test_market_request_and_publisher_commands(hub, capsys):
    asked = {"id": 4, "project": "demo", "plugin": "lumen-notes", "state": "open", "created": True}
    publishers = [{"id": "lumen", "identities": [], "core": False, "operator": False, "trusted": []}]
    found = hub(
        {
            ("GET", "/v1/market"): LISTED,
            ("POST", "/v1/market/1/lumen-notes/request"): asked,
            ("POST", "/v1/plugin-publishers"): publishers,
            ("DELETE", "/v1/plugin-publishers/3"): publishers,
        }
    )
    assert main(["hub", "market", "request", "lumen-notes", "--project", "demo"]) == 0
    assert main(["hub", "plugin", "publisher", "add", "lumen", NOTES["publisher"]["signer"]]) == 0
    assert main(["hub", "plugin", "publisher", "remove", "3"]) == 0
    assert [call[:2] for call in found.calls][1:] == [
        ("POST", "/v1/market/1/lumen-notes/request"),
        ("POST", "/v1/plugin-publishers"),
        ("DELETE", "/v1/plugin-publishers/3"),
    ]
