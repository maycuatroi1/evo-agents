"""The runtimes a plugin declares (``evo_agents.plugins.runtimes``): an ACP agent's command, its models and the secrets
its agent needs, read from the manifest, registered as <plugin>/<name>, and sent to a worker as a declaration it reads
back whole or refuses."""

from __future__ import annotations

import pytest

from evo_agents.plugins.manifest import ManifestError, is_per_project, read_manifest
from evo_agents.plugins.runtimes import (
    AcpRuntime,
    acp_runtimes,
    is_plugin_runtime,
    plugin_of,
    runtime_name,
)
from tests.plugins.samples import manifest

FAKE = {
    "name": "fake",
    "title": "Fake ACP agent",
    "acp": {"command": ["fake-acp", "--stdio"]},
    "models": ["small", "large"],
    "secrets": ["FAKE_API_KEY"],
}


def test_plugin_runtime_of_acp_reads_and_registers_as_plugin_slash_name():
    read = read_manifest(manifest(version="0.2.0", contributes={"runtimes": [FAKE, {"name": "bare", **_acp("bare")}]}))
    found = acp_runtimes(read)
    assert found == [
        AcpRuntime(
            runtime="sandbox/fake",
            plugin="sandbox",
            version="0.2.0",
            title="Fake ACP agent",
            command=("fake-acp", "--stdio"),
            models=("small", "large"),
            secrets=("FAKE_API_KEY",),
        ),
        AcpRuntime("sandbox/bare", "sandbox", "0.2.0", "sandbox/bare", ("bare",)),
    ]
    assert found[0].program == "fake-acp"
    assert AcpRuntime.from_json(found[0].to_json()) == found[0]


def _acp(program: str) -> dict:
    return {"acp": {"command": [program]}}


def test_plugin_runtime_names_are_namespaced_by_the_plugin():
    assert runtime_name("sandbox", "fake") == "sandbox/fake"
    assert is_plugin_runtime("sandbox/fake") and is_plugin_runtime("srs-docs/gemini_cli")
    for name in ("claude-code", "any", "sandbox/", "/fake", "Sandbox/fake", "a/b/c", None, 3):
        assert not is_plugin_runtime(name), name
    assert plugin_of("sandbox/fake") == "sandbox" and plugin_of("codex") is None


def test_plugin_runtime_of_a_core_plugin_with_acp_is_enabled_per_project():
    core_acp = manifest(tier="core", permissions=None, config=None, contributes={"runtimes": [FAKE]})
    core_python = manifest(tier="core", permissions=None, config=None, contributes={"runtimes": [_python()]})
    assert is_per_project(read_manifest(core_acp).data)
    assert not is_per_project(read_manifest(core_python).data)
    assert acp_runtimes(read_manifest(core_python)) == []


def _python() -> dict:
    return {"name": "codex", "python": "pkg.adapters:Codex"}


@pytest.mark.parametrize(
    ("item", "problem"),
    [
        ({"name": "fake"}, "contributes.runtimes[0]: a runtime names one of acp"),
        ({**FAKE, "python": "pkg.mod:Acp"}, "contributes.runtimes[0]: a runtime names one of acp"),
        ({**FAKE, "acp": {"command": []}}, "contributes.runtimes[0].acp.command:"),
        ({**FAKE, "acp": {"command": ["x"] * 33}}, "contributes.runtimes[0].acp.command: at most 32 items"),
        ({**FAKE, "acp": {"command": ["x"], "cwd": "/"}}, "contributes.runtimes[0].acp"),
        ({**FAKE, "acp": {"command": ["fake\nacp"]}}, "contributes.runtimes[0].acp.command[0]:"),
        ({**FAKE, "models": ["m"] * 201}, "contributes.runtimes[0].models: at most 200 items"),
        ({**FAKE, "models": ["small", "small"]}, "contributes.runtimes[0].models: 'small' is listed twice"),
        ({**FAKE, "secrets": ["api_key"]}, "contributes.runtimes[0].secrets[0]:"),
        ({**FAKE, "secrets": [f"K{i}" for i in range(21)]}, "contributes.runtimes[0].secrets: at most 20 items"),
    ],
)
def test_plugin_runtime_that_is_not_one_is_refused_saying_where(item, problem):
    with pytest.raises(ManifestError) as refused:
        read_manifest(manifest(tier="core", contributes={"runtimes": [item]}))
    assert any(problem in found for found in refused.value.problems), refused.value.problems


@pytest.mark.parametrize(
    "change",
    [
        {"runtime": "claude-code"},
        {"plugin": "other"},
        {"version": None},
        {"command": []},
        {"command": "fake-acp"},
        {"command": ["fake\nacp"]},
        {"models": ["m"] * 201},
        {"secrets": ["lower"]},
        {"title": "x" * 201},
    ],
)
def test_plugin_runtime_declaration_the_worker_cannot_read_is_refused(change):
    data = {**AcpRuntime("sandbox/fake", "sandbox", "0.2.0", "Fake", ("fake-acp",)).to_json(), **change}
    with pytest.raises(ValueError):
        AcpRuntime.from_json(data)
    with pytest.raises(ValueError):
        AcpRuntime.from_json(["sandbox/fake"])
