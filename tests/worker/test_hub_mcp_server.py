"""The hub's MCP server for the agent of a run of the Curator (``evo_agents.worker.runtimes.hub_tools``), on Claude Code
and Codex, whatever the worker's machine has configured: finding #65 of evo-agents and proposal #28, criterion a4 of
plan history-driven-curator. The adapters are the fakes of ``tests.worker.test_runtimes``; each test's configuration
directories are its own, so the machine running the tests counts for nothing."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys

from evo_agents.worker.runtimes import hub_tools
from tests.worker.test_agent_packs import _options, built, lean_files, with_packs
from tests.worker.test_agent_profiles import profile
from tests.worker.test_runtimes import _claude, _codex, _codex_samples, _collect, _context, _samples, needs

REVIEW = {"curator": {"role": "reviewer", "protected_paths": []}, "kind": "review"}
RUN_ENV = {"EVO_RUN_ID": "41", "EVO_WORKER_HOME": "/worker-home", "EVO_RUN_PROJECT": "demo"}
SERVER = {
    "type": "stdio",
    "command": sys.executable,
    "args": ["-I", "-m", "evo_agents", "hub", "mcp"],
    "env": RUN_ENV,
}


def isolated(tmp_path, **extra) -> dict:
    """An environment whose Claude Code and Codex configuration are the test's own, empty, with the run's variables."""
    claude, codex = tmp_path / "claude-config", tmp_path / "codex-home"
    home = tmp_path / "home"
    for directory in (claude, codex, home):
        directory.mkdir(parents=True, exist_ok=True)
    return {"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex), "HOME": str(home), **RUN_ENV, **extra}


def test_a_curator_run_on_claude_code_gets_the_hubs_mcp_server_and_another_run_does_not(tmp_path):
    needs("claude_agent_sdk")
    env = isolated(tmp_path)
    options = _options(_claude(tmp_path, _samples, env=env, **REVIEW))
    assert options.mcp_servers == {"evo-hub": SERVER}
    for role in ("builder", "judge"):
        run = {"curator": {"role": role}, "kind": "plan" if role == "builder" else "judge"}
        assert _options(_claude(tmp_path, _samples, env=env, **run)).mcp_servers == {"evo-hub": SERVER}
    for role in ("replay", "history"):  # their agents call none of the hub's tools
        assert _options(_claude(tmp_path, _samples, env=env, curator={"role": role})).mcp_servers == {}
    assert _options(_claude(tmp_path, _samples, env=env)).mcp_servers == {}, "a member's run: as before"


def test_claude_code_keeps_the_hubs_server_of_the_owners_own_and_the_packs_servers(tmp_path):
    needs("claude_agent_sdk")
    env = isolated(tmp_path)
    config = tmp_path / "claude-config"
    (config / "settings.json").write_text(json.dumps({"enabledPlugins": {"evo-hub@evo-agents": True}}))
    (config / "plugins").mkdir()
    installed = {"plugins": {"evo-hub@evo-agents": [{"installPath": str(tmp_path)}]}}
    (config / "plugins" / "installed_plugins.json").write_text(json.dumps(installed))
    assert _options(_claude(tmp_path, _samples, env=env, **REVIEW)).mcp_servers == {}, "the evo-hub plugin has one"

    other = isolated(tmp_path / "by-hand")
    path = tmp_path / "by-hand" / "claude-config" / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {"evo-hub": {"command": "evo-agents", "args": ["hub", "mcp"]}}}))
    assert _options(_claude(tmp_path, _samples, env=other, **REVIEW)).mcp_servers == {}, "one declared by hand"

    found = built(tmp_path, lean_files())
    packed = _options(with_packs(_claude(tmp_path, _samples, env=isolated(tmp_path / "packs"), **REVIEW), found))
    assert sorted(packed.mcp_servers) == ["docs", "evo-hub", "lint"]


def test_a_profile_that_allows_no_hub_server_gets_none_and_one_that_names_it_does(tmp_path):
    needs("claude_agent_sdk")
    env = isolated(tmp_path)
    limited = profile(tool=["read", "edit"], model=["opus"])
    options = _options(_claude(tmp_path, _samples, env=env, profile=limited, model="opus", **REVIEW))
    assert options.mcp_servers == {}
    named = profile(tool=["read", "edit", "mcp:evo-hub"], model=["opus"])
    options = _options(_claude(tmp_path, _samples, env=env, profile=named, model="opus", **REVIEW))
    assert options.mcp_servers == {"evo-hub": SERVER}


def test_a_curator_run_on_codex_gets_the_hubs_mcp_server_unless_its_config_names_one(tmp_path):
    needs("openai_codex")
    env = isolated(tmp_path)
    adapter = _codex(tmp_path, _codex_samples, env=env, **REVIEW)
    asyncio.run(_collect(adapter))
    (_, options) = next(entry for entry in adapter.fake.log if entry[0] == "thread/start")
    assert options["config"]["mcp_servers"] == {
        "evo-hub": {"command": sys.executable, "args": ["-I", "-m", "evo_agents", "hub", "mcp"], "env": RUN_ENV}
    }
    plain = _codex(tmp_path, _codex_samples, env=env)
    asyncio.run(_collect(plain))
    (_, options) = next(entry for entry in plain.fake.log if entry[0] == "thread/start")
    assert "mcp_servers" not in options["config"]

    (tmp_path / "codex-home" / "config.toml").write_text('[mcp_servers.evo-hub]\ncommand = "evo-agents"\n')
    configured = _codex(tmp_path, _codex_samples, env=env, **REVIEW)
    asyncio.run(_collect(configured))
    (_, options) = next(entry for entry in configured.fake.log if entry[0] == "thread/start")
    assert "evo-hub" not in options["config"].get("mcp_servers", {})


def test_the_hubs_server_stays_off_where_the_agent_runs_in_a_container(tmp_path):
    from tests.worker.test_environments import container

    context = _context(tmp_path, "claude-code", env=isolated(tmp_path), **REVIEW)
    boxed = dataclasses.replace(context, environment=container(tmp_path, {}))
    assert hub_tools.wanted(context) and not hub_tools.wanted(boxed)
    assert hub_tools.claude_servers(boxed) == {} and hub_tools.codex_servers(boxed, []) == {}
