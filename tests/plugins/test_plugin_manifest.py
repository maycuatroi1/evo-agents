"""The manifest of a plugin, evo-plugin.json, against evo_agents/harness/schemas/plugin.schema.json (seam plugin-v1):
the schema ships beside the harness schemas and uses only what ``evo_agents.schema`` checks, a full manifest reads
with its defaults, and each wrong manifest is refused with every problem and where it is. When jsonschema is
installed, it agrees with ``evo_agents.schema`` on every sample."""

from __future__ import annotations

import json
import re

import pytest

from evo_agents.plugins.manifest import (
    EXTENSION_POINTS,
    MANIFEST_FILE,
    MAX_MANIFEST,
    PERMISSIONS,
    SCHEMA_FILE,
    TIERS,
    ManifestError,
    read_manifest,
    schema,
)
from tests.plugins.samples import manifest

# What evo_agents.schema implements (its module docstring), with the annotations it reads past.
CHECKED = {
    "type",
    "enum",
    "const",
    "pattern",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "anyOf",
    "oneOf",
    "$ref",
}
ANNOTATIONS = {"$schema", "$id", "$defs", "title", "description"}


def keywords(node, found: set[str]) -> set[str]:
    """Every keyword of a schema node and the nodes under it."""
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            if key in ("properties", "$defs"):
                for child in value.values():
                    keywords(child, found)
            elif key in ("items", "additionalProperties", "anyOf", "oneOf"):
                keywords(value, found)
    elif isinstance(node, list):
        for child in node:
            keywords(child, found)
    return found


def test_plugin_manifest_schema_ships_with_the_harness_schemas_and_uses_what_the_validator_checks():
    assert SCHEMA_FILE.name == "plugin.schema.json" and SCHEMA_FILE.parent.name == "schemas"
    assert SCHEMA_FILE.parent.parent.name == "harness"  # package-data harness/schemas/*.json ships it
    loaded = schema()
    assert loaded["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert sorted(keywords(loaded, set()) - CHECKED - ANNOTATIONS) == []
    assert loaded["properties"]["permissions"]["items"]["enum"] == list(PERMISSIONS)
    assert loaded["properties"]["tier"]["enum"] == list(TIERS)
    assert list(loaded["$defs"]["contributes"]["properties"]) == list(EXTENSION_POINTS)


def test_plugin_manifest_reads_with_its_defaults():
    read = read_manifest(json.dumps(manifest()).encode())
    assert (read.id, read.version, read.publisher, read.tier, read.plugin_api) == (
        "sandbox",
        "1.0.0",
        "acme",
        "isolated",
        1,
    )
    assert read.engines == {"evo-agents": ">=0.16"} and read.permissions == ("runs:read", "chats:write")
    assert read.contributions("routes") == ({"name": "hello", "title": "Hello"},) and read.contributions("pages") == ()
    assert read.config["required"] == ["greeting"]
    bare = read_manifest(manifest(name=None, description=None, permissions=None, config=None, contributes=None))
    assert (bare.name, bare.description, bare.permissions, bare.config, bare.contributes) == (
        "sandbox",
        "",
        (),
        None,
        {},
    )
    core = read_manifest(manifest(tier="core", contributes={"runtimes": [{"name": "acp", "python": "pkg.mod:Acp"}]}))
    assert core.python_refs("runtimes") == {"acp": "pkg.mod:Acp"} and core.python_refs("routes") == {}


BAD = [
    (manifest(id=None), ["id: is required"]),
    (manifest(id="Sandbox"), ["id:"]),
    (manifest(version="1.0"), ["version:"]),
    (manifest(publisher="Acme Inc"), ["publisher:"]),
    (manifest(plugin_api=0), ["plugin_api:"]),
    (manifest(plugin_api="1"), ["plugin_api:"]),
    (manifest(engines={}), ["engines.evo-agents: is required"]),
    (manifest(tier="trusted"), ["tier:"]),
    (manifest(permissions=["secrets:read"]), ["permissions[0]:"]),
    (manifest(extra=True), ["extra"]),
    (manifest(contributes={"widgets": [{"name": "w"}]}), ["widgets"]),
    (manifest(contributes={"routes": [{"title": "no name"}]}), ["contributes.routes[0].name: is required"]),
    (manifest(config={"type": "array"}), ["config.type:"]),
]
SEMANTIC = [
    (manifest(engines={"evo-agents": "at least 0.16"}), ["engines.evo-agents: 'at least 0.16' is not a clause"]),
    (manifest(engines={"evo-agents": ">=0.16.*"}), ["engines.evo-agents: >=0.16.*: a wildcard goes with == or !="]),
    (
        manifest(contributes={"routes": [{"name": "hello"}, {"name": "hello"}]}),
        ["contributes.routes: 'hello' is contributed twice"],
    ),
    (
        manifest(contributes={"runtimes": [{"name": "acp", "python": "pkg.mod:Acp"}]}),
        ["contributes.runtimes[0].python: only a core plugin runs Python"],
    ),
    (manifest(config={"type": "object", "required": ["missing"]}), ["config.required: 'missing' is not one of"]),
    (manifest(permissions=["runs:read", "runs:read"]), ["permissions: 'runs:read' is listed twice"]),
]


@pytest.mark.parametrize(("data", "problems"), BAD + SEMANTIC)
def test_plugin_manifest_refuses_each_wrong_manifest_saying_where(data, problems):
    with pytest.raises(ManifestError) as refused:
        read_manifest(data)
    for problem in problems:
        assert any(problem in found for found in refused.value.problems), refused.value.problems
    assert str(refused.value) == "; ".join(refused.value.problems)


def test_plugin_manifest_lists_every_problem_of_a_manifest_at_once():
    with pytest.raises(ManifestError) as refused:
        read_manifest(manifest(id="X", version="one", plugin_api=0))
    assert [problem.split(":")[0] for problem in refused.value.problems] == ["id", "version", "plugin_api"]


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        (b"{", f"{MANIFEST_FILE} is not JSON"),
        (b"\xff\xfe", f"{MANIFEST_FILE} is not JSON"),
        (b"[1, 2]", f"{MANIFEST_FILE} holds list, not an object"),
        (b" " * (MAX_MANIFEST + 1), f"{MANIFEST_FILE} is over 256 KiB"),
    ],
)
def test_plugin_manifest_that_is_not_an_object_of_json_is_refused(source, problem):
    with pytest.raises(ManifestError, match=problem):
        read_manifest(source)


@pytest.mark.parametrize(("data", "problems"), [(manifest(), [])] + BAD)
def test_plugin_manifest_schema_agrees_with_jsonschema(data, problems):
    jsonschema = pytest.importorskip("jsonschema")
    found = list(jsonschema.Draft202012Validator(schema()).iter_errors(data))
    assert bool(found) == bool(problems), [error.message for error in found]


# The extension points: their versions, the tables of a core plugin, and the keys of each point


def test_plugin_points_each_have_a_version_and_the_schema_names_the_kinds_of_events():
    from evo_agents.plugins import compat
    from evo_agents.plugins.protocol import EVENT_KINDS

    assert EXTENSION_POINTS == tuple(compat.POINT_VERSIONS) and all(compat.POINT_VERSIONS.values())
    defs = schema()["$defs"]
    assert defs["event"]["properties"]["on"]["items"]["enum"] == list(EVENT_KINDS)
    assert all("api" in defs[point]["properties"] for point in ("contribution", "route", "mcp_tool", "job", "event"))
    later = manifest(contributes={"jobs": [{"name": "tick", "api": 2}]})
    assert "version 2 of that extension point" in compat.problem(later["engines"], 1, contributes=later["contributes"])


def test_plugin_points_core_tables_carry_the_prefix_and_routes_name_a_prefix_only_in_core():
    from evo_agents.plugins.manifest import is_per_project, table_prefix

    core = manifest(id="srs-docs", tier="core", permissions=None, config=None, contributes={})
    owned = read_manifest({**core, "contributes": {"tables": [{"name": "srs_docs_items"}]}})
    assert table_prefix("srs-docs") == "srs_docs_" and owned.tables() == ("srs_docs_items",)
    assert not owned.per_project()  # tables alone: nothing a project enables
    cases = [
        ({**core, "contributes": {"tables": [{"name": "docs"}]}}, "must start with srs_docs_"),
        ({**core, "tier": "isolated", "contributes": {"tables": [{"name": "srs_docs_x"}]}}, "only a core plugin"),
        (manifest(contributes={"routes": [{"name": "hello", "prefix": "/v1"}]}), "only a core plugin's route"),
        ({**core, "contributes": {"channels": [{"name": "web", "python": "m:C"}]}}, "kind of members' channel"),
    ]
    for data, problem in cases:
        with pytest.raises(ManifestError, match=problem):
            read_manifest(data)
    kept = read_manifest({**core, "contributes": {"routes": [{"name": "me", "python": "m:r", "prefix": ""}]}})
    assert not kept.per_project()
    assert read_manifest({**core, "contributes": {"routes": [{"name": "docs", "python": "m:r"}]}}).per_project()
    # the same rule on a manifest as the hub's store keeps it, without reading it again
    for data in (owned.data, kept.data, {**core, "permissions": ["runs:read"]}, manifest()):
        assert is_per_project(data) == read_manifest(data).per_project()


def test_plugin_pages_isolated_name_an_html_entry_under_pages_and_core_ones_are_native():
    board = {"name": "board", "title": "Board", "icon": "kanban", "entry": "pages/board.html"}
    framed = read_manifest(manifest(contributes={"pages": [board]}))
    assert framed.pages() == (board,) and framed.per_project()
    core = manifest(id="srs", tier="core", permissions=None, config=None, contributes={})
    native = read_manifest({**core, "contributes": {"pages": [{"name": "docs", "title": "SRS", "menu": False}]}})
    assert native.pages()[0]["name"] == "docs" and native.per_project()  # a page alone: a project enables it
    cases = [
        (manifest(contributes={"pages": [{"name": "board"}]}), "names its entry, an HTML file of its bundle"),
        ({**core, "contributes": {"pages": [board]}}, "a core plugin's page is native"),
        (manifest(contributes={"pages": [{**board, "entry": "pages/a/../../x.html"}]}), "without empty, . or .."),
        (manifest(contributes={"pages": [{**board, "entry": "web/board.html"}]}), "does not match"),
        (manifest(contributes={"pages": [{**board, "icon": "rocket"}]}), "must be one of"),
        (manifest(contributes={"pages": [{**board, "title": "x" * 61}]}), "longer than"),
    ]
    for data, problem in cases:
        with pytest.raises(ManifestError, match=problem):
            read_manifest(data)


def test_plugin_agent_types_name_the_plugins_own_parts_each_once_and_their_handles_once():
    triager = {
        "name": "triager",
        "title": "Issue triager",
        "profile": {"handle": "triager", "name": "Triager", "instructions": "Fix what the task names."},
        "jobs": ["tick"],
        "events": ["results"],
    }
    parts = {"jobs": [{"name": "tick"}], "events": [{"name": "results", "on": ["task.result"]}]}
    read = read_manifest(manifest(permissions=["runs:dispatch"], contributes={**parts, "agent_types": [triager]}))
    assert read.contributions("agent_types") == (triager,) and read.per_project()
    other = {**triager, "name": "other", "jobs": [], "events": []}
    cases = [
        ({**triager, "jobs": ["nightly"]}, "agent_types[0].jobs: 'nightly' is not one of the plugin's jobs"),
        ({**triager, "events": ["results", "results"]}, "agent_types[0].events: 'results' is named twice"),
        ({**triager, "packs": ["triage"]}, "agent_types[0].packs: 'triage' is not one of the plugin's agent_packs"),
        ({**triager, "pages": ["board"]}, "agent_types[0].pages: 'board' is not one of the plugin's pages"),
        ({**triager, "environment": "box"}, "agent_types[0].environment: 'box' is not one of the plugin's environ"),
        ({**triager, "profile": {"handle": "Triager"}}, "agent_types[0].profile.handle"),
        ({**triager, "profile": {"name": "Triager"}}, "agent_types[0].profile.handle: is required"),
        ({**triager, "model": "large"}, "model"),
    ]
    for item, problem in cases:
        with pytest.raises(ManifestError, match=re.escape(problem)):
            read_manifest(manifest(contributes={**parts, "agent_types": [item]}))
    with pytest.raises(ManifestError, match="two agent types have the handle 'triager'"):
        read_manifest(manifest(contributes={**parts, "agent_types": [triager, other]}))
