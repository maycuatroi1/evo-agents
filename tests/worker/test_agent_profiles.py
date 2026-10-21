"""Agent profiles on the worker (``evo_agents.worker.runtimes.profiles``): what each runtime's adapter makes of a run's
profile, against the fakes of ``tests.worker.test_runtimes``, and what the daemon does with a run whose adapter cannot
apply its profile, with the daemon in this process, the fake runtime and the in-memory hub of
``tests.worker.test_plan_runs``.

The checks step 3 of the agent-profiles-group-chat plan names (criterion a4): on Claude Code, tools outside the
allowlist (built-in and MCP) are not in the session, only allowed skills load, the model is an allowed one and the
instructions follow the system prompt's note; opencode and Codex apply what they can, and a run whose profile limits
what the adapter cannot apply fails before its agent starts, saying why; a profile that allows everything starts the
agent with the same settings as a run without one (a11). What the runtimes themselves did with these settings was
checked against fakes of their model APIs (the module's docstring of ``profiles``).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from evo_agents.hub import runs
from evo_agents.worker import adapter as adapter_module
from evo_agents.worker.adapter import Adapter, ProfileRefused
from evo_agents.worker.runtimes import claude_code as claude_module
from evo_agents.worker.runtimes import codex as codex_module
from evo_agents.worker.runtimes import common, profiles
from evo_agents.worker.runtimes import opencode as opencode_module
from evo_agents.worker.runtimes.claude_code import ClaudeCodeAdapter
from evo_agents.worker.runtimes.codex import CodexAdapter
from evo_agents.worker.runtimes.opencode import OpencodeAdapter
from tests.worker.test_runtimes import (
    MODEL,
    OC_SESSION,
    PROMPT,
    THREAD,
    _claude,
    _codex,
    _codex_samples,
    _collect,
    _opencode,
    _opencode_samples,
    _samples,
    needs,
)

AXES = ("credential", "tool", "skill", "worker", "project", "model", "runtime")
ALL = {axis: {"mode": "all"} for axis in AXES}
INSTRUCTIONS = "Answer in Vietnamese. Never push to main."


def profile(handle: str = "reviewer", *, instructions: str = "", version: int = 3, **allow) -> dict:
    """A profile as the claim carries it (``RunSpec.profile``): every axis all, but those of ``allow``."""
    permissions = {axis: {"mode": "all"} for axis in AXES}
    for axis, items in allow.items():
        permissions[axis] = {"mode": "allowlist", "allow": list(items)}
    return {
        "handle": handle,
        "version": version,
        "name": handle.title(),
        "instructions": instructions,
        "permissions": permissions,
    }


DEFAULT = profile("demo", version=1)  # a project's default profile: every permission, no instructions


def _command(options) -> list[str]:
    """The flags claude-agent-sdk gives the CLI for ``options``."""
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    return SubprocessCLITransport(prompt="", options=options)._build_command()


def _launch(tmp_path, **run) -> tuple[list[str], dict, object]:
    """The launch of Claude Code for a run: the CLI's flags, its environment, and the options."""
    adapter = _claude(tmp_path, _samples, **run)
    adapter._session = "11111111-2222-3333-4444-555555555555"  # each adapter makes its own
    options = adapter.build_options("/opt/bin/claude")
    return _command(options), dict(options.env), options


# A profile that allows everything changes nothing


def test_an_all_access_profile_launches_claude_code_exactly_as_a_run_without_a_profile(tmp_path):
    needs("claude_agent_sdk")
    for run in ({}, {"model": "opus", "effort": "high"}, {"kind": "author"}, {"budget": {"max_usd": 5}}):
        without, env_without, options_without = _launch(tmp_path, **run)
        under, env_under, options_under = _launch(tmp_path, profile=DEFAULT, **run)
        assert under == without and env_under == env_without
        assert options_under.hooks is None and options_under.tools is None and options_under.skills is None
    assert "--tools" not in without and "--setting-sources" not in " ".join(without)
    assert ClaudeCodeAdapter.profile_refusal({"profile": DEFAULT}) is None
    context = _claude(tmp_path, _samples, profile=DEFAULT).context
    assert profiles.profile_of(context) is None and ClaudeCodeAdapter.environment_notes(context) == []


def test_an_all_access_profile_starts_opencode_and_codex_exactly_as_a_run_without_a_profile(tmp_path):
    needs("aiohttp")
    needs("openai_codex")
    seen = []
    owner = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "CODEX_HOME": str(tmp_path / "codex-home")}
    for run in ({"model": MODEL}, {"model": MODEL, "profile": DEFAULT}):
        adapter = _opencode(tmp_path, _opencode_samples, env=owner, **run)
        asyncio.run(_collect(adapter))
        seen.append((adapter.fake.requests, adapter.server_env()))
    assert seen[0] == seen[1]
    assert seen[1][0][0][2]["permission"] == opencode_module.RUN_RULES and ("mcp",) not in seen[1][0]
    assert json.loads(seen[1][1]["OPENCODE_CONFIG_CONTENT"]) == {"compaction": {"prune": True}}, "no permission"
    started = []
    for run in ({"effort": "low"}, {"effort": "low", "profile": DEFAULT}):
        adapter = _codex(tmp_path, _codex_samples, env=owner, **run)
        asyncio.run(_collect(adapter))
        started.append(adapter.fake.log[0])
    assert started[0] == started[1]
    assert started[1][1]["config"] == {**codex_module.compact_config({}), "model_reasoning_effort": "low"}
    assert started[1][1]["developer_instructions"] == codex_module.DEVELOPER_NOTE


# Claude Code


def test_claude_code_under_a_profile_gets_only_its_tools_skills_mcp_servers_and_models(tmp_path):
    needs("claude_agent_sdk")
    limited = profile(
        tool=["read", "edit", "mcp:github"],
        skill=["create-exec-plan"],
        model=["opus", "sonnet"],
        instructions=INSTRUCTIONS,
    )
    command, env, options = _launch(tmp_path, profile=limited, model="sonnet")
    tools = command[command.index("--tools") + 1].split(",")
    assert tools == [*profiles.CLAUDE_TOOLS["read"], *profiles.CLAUDE_TOOLS["edit"], "TodoWrite", "ToolSearch", "Skill"]
    for left_out in ("Bash", "WebFetch", "WebSearch", "Agent", "Task", "Workflow"):
        assert left_out not in tools
    assert options.skills == ["create-exec-plan"]
    assert command[command.index("--allowedTools") + 1] == "Skill(create-exec-plan)"
    assert "--setting-sources=user,project,local" in command, "the SDK alone would drop local"
    settings = json.loads(command[command.index("--settings") + 1])
    assert settings == {"allowedMcpServers": [{"serverName": "github"}], "availableModels": ["opus", "sonnet"]}
    assert command[command.index("--model") + 1] == "sonnet"
    append = command[command.index("--append-system-prompt") + 1]
    assert append.startswith(claude_module.SYSTEM_NOTE + "\n\n")
    assert append.endswith(
        f"You are Reviewer (@reviewer), an agent of this project. Its instructions:\n\n{INSTRUCTIONS}"
    )
    assert options.permission_mode == "bypassPermissions", "the rest of the session is as before"
    assert list(options.hooks) == ["PreToolUse"] and options.hooks["PreToolUse"][0].matcher is None


def test_claude_code_under_a_profile_that_limits_skills_starts_no_subagent(tmp_path):
    needs("claude_agent_sdk")
    command, _, options = _launch(tmp_path, profile=profile(skill=["dataviz"], tool=["read", "agent", "bash"]))
    tools = command[command.index("--tools") + 1].split(",")
    assert "Bash" in tools and "Read" in tools and not set(profiles.CLAUDE_TOOLS["agent"]) & set(tools)
    _, _, options = _launch(tmp_path, profile=profile(skill=[]), kind="author")
    assert options.tools is None, "every built-in tool but the subagents'"
    assert options.disallowed_tools == ["AskUserQuestion", *profiles.CLAUDE_TOOLS["agent"]]
    assert options.skills == [] and "Skill" not in options.allowed_tools
    context = _claude(tmp_path, _samples, profile=profile(skill=["dataviz"])).context
    assert ClaudeCodeAdapter.environment_notes(context) == [
        "Claude Code runs without its tools that start subagents (Agent, Workflow): agent profile @reviewer "
        "(version 3) limits skills, and Claude Code's skill filter does not reach a subagent."
    ]


def test_the_profile_hook_refuses_mcp_servers_skills_and_subagent_models_the_profile_leaves_out():
    limited = profiles.Profile.of(
        {"profile": profile(tool=["read", "agent", "mcp:github", "mcp:plugin:x"], skill=["Dataviz"], model=["opus"])}
    )
    refusal = profiles.claude_call_refusal
    assert refusal(limited, "mcp__github__create_issue", {}) is None
    assert refusal(limited, "mcp__plugin_x__ping", {}) is None, "compared as tool names write the server"
    assert refusal(limited, "mcp__slack__post", {}) == (
        "agent profile @reviewer (version 3) does not allow the tools of MCP server slack"
    )
    assert refusal(limited, "Skill", {"skill": "dataviz"}) is None, "skill names compare ignoring case"
    assert "does not allow skill deploy" in refusal(limited, "Skill", {"skill": "deploy"})
    assert "does not allow skill evo-hub:using-agent-hub" in refusal(
        limited, "Skill", {"skill": "evo-hub:using-agent-hub"}
    )
    assert (
        refusal(limited, "Agent", {"prompt": "x"}) is None and refusal(limited, "Agent", {"model": "inherit"}) is None
    )
    assert refusal(limited, "Agent", {"model": "opus"}) is None
    assert "does not allow model haiku" in refusal(limited, "Agent", {"model": "haiku"})
    assert refusal(limited, "Read", {"file_path": "/etc/hosts"}) is None
    needs("claude_agent_sdk")
    gate = profiles.claude_hooks(limited)["PreToolUse"][0].hooks[0]
    denied = asyncio.run(gate({"tool_name": "mcp__slack__post", "tool_input": {}}, "toolu_1", {}))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio.run(gate({"tool_name": "mcp__github__x", "tool_input": {}}, "toolu_2", {})) == {}
    assert profiles.claude_hooks(profiles.Profile.of({"profile": profile(instructions=INSTRUCTIONS)})) is None


def _status(*servers: tuple[str, str]) -> dict:
    return {"mcpServers": [{"name": name, "status": status, "scope": "user"} for name, status in servers]}


def test_claude_code_fails_a_profile_run_whose_cli_has_an_mcp_server_the_profile_leaves_out(tmp_path):
    adapter = _claude(tmp_path, _samples, profile=profile(tool=["read", "mcp:github"]))
    original = adapter.make_client

    def make_client(options):
        client = original(options)
        adapter.cli.answers["mcp_status"] = _status(("github", "connected"), ("slack", "pending"), ("old", "disabled"))
        return client

    adapter.make_client = make_client
    with pytest.raises(ProfileRefused, match=r"MCP server\(s\) slack, which agent profile @reviewer"):
        asyncio.run(adapter.start())
    assert ("control", "mcp_status") in adapter.cli.log
    assert not [entry for entry in adapter.cli.log if entry[0] == "user"], "the prompt never went in"


def test_claude_code_starts_a_profile_run_whose_cli_has_only_allowed_mcp_servers(tmp_path):
    adapter = _claude(tmp_path, _samples, profile=profile(tool=["read", "mcp:github"]))
    events, outcome = asyncio.run(_collect(adapter))
    assert outcome.completed
    assert adapter.cli.log[:3] == [("control", "initialize"), ("control", "mcp_status"), ("user", PROMPT)]
    # Without a tool allowlist the adapter asks nothing more of the CLI than before.
    adapter = _claude(tmp_path, _samples, profile=profile(model=["opus"]), model="opus")
    asyncio.run(_collect(adapter))
    assert adapter.cli.log[:2] == [("control", "initialize"), ("user", PROMPT)]


# The model


def test_a_profile_keeps_the_session_to_its_models_and_refuses_a_run_on_another():
    limited = profile(model=["opus", "sonnet"])
    for cls in (ClaudeCodeAdapter, OpencodeAdapter, CodexAdapter):
        assert cls.profile_refusal({"profile": limited, "model": "opus"}) is None
        assert cls.profile_refusal({"profile": limited, "model": "haiku"}) == (
            "the run's model haiku is not one agent profile @reviewer (version 3) allows (opus, sonnet)"
        )
        assert (
            cls.profile_refusal({"profile": profile(model=[])}) == "agent profile @reviewer (version 3) allows no model"
        )

    def chosen(run: dict, env: dict) -> str | None:
        context = adapter_module.RunContext(run=run, worktree=None, prompt="", env=env)
        return profiles.session_model(context, "claude-code")

    setting = {"EVO_WORKER_CLAUDE_CODE_MODEL": "haiku"}
    assert chosen({"model": "sonnet", "profile": limited}, setting) == "sonnet"
    assert chosen({"profile": limited}, setting) == "opus", "never the daemon's model outside the allowlist"
    assert chosen({"profile": limited}, {"EVO_WORKER_CLAUDE_CODE_MODEL": "sonnet"}) == "sonnet"
    assert chosen({"profile": DEFAULT}, setting) == "haiku" and chosen({}, setting) == "haiku", "as before"


# opencode


def test_opencode_under_a_profile_denies_the_tools_skills_and_mcp_servers_it_leaves_out(tmp_path):
    limited = profile(tool=["read", "mcp:github"], skill=["dataviz"], model=[MODEL], instructions=INSTRUCTIONS)
    owner = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}  # no opencode config of the machine's
    adapter = _opencode(tmp_path, _opencode_samples, model=MODEL, profile=limited, env=owner)

    async def go():
        adapter.fake_mcp = {"github": {"status": "connected"}, "my.slack": {"status": "connected"}}
        original = adapter.start_server

        async def start_server():
            server = await original()
            adapter.fake.mcp = adapter.fake_mcp
            return server

        adapter.start_server = start_server
        return await _collect(adapter)

    events, outcome = asyncio.run(go())
    assert outcome.completed
    requests = adapter.fake.requests
    assert requests[0] == ("mcp",)
    create = requests[1]
    denied = ["edit", "bash", "webfetch", "websearch", "codesearch", "task"]
    assert create[0] == "create" and create[2]["permission"] == [
        *opencode_module.RUN_RULES,
        *[{"permission": name, "action": "deny", "pattern": "*"} for name in denied],
        {"permission": "my_slack_*", "action": "deny", "pattern": "*"},
        {"permission": "skill", "action": "deny", "pattern": "*"},
        {"permission": "skill", "action": "allow", "pattern": "dataviz"},
    ]
    prompt = requests[2]
    assert prompt[2]["model"] == {"providerID": "zai-coding-plan", "modelID": "glm-5.3-flash"}
    assert prompt[2]["system"] == profiles.with_instructions(
        common.HEADLESS_NOTE, profiles.Profile.of({"profile": limited})
    )
    assert prompt[2]["system"].endswith(INSTRUCTIONS)
    content = json.loads(adapter.server_env()["OPENCODE_CONFIG_CONTENT"])
    permission = {**dict.fromkeys(denied, "deny"), "skill": {"*": "deny", "dataviz": "allow"}}
    assert content == {"compaction": {"prune": True}, "permission": permission}


def test_opencode_under_a_profile_merges_its_permissions_over_the_owners_config_content():
    limited = profiles.Profile.of({"profile": profile(tool=["read", "edit", "bash", "web"], skill=[])})
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps({"model": "a/b", "permission": {"bash": "ask", "read": "allow"}})}
    content = json.loads(profiles.opencode_env(env, limited)["OPENCODE_CONFIG_CONTENT"])
    assert content == {"model": "a/b", "permission": {"bash": "ask", "read": "allow", "task": "deny", "skill": "deny"}}
    assert profiles.opencode_env(env, None) == env


def test_opencode_resuming_a_session_under_a_profile_sets_its_rules_on_it_again(tmp_path):
    adapter = _opencode(tmp_path, _opencode_samples, model=MODEL, profile=profile(skill=["dataviz"]), resume=OC_SESSION)
    asyncio.run(_collect(adapter))
    get, patch = adapter.fake.requests[:2]
    assert get == ("get", OC_SESSION) and patch[:2] == ("patch", OC_SESSION)
    assert patch[2]["permission"] == [
        *opencode_module.RUN_RULES,
        {"permission": "skill", "action": "deny", "pattern": "*"},
        {"permission": "skill", "action": "allow", "pattern": "dataviz"},
    ]
    assert ("mcp",) not in adapter.fake.requests, "a profile that allows every tool asks for no MCP server"
    adapter = _opencode(tmp_path, _opencode_samples, model=MODEL, profile=DEFAULT, resume=OC_SESSION)
    asyncio.run(_collect(adapter))
    assert [item[0] for item in adapter.fake.requests[:2]] == ["get", "prompt"], "as before for a default profile"


def test_opencode_under_a_profile_that_limits_models_never_takes_the_configs_model(tmp_path):
    adapter = _opencode(
        tmp_path, _opencode_samples, config_model="zai-coding-plan/glm-5.3-highspeed", profile=profile(model=[MODEL])
    )
    asyncio.run(_collect(adapter))
    prompt = next(item for item in adapter.fake.requests if item[0] == "prompt")
    assert prompt[2]["model"] == {"providerID": "zai-coding-plan", "modelID": "glm-5.3-flash"}


# Codex


def test_codex_under_a_profile_turns_off_the_features_and_mcp_servers_it_leaves_out(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[mcp_servers.github]\ncommand = "gh-mcp"\n\n[mcp_servers.slack]\ncommand = "s"\n'
    )
    worktree = tmp_path / "worktree"
    (worktree / ".codex").mkdir(parents=True)
    (worktree / ".codex" / "config.toml").write_text('[mcp_servers.local-db]\ncommand = "db"\n')
    limited = profile(tool=["read", "edit", "mcp:github"], model=["gpt-5.5"], instructions=INSTRUCTIONS)
    adapter = _codex(
        tmp_path, _codex_samples, profile=limited, model="gpt-5.5", effort="low", env={"CODEX_HOME": str(home)}
    )
    asyncio.run(_collect(adapter))
    options = adapter.fake.log[0][1]
    assert options["model"] == "gpt-5.5"
    assert options["developer_instructions"].startswith(codex_module.DEVELOPER_NOTE + "\n\n")
    assert options["developer_instructions"].endswith(INSTRUCTIONS)
    off = dict.fromkeys(profiles.CODEX_TOOL_FEATURES, False)
    off.update(shell_tool=False, unified_exec=False, multi_agent=False, multi_agent_v2=False)
    assert options["config"] == {
        "features": off,
        "web_search": "disabled",
        "mcp_servers": {"slack": {"enabled": False}, "local-db": {"enabled": False}},
        "model_auto_compact_token_limit": 200_000,
        "tool_output_token_limit": 8_000,
        "model_reasoning_effort": "low",
    }
    adapter = _codex(
        tmp_path, _codex_samples, profile=limited, model="gpt-5.5", resume=THREAD, env={"CODEX_HOME": str(home)}
    )
    asyncio.run(_collect(adapter))
    assert adapter.fake.log[0][0] == "thread/resume" and adapter.fake.log[0][2]["config"]["features"] == off


def test_codex_refuses_a_profile_that_leaves_out_edit_or_limits_skills():
    assert "codex cannot turn off apply_patch" in CodexAdapter.profile_refusal({"profile": profile(tool=["read"])})
    assert "codex has no switch that hides skills" in CodexAdapter.profile_refusal({"profile": profile(skill=["x"])})
    assert CodexAdapter.profile_refusal({"profile": profile(tool=["read", "edit"], instructions="x")}) is None
    for cls in (ClaudeCodeAdapter, OpencodeAdapter):
        assert cls.profile_refusal({"profile": profile(tool=["read"], skill=["x"])}) is None


# An adapter that knows no profiles, and the daemon


class _Plain(Adapter):
    runtime = "claude-code"
    binary = "claude"


def test_an_adapter_that_knows_no_profiles_refuses_a_profile_that_limits_the_session():
    assert _Plain.profile_refusal({}) is None and _Plain.profile_refusal({"profile": DEFAULT}) is None
    assert _Plain.profile_refusal({"profile": profile(tool=["read"])}) == (
        "the claude-code adapter of this worker does not apply agent profiles, and agent @reviewer limits tools, "
        "skills or models or has instructions"
    )
    assert _Plain.profile_refusal({"profile": profile(instructions=INSTRUCTIONS)}) is not None


def test_the_daemon_says_it_applies_agent_profiles():
    assert runs.AGENT_PROFILES in runs.WORKER_FEATURES and set(runs.HUB_FEATURES) == set(runs.WORKER_FEATURES)


pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from tests.worker import fake_adapter  # noqa: E402
from tests.worker.test_plan_runs import PLAN, machine, wait_for, with_daemon  # noqa: E402, F401


def _queue(hub, spec_profile) -> int:
    run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
    hub.runs[run_id]["spec"]["profile"] = spec_profile
    return run_id


def test_a_profile_run_its_adapter_cannot_apply_fails_before_anything_with_the_reason(machine):  # noqa: F811
    found = {}

    async def body(hub, daemon):
        run_id = _queue(hub, profile(tool=["read"], instructions=INSTRUCTIONS))
        assert await hub.wait_state(run_id, "done", "failed") == "failed", hub.texts(run_id)
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(hub=hub, run=run_id)
        assert runs.AGENT_PROFILES in hub.last_heartbeat["features"]

    with_daemon(machine, body)
    hub, run_id = found["hub"], found["run"]
    run = hub.runs[run_id]
    why = (
        "the claude-code adapter of this worker does not apply agent profiles, and agent @reviewer limits tools, "
        "skills or models or has instructions"
    )
    assert run["failure_cause"] == "runtime" and run["error"] == f"agent profile: {why}"
    assert f"Agent profile: {why}; the agent did not start." in hub.texts(run_id)
    starts = machine.starts() if machine.starts_path.exists() else []
    assert [item for item in starts if item["run"] == run_id] == [], "the agent never started"
    assert ("ask", run_id) not in hub.credential_calls, "no lease was taken"
    assert not machine.directory(run_id).exists(), "no worktree, no directory of the run"


def test_a_profile_run_its_adapter_applies_goes_on_and_says_what_the_profile_limits(machine, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fake_adapter.FakeAdapter, "profile_refusal", classmethod(lambda cls, run: None))
    steps = [{"write": {"a.txt": "a\n"}}, {"result": {"verify_commands": ["test -f a.txt"]}}]
    machine.scenarios({"1": steps})
    found = {}

    async def body(hub, daemon):
        limited = _queue(hub, profile(tool=["read", "edit"], skill=[], instructions=INSTRUCTIONS))
        assert await hub.wait_state(limited, "done", "failed", "review") == "done", hub.texts(limited)
        default = hub.queue_step_run(PLAN, "1", "alpha", "feat/default")
        hub.runs[default]["spec"]["profile"] = DEFAULT
        assert await hub.wait_state(default, "done", "failed", "review") == "done", hub.texts(default)
        found.update(hub=hub, limited=limited, default=default)

    with_daemon(machine, body)
    hub = found["hub"]
    assert (
        "Agent profile @reviewer (version 3) applies in the agent session: tools read, edit; skills none; its "
        "instructions added to the system prompt."
    ) in hub.texts(found["limited"])
    assert not any(text.startswith("Agent profile") for text in hub.texts(found["default"]))


def test_a_profile_run_is_never_handed_to_a_terminal(tmp_path):
    from types import SimpleNamespace

    from evo_agents.worker.runner.agent import Agent

    run = SimpleNamespace(
        spec={"profile": profile(model=["opus"])},
        runtime="claude-code",
        daemon=SimpleNamespace(adapters={"claude-code": ClaudeCodeAdapter}, tmux=SimpleNamespace(available=True)),
    )
    agent = Agent.__new__(Agent)
    agent.run, agent.terminal = run, SimpleNamespace(failed=None)
    assert agent.interactive_unsupported() == (
        "agent profile @reviewer (version 3) limits the session, and the terminal UI does not apply it"
    )
    run.spec = {"profile": DEFAULT}
    assert agent.interactive_unsupported() is None
