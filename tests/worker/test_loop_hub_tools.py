"""The hub's MCP server for the agent of a task run of a plugin's agent type (``evo_agents.worker.runtimes.hub_tools``),
on Claude Code and on Codex: the brainstorm of @strategist and the use case of @ba the Loop hands out call the tools
of the plugins canvas and srs, which a run on Codex had no way to reach (plan loop-engine, criterion a6; the checkpoint
of step 1, answer 7). The adapters are the fakes of ``tests.worker.test_runtimes``; each test's configuration
directories are its own."""

from __future__ import annotations

import asyncio
import dataclasses
import sys

from evo_agents.worker.runtimes import hub_tools
from tests.worker.test_agent_packs import _options
from tests.worker.test_hub_mcp_server import RUN_ENV, SERVER, isolated
from tests.worker.test_runtimes import _claude, _codex, _codex_samples, _collect, _context, _samples, needs

TASK = {"kind": "task"}  # a task run's claim: always of an agent type


def test_loop_task_run_on_claude_code_gets_the_hubs_mcp_server_and_a_step_run_does_not(tmp_path):
    needs("claude_agent_sdk")
    env = isolated(tmp_path)
    assert _options(_claude(tmp_path, _samples, env=env, **TASK)).mcp_servers == {"evo-hub": SERVER}
    assert _options(_claude(tmp_path, _samples, env=env, kind="step")).mcp_servers == {}, "a member's run: as before"


def test_loop_task_run_on_codex_gets_the_hubs_mcp_server_unless_its_config_names_one(tmp_path):
    needs("openai_codex")
    env = isolated(tmp_path)
    adapter = _codex(tmp_path, _codex_samples, env=env, **TASK)
    asyncio.run(_collect(adapter))
    (_, options) = next(entry for entry in adapter.fake.log if entry[0] == "thread/start")
    assert options["config"]["mcp_servers"] == {
        "evo-hub": {"command": sys.executable, "args": ["-I", "-m", "evo_agents", "hub", "mcp"], "env": RUN_ENV}
    }
    (tmp_path / "codex-home" / "config.toml").write_text('[mcp_servers.evo-hub]\ncommand = "evo-agents"\n')
    configured = _codex(tmp_path, _codex_samples, env=env, **TASK)
    asyncio.run(_collect(configured))
    (_, options) = next(entry for entry in configured.fake.log if entry[0] == "thread/start")
    assert "evo-hub" not in options["config"].get("mcp_servers", {})


def test_loop_task_run_in_a_container_gets_no_hub_server(tmp_path):
    from tests.worker.test_environments import container

    context = _context(tmp_path, "codex", env=isolated(tmp_path), **TASK)
    boxed = dataclasses.replace(context, environment=container(tmp_path, {}))
    assert hub_tools.wanted(context) and not hub_tools.wanted(boxed)
    assert hub_tools.codex_servers(boxed, []) == {}
