"""A run on a provider account's model goes through the hub's gateway route (plan model-providers, step 5;
``evo_agents.worker.runtimes.gateway``): what its gateway lease gives Claude Code, opencode and Codex, that a Claude
Code agent of such a run gets no login nor key of the owner or of a lease, and (the ``gateway_secrecy`` tests) that
the run's token stays out of the log, the events, the pane's script and every file the run writes."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import secrets
from pathlib import Path

import pytest

from evo_agents.hub.credentials import Lease
from evo_agents.worker import credentials, interactive, selftest
from evo_agents.worker.adapter import RunContext
from evo_agents.worker.runtimes import claude_code as claude_module
from evo_agents.worker.runtimes import claude_home, codex, gateway, opencode
from tests.worker.test_claude_home import agent, owner
from tests.worker.test_codex_opencode_compaction import _tui_context
from tests.worker.test_credentials import Machine, StubHub, env_lease, needs_worker
from tests.worker.test_runtimes import (
    CLAUDE_KEYS,
    DAEMON_KEY,
    LEASED_TOKEN,
    FakeOpencode,
    _claude,
    _codex,
    _codex_samples,
    _collect,
    _context,
    _opencode,
    _opencode_samples,
    _samples,
)

ROUTE = "https://hub.example.org/v1"
MODEL = "zai/glm-5.3"
TOKEN = "evr_" + secrets.token_hex(20)  # stands for the run's token to the gateway route
FORMATS = {"claude-code": gateway.ANTHROPIC, "opencode": gateway.OPENAI, "codex": gateway.RESPONSES}


def lease(runtime: str = "claude-code", token: str = TOKEN, **fields) -> Lease:
    """The gateway lease the hub gives a run of ``runtime`` (``hub.gateway_runs``)."""
    found = {"base_url": ROUTE, "model": MODEL, "api_format": FORMATS[runtime], **fields}
    return Lease(9, gateway.GATEWAY, gateway.GATEWAY, "gateway:zai", value=token, **found)


def gateway_context(tmp_path: Path, runtime: str, env: dict | None = None, **run) -> RunContext:
    """The context of a gateway run of ``runtime``: the daemon's ``env`` with what its lease adds."""
    added = gateway.agent_env(lease(runtime), env or {})
    return _context(tmp_path, runtime, env={**(env or {}), **added}, leased=frozenset(added), **run)


# What the lease adds to the agent's environment


def test_gateway_lease_gives_claude_code_the_hubs_root_its_token_and_the_runs_model_in_every_slot():
    env = gateway.agent_env(lease(), {})
    assert env["ANTHROPIC_BASE_URL"] == "https://hub.example.org", "Claude Code adds /v1/messages itself"
    assert env["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert all(env[name] == MODEL for name in gateway.CLAUDE_MODELS)
    assert {"ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"} <= set(env)
    assert env[gateway.MODEL] == MODEL and env[gateway.URL] == ROUTE
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert not set(env) & set(gateway.CLAUDE_DROPPED) and gateway.TOKEN not in env
    assert gateway.anthropic_root("https://hub.example.org/v1/") == "https://hub.example.org"


def test_gateway_lease_gives_opencode_a_provider_whose_key_is_read_from_the_environment():
    own = json.dumps({"model": "a/b", "provider": {"mine": {"npm": "x"}}, "compaction": {"prune": True}})
    env = gateway.agent_env(lease("opencode"), {gateway.CONFIG_CONTENT: own})
    assert env[gateway.TOKEN] == TOKEN and "ANTHROPIC_AUTH_TOKEN" not in env
    content = json.loads(env[gateway.CONFIG_CONTENT])
    assert content["model"] == "a/b" and content["compaction"] == {"prune": True}, "the daemon's config stays"
    assert content["provider"]["mine"] == {"npm": "x"}
    assert content["provider"][gateway.PROVIDER] == {
        "npm": "@ai-sdk/openai-compatible",
        "name": gateway.NAME,
        "options": {"baseURL": ROUTE, "apiKey": "{env:EVO_GATEWAY_TOKEN}"},
        "models": {MODEL: {"name": MODEL}},
    }
    for unreadable in ("not json", "[1, 2]"):
        content = json.loads(gateway.opencode_content(ROUTE, MODEL, unreadable))
        assert list(content) == ["provider"] and list(content["provider"]) == [gateway.PROVIDER]


def test_gateway_lease_gives_codex_the_route_and_the_variable_of_its_token():
    env = gateway.agent_env(lease("codex"), {})
    assert env == {gateway.MODEL: MODEL, gateway.URL: ROUTE, gateway.TOKEN: TOKEN}


def test_gateway_lease_counts_only_with_a_route_a_token_and_a_model():
    assert gateway.lease_of([lease()]) == lease()
    for missing in ({"base_url": None}, {"model": None}, {"token": ""}):
        assert gateway.lease_of([lease(**missing)]) is None
    env = Lease(1, "env", "secret", "key", env_var="OPENAI_API_KEY", value="x" * 20)
    assert gateway.lease_of([env]) is None
    assert gateway.drops(lease(), "CLAUDE_CODE_OAUTH_TOKEN") and gateway.drops(lease(), "ANTHROPIC_API_KEY")
    assert not gateway.drops(lease(), "OPENAI_API_KEY") and not gateway.drops(None, "ANTHROPIC_API_KEY")
    assert not gateway.drops(lease("opencode"), "CLAUDE_CODE_OAUTH_TOKEN"), "only Claude Code's login is refused"


# The run's credentials


@pytest.fixture
def gateway_machine(tmp_path) -> Machine:
    return Machine(tmp_path)


@needs_worker
def test_gateway_run_of_claude_code_hands_over_no_leased_login_and_says_so(gateway_machine):
    oauth, other = LEASED_TOKEN, "o" * 24
    leases = [lease().to_json(), env_lease(2, "CLAUDE_CODE_OAUTH_TOKEN", oauth), env_lease(3, "OTHER_KEY", other)]
    held = gateway_machine.leases(7)

    async def go():
        await held.take(StubHub({"leases": leases}), {})
        try:
            assert held.gateway is not None and held.gateway.model == MODEL
            assert "CLAUDE_CODE_OAUTH_TOKEN" not in held.agent_vars
            assert held.agent_vars["OTHER_KEY"] == other
            assert held.agent_vars["ANTHROPIC_AUTH_TOKEN"] == TOKEN
            assert held.agent_vars["ANTHROPIC_MODEL"] == MODEL
            assert {"ANTHROPIC_AUTH_TOKEN", gateway.MODEL} <= held.withheld, "a pane's script holds none of them"
        finally:
            await held.release()

    asyncio.run(go())
    notes = [text for text, _ in gateway_machine.notes if "CLAUDE_CODE_OAUTH_TOKEN" in text or "gateway" in text]
    assert any("is not put into the agent's environment" in text and "gateway route" in text for text in notes)


@needs_worker
def test_gateway_run_of_opencode_keeps_its_env_leases(gateway_machine):
    leases = [lease("opencode").to_json(), env_lease(2, "CLAUDE_CODE_OAUTH_TOKEN", LEASED_TOKEN)]
    held = gateway_machine.leases(7)

    async def go():
        await held.take(StubHub({"leases": leases}), {})
        try:
            assert held.agent_vars["CLAUDE_CODE_OAUTH_TOKEN"] == LEASED_TOKEN
            assert held.agent_vars[gateway.TOKEN] == TOKEN
            assert gateway.PROVIDER in json.loads(held.agent_vars[gateway.CONFIG_CONTENT])["provider"]
        finally:
            await held.release()

    asyncio.run(go())


# Claude Code


def test_gateway_claude_code_agent_gets_no_login_nor_key_of_the_daemon_and_the_launcher_unsets_them(tmp_path):
    added = gateway.agent_env(lease(), {})
    env = {**CLAUDE_KEYS, "CLAUDE_CODE_USE_BEDROCK": "1", **added}
    adapter = _claude(tmp_path, _samples, env=env, leased=frozenset(added))
    seen = {}

    async def look(adapter, events):
        seen["launcher"] = Path(adapter.options.cli_path).read_text(encoding="utf-8")

    events, outcome = asyncio.run(_collect(adapter, look))
    assert outcome.completed
    options = adapter.options.env
    assert not set(options) & set(gateway.CLAUDE_DROPPED), "no login, key or other backend beside the route"
    assert options["ANTHROPIC_AUTH_TOKEN"] == TOKEN and options["ANTHROPIC_BASE_URL"] == "https://hub.example.org"
    assert all(options[name] == MODEL for name in gateway.CLAUDE_MODELS)
    unsets = [line.removeprefix("unset ") for line in seen["launcher"].splitlines() if line.startswith("unset ")]
    assert set(unsets) == set(gateway.CLAUDE_DROPPED), "the SDK passes the daemon's own environment through"
    assert set(claude_module.dropped_env(adapter.context)) >= set(gateway.CLAUDE_DROPPED)
    assert claude_module.dropped_env(_context(tmp_path, "claude-code", env=CLAUDE_KEYS)) == claude_module.DROPPED_ENV


def test_gateway_claude_code_terminal_ui_drops_the_login_and_remote_control(tmp_path):
    context = gateway_context(tmp_path, "claude-code", env=CLAUDE_KEYS)
    tui = interactive.ClaudeCodeTui(context, "sid-1")
    assert set(tui.drop_env) >= set(gateway.CLAUDE_DROPPED)
    assert "--remote-control" not in tui.command("evo-run-41"), "the Claude apps reach no session of the gateway"


def test_gateway_claude_code_agent_runs_in_a_config_dir_of_its_own_without_the_owners_login(tmp_path):
    added = gateway.agent_env(lease(), {})
    daemon = {**owner(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "owner" / ".claude"), **CLAUDE_KEYS}
    run = agent(tmp_path, daemon, leased=tuple(added), gateway=lease())
    env = run.env()
    assert env["CLAUDE_CONFIG_DIR"] == str(claude_home.config_dir(tmp_path / "worktree")), "whatever its profile"
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env, "the daemon's own are left out"
    context = run.context("go", None)
    asyncio.run(run.prepare_home(context))
    directory = Path(context.env["CLAUDE_CONFIG_DIR"])
    assert (directory / ".claude.json").is_file() and not (directory / claude_home.CREDENTIALS_FILE).exists()
    notes = [text for text, _ in run.run.note]
    assert len(notes) == 1 and claude_home.GATEWAY_LOGIN in notes[0] and "gateway route" in notes[0]
    # a login copied there by an earlier start goes
    (directory / claude_home.CREDENTIALS_FILE).write_text("{}", encoding="utf-8")
    login = claude_home.prepare(directory, owner_env=daemon, leased=False, server=None, gateway=True)
    assert login == claude_home.GATEWAY_LOGIN and not (directory / claude_home.CREDENTIALS_FILE).exists()
    assert claude_home.wanted({}, "claude-code", True) and not claude_home.wanted({}, "codex", True)
    assert not claude_home.wanted({"environment": {"plugin": "p", "name": "n"}}, "claude-code", True)


# Codex and opencode


def test_gateway_codex_thread_and_terminal_ui_get_the_route_as_their_model_provider(tmp_path):
    added = gateway.agent_env(lease("codex"), {})
    home = tmp_path / "codex-home"
    home.mkdir()
    adapter = _codex(tmp_path, _codex_samples, env={"CODEX_HOME": str(home), **added}, leased=frozenset(added))
    asyncio.run(_collect(adapter))
    config = adapter.fake.log[0][1]["config"]
    assert config["model_provider"] == gateway.PROVIDER
    assert config["model_providers"][gateway.PROVIDER] == {
        "name": gateway.NAME,
        "base_url": ROUTE,
        "env_key": gateway.TOKEN,
        "wire_api": "responses",
    }
    context = dataclasses.replace(_tui_context(tmp_path, {"CODEX_HOME": str(home), **added}), leased=frozenset(added))
    argv = interactive.CodexTui(context, "thread-1").command("evo-run-7")
    assert codex.config_args(context, None)[-4:] == [
        "-c",
        'model_provider="evo_gateway"',
        "-c",
        'model_providers.evo_gateway={name = "evo-agents gateway", base_url = "https://hub.example.org/v1", '
        'env_key = "EVO_GATEWAY_TOKEN", wire_api = "responses"}',
    ]
    assert 'model_provider="evo_gateway"' in argv, "the terminal UI on the same provider"
    plain = _context(tmp_path, "codex", env={gateway.MODEL: MODEL})  # set by the daemon, not by a lease
    assert gateway.codex_config(plain) == {} and gateway.codex_args(plain) == []


def test_gateway_opencode_session_runs_on_the_gateways_provider(tmp_path, monkeypatch):
    async def providers(self, request):
        from aiohttp import web

        listing = [{"id": gateway.PROVIDER, "models": {MODEL: {}}}, {"id": "zai-coding-plan", "models": {}}]
        return web.json_response({"providers": listing, "default": {}})

    monkeypatch.setattr(FakeOpencode, "providers", providers)
    added = gateway.agent_env(lease("opencode"), {})
    env = {"EVO_WORKER_OPENCODE_MODEL": "zai-coding-plan/glm-5.3-flash", **added}
    adapter = _opencode(tmp_path, _opencode_samples, env=env, leased=frozenset(added))
    events, outcome = asyncio.run(_collect(adapter))
    assert adapter.model == (gateway.PROVIDER, MODEL) and outcome.completed
    prompt = next(request for request in adapter.fake.requests if request[0] == "prompt")
    assert prompt[2]["model"] == {"providerID": gateway.PROVIDER, "modelID": MODEL}
    assert gateway.opencode_model(_context(tmp_path, "opencode", env=env)) is None, "not a lease's: no gateway run"


# Selftest


def test_gateway_selftest_builds_the_runs_environment_for_a_fake_gateway(tmp_path):
    daemon = {**CLAUDE_KEYS, "PATH": os.environ.get("PATH", "")}
    env, leased = selftest.gateway_env("claude-code", "http://127.0.0.1:9/v1", MODEL, TOKEN, daemon, tmp_path)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9" and env["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert not set(env) & set(gateway.CLAUDE_DROPPED) and gateway.MODEL in leased
    directory = Path(env["CLAUDE_CONFIG_DIR"])
    assert directory == claude_home.config_dir(tmp_path) and not (directory / claude_home.CREDENTIALS_FILE).exists()
    env, leased = selftest.gateway_env("codex", ROUTE, MODEL, TOKEN, daemon, tmp_path)
    assert env[gateway.TOKEN] == TOKEN and env["ANTHROPIC_API_KEY"] == DAEMON_KEY, "only Claude Code's are dropped"
    assert gateway.codex_config(RunContext(run={}, worktree=tmp_path, prompt="", env=env, leased=leased))


# gateway_secrecy: the run's token goes to the route alone


@needs_worker
def test_gateway_secrecy_the_runs_token_is_masked_in_the_log_and_the_events(gateway_machine):
    from evo_agents.worker import logs

    log_path = gateway_machine.tmp / "worker.log"
    logs.configure(log_path, stderr=False)
    held = gateway_machine.leases(7)

    def logged(text: str) -> str:
        logging.getLogger("evo_agents.worker").warning(text)
        return log_path.read_text(encoding="utf-8").splitlines()[-1]

    async def go():
        await held.take(StubHub({"leases": [lease().to_json()]}), {})
        try:
            assert TOKEN not in logged(f"the agent printed Authorization: Bearer {TOKEN}")
            body = {"text": f"curl -H 'x-api-key: {TOKEN}'", "env": {"ANTHROPIC_AUTH_TOKEN": TOKEN}}
            assert TOKEN not in json.dumps(credentials.scrub(body))
            assert TOKEN not in repr(held.gateway) and TOKEN not in repr(held.leases)
        finally:
            await held.release()

    asyncio.run(go())
    assert TOKEN not in credentials.held_values()


def test_gateway_secrecy_the_panes_script_never_holds_the_token(tmp_path):
    for runtime in FORMATS:
        added = gateway.agent_env(lease(runtime), {})
        env = {"PATH": "/usr/bin", "HOME": str(tmp_path), **added}
        script = interactive.write_script(
            tmp_path / f"{runtime}.sh",
            ["true"],
            env,
            tmp_path,
            withheld=frozenset(added),
            env_command="evo-agents worker env --run 7",
        )
        text = script.read_text(encoding="utf-8")
        assert TOKEN not in text and 'eval "$(evo-agents worker env --run 7)"' in text


def test_gateway_secrecy_no_config_the_runtimes_read_holds_the_token(tmp_path):
    content = gateway.agent_env(lease("opencode"), {})[gateway.CONFIG_CONTENT]
    assert TOKEN not in content and "{env:EVO_GATEWAY_TOKEN}" in content
    context = gateway_context(tmp_path, "codex")
    assert TOKEN not in json.dumps(gateway.codex_config(context)) and TOKEN not in " ".join(gateway.codex_args(context))
    served = opencode.compaction_env({**os.environ, **gateway_context(tmp_path, "opencode").env}, tmp_path)
    assert TOKEN not in served[gateway.CONFIG_CONTENT]
    worktree = tmp_path / "worktree"
    assert not [path for path in worktree.rglob("*") if path.is_file() and TOKEN in path.read_text(errors="ignore")]
