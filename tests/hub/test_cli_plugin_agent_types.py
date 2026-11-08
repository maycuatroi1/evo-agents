"""``evo-agents hub plugin agent-type list|enable|disable`` and ``hub plugin enable --records-level``
(``evo_agents.hub.cli_plugin_agent_types``, ``cli_plugin_projects``): the routes they call, the bodies they send and
what they print, against a hub that answers in this process. No hub, no Postgres."""

from __future__ import annotations

import json

import pytest

from evo_agents.hub import cli_plugin_agent_types, cli_plugin_projects

TRIAGER = {
    "plugin": "lumen-qa",
    "name": "qa",
    "title": "QA agent",
    "description": "Runs the cases",
    "handle": "qa-northwind",
    "environment": None,
    "packs": [],
    "enabled": True,
    "run_as": "ada",
    "daily_usd": 20.0,
    "spent_usd": 1.5,
    "enabled_by": "ada",
    "enabled_at": "2026-11-08T08:00:00Z",
    "disabled_at": None,
}
PLUGIN = {
    "name": "lumen-qa",
    "version": "1.0.0",
    "publisher": "lumen",
    "enabled": True,
    "pending": [],
    "records_level": None,
    "permissions": [{"name": "records:use", "text": "keep records", "granted": True}],
}


class FakeHub:
    def __init__(self):
        self.calls: list[tuple] = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/agent-types"):
            return [TRIAGER, {**TRIAGER, "name": "dev", "handle": "dev", "enabled": False, "run_as": None}]
        if path.endswith("/plugins/lumen-qa"):
            return PLUGIN
        if path.endswith("/enable"):
            return {**PLUGIN, "records_level": (body or {}).get("records_level")}
        if method == "DELETE":
            return {**TRIAGER, "enabled": False, "disabled_at": "2026-11-08T09:00:00Z"}
        return {**TRIAGER, **(body or {})}


@pytest.fixture
def fake_hub(monkeypatch):
    hub = FakeHub()
    monkeypatch.setattr(cli_plugin_agent_types, "signed_in", lambda: (hub, None))
    monkeypatch.setattr(cli_plugin_projects, "signed_in", lambda: (hub, None))
    return hub


def run_cli(*argv) -> int:
    from evo_agents.cli import main

    return main(["hub", "plugin", *argv])


def test_agent_type_list_enable_and_disable_call_the_routes_of_the_project(fake_hub, capsys):
    assert run_cli("agent-type", "list", "--project", "northwind") == 0
    printed = capsys.readouterr().out
    assert "lumen-qa  qa    @qa-northwind  enabled" in printed and "$20.00" in printed and "not enabled" in printed
    assert run_cli("agent-type", "list", "--project", "northwind", "--json") == 0
    assert [item["name"] for item in json.loads(capsys.readouterr().out)] == ["qa", "dev"]
    argv = ("agent-type", "enable", "lumen-qa", "qa", "--project", "northwind", "--run-as", "@ada", "--daily-usd", "20")
    assert run_cli(*argv, "--handle", "qa-northwind") == 0
    assert "as @qa-northwind: its runs go under ada, at most $20.00 in 24 hours" in capsys.readouterr().out
    assert run_cli("agent-type", "disable", "lumen-qa", "qa", "--project", "northwind") == 0
    assert "Turned agent type qa of plugin lumen-qa off for project northwind" in capsys.readouterr().out
    assert fake_hub.calls == [
        ("GET", "/v1/projects/northwind/agent-types", None),
        ("GET", "/v1/projects/northwind/agent-types", None),
        (
            "PUT",
            "/v1/projects/northwind/agent-types/lumen-qa/qa",
            {"run_as": "ada", "daily_usd": 20.0, "handle": "qa-northwind"},
        ),
        ("DELETE", "/v1/projects/northwind/agent-types/lumen-qa/qa", None),
    ]
    with pytest.raises(SystemExit):
        run_cli("agent-type", "enable", "lumen-qa", "qa", "--project", "northwind", "--daily-usd", "20")


def test_plugin_enable_sends_the_records_level_even_when_everything_is_granted(fake_hub, capsys):
    assert run_cli("enable", "lumen-qa", "--project", "northwind", "--yes") == 0
    assert "enabled for project northwind already" in capsys.readouterr().out
    assert run_cli("enable", "lumen-qa", "--project", "northwind", "--yes", "--records-level", "internal") == 0
    assert "its jobs and events reaching records up to internal" in capsys.readouterr().out
    assert fake_hub.calls[-1] == (
        "POST",
        "/v1/projects/northwind/plugins/lumen-qa/enable",
        {"permissions": ["records:use"], "records_level": "internal"},
    )
    assert [method for method, _, _ in fake_hub.calls] == ["GET", "GET", "POST"]
