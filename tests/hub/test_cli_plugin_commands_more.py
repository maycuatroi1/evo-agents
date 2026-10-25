"""What a plugin's command says beyond one route (``evo_agents.hub.cli_plugin_commands``, ``docs/plugins.md``, Commands
of the CLI): ``when`` switches route on an argument, ``values`` splits a flag into fields, ``group`` makes flags exclude
each other, ``confirm`` calls nothing without its switch, ``output.render`` asks the route for its words, and the
core plugins installed with the CLI ship theirs in ``cli.json``, which win over the list kept. No hub, no Postgres.
"""

from __future__ import annotations

import argparse
import json

import pytest

from evo_agents.hub import cli_plugin_commands as cpc
from evo_agents.plugins.manifest import _command_problems

DENYLIST = {
    "name": "denylist",
    "project": True,
    "http": {"method": "GET", "path": "denylist"},
    "args": [
        {"name": "add", "type": "string", "in": "body", "values": ["kind", "value"], "group": "edit"},
        {"name": "remove", "type": "integer", "in": "path", "group": "edit"},
    ],
    "when": [
        {"arg": "add", "http": {"method": "POST", "path": "denylist"}},
        {"arg": "remove", "http": {"method": "DELETE", "path": "denylist/{remove}"}},
    ],
    "output": {"render": "text", "json": "array", "keys": ["id"], "schema": "LumenDeny"},
}
DELETE = {
    "name": "delete",
    "project": True,
    "http": {"method": "DELETE", "path": "contacts/{login}"},
    "args": [{"name": "login", "type": "string", "in": "path", "positional": True, "required": True}],
    "confirm": {"flag": "--yes", "text": "Delete {login} from project {project}? Run again with --yes."},
}


def parser_of(*commands: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    hsub = parser.add_subparsers(dest="hub_command")
    cpc.register_plugin_commands(hsub, [{"id": "lumen", "commands": list(commands)}])
    return parser


def test_plugin_cli_when_picks_the_route_of_the_first_argument_given_and_values_split_a_flag():
    parser = parser_of(DENYLIST)
    listed = parser.parse_args(["lumen", "denylist", "--project", "northwind"])
    assert cpc.request_of("lumen", DENYLIST, vars(listed)) == ("GET", "/v1/x/lumen/northwind/denylist", None)
    added = parser.parse_args(["lumen", "denylist", "--project", "northwind", "--add", "domain", "lumen.example"])
    assert cpc.request_of("lumen", DENYLIST, vars(added), "text") == (
        "POST",
        "/v1/x/lumen/northwind/denylist?render=text",
        {"kind": "domain", "value": "lumen.example"},
    )
    removed = parser.parse_args(["lumen", "denylist", "--project", "northwind", "--remove", "7"])
    assert cpc.request_of("lumen", DENYLIST, vars(removed)) == ("DELETE", "/v1/x/lumen/northwind/denylist/7", None)
    with pytest.raises(SystemExit):
        parser.parse_args(["lumen", "denylist", "--project", "northwind", "--add", "a", "b", "--remove", "7"])
    assert listed.json_output.to_json() == {"kind": "array", "keys": ["id"], "schema": "LumenDeny"}


def test_plugin_cli_confirm_calls_nothing_without_its_switch(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cpc, "signed_in", lambda: called.append(1))
    args = parser_of(DELETE).parse_args(["lumen", "delete", "--project", "northwind", "ada"])
    assert args.func(args) == 2 and called == []
    assert capsys.readouterr().err.strip() == "Delete ada from project northwind? Run again with --yes."


def test_plugin_cli_render_text_prints_the_routes_words_and_json_the_answer(monkeypatch, capsys):
    asked = []

    class Hub:
        def call(self, method, path, body=None):
            asked.append(path)
            return {"text": "Denied domain lumen.example"} if "render=text" in path else [{"id": 1}]

    monkeypatch.setattr(cpc, "signed_in", lambda: (Hub(), None))
    parser = parser_of(DENYLIST)
    args = parser.parse_args(["lumen", "denylist", "--project", "northwind", "--add", "domain", "lumen.example"])
    assert args.func(args) == 0
    assert capsys.readouterr().out == "Denied domain lumen.example\n"
    args = parser.parse_args(["lumen", "denylist", "--project", "northwind", "--json"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out) == [{"id": 1}]
    assert asked == ["/v1/x/lumen/northwind/denylist?render=text", "/v1/x/lumen/northwind/denylist"]


def test_plugin_cli_core_plugins_ship_their_commands_and_win_over_the_list_kept(monkeypatch, tmp_path):
    shipped = {plugin["id"]: plugin for plugin in cpc.bundled()}
    assert {"crm", "pm", "marketing"} <= set(shipped)
    assert {c["name"] for c in shipped["crm"]["commands"]} == {"list", "show", "delete"}
    monkeypatch.setenv("EVO_HUB_HOME", str(tmp_path))
    held = [{"id": "crm", "commands": [{"name": "old"}]}, {"id": "lumen", "commands": [{"name": "list"}]}]
    (tmp_path / cpc.CACHE_FILE).write_text(json.dumps({"plugins": held}), encoding="utf-8")
    known = {plugin["id"]: plugin for plugin in cpc.known_plugins()}
    assert known["crm"]["commands"] == shipped["crm"]["commands"] and "lumen" in known


def test_plugin_cli_manifest_checks_the_routes_of_when_and_the_flag_of_confirm():
    assert _command_problems("c", DENYLIST) == []
    assert _command_problems("c", DELETE) == []
    wrong = {**DENYLIST, "when": [{"arg": "nobody", "http": {"method": "DELETE", "path": "denylist/{gone}"}}]}
    found = _command_problems("c", wrong)
    assert any("{gone}" in line for line in found) and any("'nobody'" in line for line in found)
    assert any("'remove' is not {remove}" in line for line in found)
    clash = {**DELETE, "args": [*DELETE["args"], {"name": "yes", "type": "boolean", "in": "query"}]}
    assert any("confirm.flag" in line for line in _command_problems("c", clash))
