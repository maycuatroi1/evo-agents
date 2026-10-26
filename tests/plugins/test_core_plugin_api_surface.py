"""The surface of the API of core plugins against its golden copy, tests/plugins/golden/core-plugin-api.json, and the
rule of its versions (criterion a11 of the plan external-core-plugins, its golden part): a surface that differs from
the golden copy fails, and says which version API_VERSION must take before the golden copy is written again. Only
adding (a name, a keyword with a default, a field with a default, a key that is not required, a value of a Literal)
takes the next minor version; removing or changing anything takes the next major version, minor 0.

``require`` passes only for the version the hub serves, exactly."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from evo_agents.hub.server import core_plugin_api as hub
from evo_agents.hub.server.core_plugin_api._surface import changes, required_version, surface

GOLDEN = Path(__file__).parent / "golden" / "core-plugin-api.json"
REGENERATE = "python -m evo_agents.hub.server.core_plugin_api surface > tests/plugins/golden/core-plugin-api.json"


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_surface_is_the_golden_copy_or_takes_the_version_the_rule_asks_for():
    kept, now = golden(), surface()
    if now == kept:
        return
    found = changes(kept, now)
    wanted = required_version(kept, now)
    listed = "; ".join(f"{what}: {', '.join(names)}" for what, names in found.items() if names) or "nothing named"
    if tuple(now["api_version"]) != wanted:
        pytest.fail(
            f"the API of core plugins changed ({listed}) and API_VERSION is {tuple(now['api_version'])}: it must be "
            f"{wanted} (docs/plugins.md, The core plugin API, Versions); then write the golden copy again: {REGENERATE}"
        )
    pytest.fail(f"the API of core plugins changed ({listed}) at version {wanted}; write the golden copy: {REGENERATE}")


def test_the_golden_copy_holds_the_version_the_hub_serves():
    assert tuple(golden()["api_version"]) == hub.API_VERSION == (1, 4)


def test_the_command_prints_the_surface_and_the_version():
    run = [sys.executable, "-m", "evo_agents.hub.server.core_plugin_api"]
    printed = subprocess.run([*run, "surface"], capture_output=True, text=True, check=True)
    assert json.loads(printed.stdout) == surface()
    version = subprocess.run([*run, "version"], capture_output=True, text=True, check=True)
    assert version.stdout == "1.4\n"
    wrong = subprocess.run([*run, "help"], capture_output=True, text=True)
    assert wrong.returncode == 2 and "usage:" in wrong.stderr


def test_the_surface_is_the_names_of_all_but_the_version():
    assert set(surface()["names"]) == set(hub.__all__) - {"API_VERSION"}


# The rule


def changed(edit) -> tuple[dict, dict]:
    """The golden surface, and a copy that ``edit`` changed."""
    kept = golden()
    now = copy.deepcopy(kept)
    edit(now["names"])
    return kept, now


def test_adding_a_name_a_keyword_with_a_default_a_field_or_a_literal_value_takes_the_next_minor():
    def added(names):
        names["shiny"] = {"kind": "async function", "params": [], "returns": "None"}

    def keyword(names):
        param = {"name": "silent", "kind": "keyword_only", "annotation": "bool", "default": "False"}
        names["send_message"]["params"].append(param)

    def field(names):
        names["AgentRef"]["fields"].append({"name": "color", "annotation": "str | None", "default": "None"})

    def literal(names):
        by = next(p for p in names["unlink_channel"]["params"] if p["name"] == "by")
        by["annotation"] = "Literal['web', 'bot', 'admin', 'job']"

    for edit in (added, keyword, field, literal):
        assert required_version(*changed(edit)) == (1, 5), edit.__name__


def test_removing_or_changing_anything_takes_the_next_major():
    def removed(names):
        del names["agents"]

    def renamed(names):
        names["send_message"]["params"][-1]["name"] = "reply"

    def required(names):
        names["send_message"]["params"].append(
            {"name": "urgent", "kind": "keyword_only", "annotation": "bool", "default": None}
        )

    def retyped(names):
        names["find_member"]["returns"] = "Member"

    def narrowed(names):
        by = next(p for p in names["unlink_channel"]["params"] if p["name"] == "by")
        by["annotation"] = "Literal['web', 'bot']"

    def field_gone(names):
        names["AgentRef"]["fields"].pop()

    for edit in (removed, renamed, required, retyped, narrowed, field_gone):
        assert required_version(*changed(edit)) == (2, 0), edit.__name__


def test_an_unchanged_surface_keeps_its_version():
    kept = golden()
    assert required_version(kept, copy.deepcopy(kept)) == (1, 4)


# require


def test_require_passes_for_the_version_the_hub_serves_alone():
    hub.require(1, 4)
    for major, minor in ((1, 0), (1, 1), (1, 2), (1, 3), (1, 5), (0, 9), (2, 0)):
        with pytest.raises(hub.UnsupportedApi, match=rf"needs core plugin API {major}\.{minor}; this hub serves 1\.4"):
            hub.require(major, minor)
    with pytest.raises(TypeError):
        hub.require(True, 0)
