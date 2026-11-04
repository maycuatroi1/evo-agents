"""``hub kg profile``: the profile out of kg_status, and what it says when the hub withholds it."""

import json
from types import SimpleNamespace

from evo_agents.hub import kg_profile_cli as cli


class FakeHub:
    def __init__(self, found):
        self.found, self.calls = found, []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.found


def status(coverage, ready=True):
    return {"structuredContent": {"graph": {"ready": ready, "build_id": 7, "coverage": coverage}}}


def test_it_reads_the_profile_from_kg_status():
    hub = FakeHub(status({"profile": {"items": 3}}))
    assert cli.read_profile(hub, "my proj") == (7, {"items": 3}, None)
    assert hub.calls == [("POST", "/v1/kg/my%20proj/tools/kg_status", {"arguments": {}})]


def test_it_says_why_there_is_none():
    assert cli.read_profile(FakeHub(status({"sources": []})), "p")[2].startswith("the newest build holds no profile")
    assert "no ready graph" in cli.read_profile(FakeHub(status({}, ready=False)), "p")[2]
    refused = {"isError": True, "content": [{"text": "error: no grant"}]}
    assert cli.read_profile(FakeHub(refused), "p") == (None, None, "no grant")


def test_the_command_prints_json(monkeypatch, capsys):
    hub = FakeHub(status({"profile": {"items": 3}}))
    monkeypatch.setattr(cli, "signed_in", lambda: (hub, None))
    args = SimpleNamespace(project="p", sink=None, json=True)
    assert cli.cmd_profile(args) == 0
    assert json.loads(capsys.readouterr().out) == {"project": "p", "build_id": 7, "profile": {"items": 3}}
    monkeypatch.setattr(cli, "signed_in", lambda: (FakeHub(status({})), None))
    assert cli.cmd_profile(args) == 1
