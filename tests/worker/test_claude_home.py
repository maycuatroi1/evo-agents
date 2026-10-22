"""The Claude Code config dir of its own of a run under a profile that is not its project's default (the claim's
``own_home``, ``evo_agents.worker.runtimes.claude_home``): where it lives, that it never goes in a commit, the login
it gets, the hub's server, and the agent's environment."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.worker import gitops
from evo_agents.worker.runner.agent import Agent
from evo_agents.worker.runner.common import RunFailed
from evo_agents.worker.runtimes import claude_home, hub_tools
from evo_agents.worker.runtimes.claude_code import subscription_login

AXES = ("credential", "tool", "skill", "worker", "project", "model", "runtime")
CREDENTIALS = {"claudeAiOauth": {"accessToken": "access-1", "refreshToken": "refresh-1", "expiresAt": 1}}


def profile(**allow) -> dict:
    permissions = {axis: {"mode": "all"} for axis in AXES}
    for axis, items in allow.items():
        permissions[axis] = {"mode": "allowlist", "allow": list(items)}
    return {"handle": "reviewer", "version": 2, "name": "Reviewer", "instructions": "", "permissions": permissions}


def owner(tmp_path: Path, credentials: dict | None = CREDENTIALS) -> dict[str, str]:
    """The daemon's environment, whose HOME has the owner's Claude Code config dir (with ``credentials``)."""
    home = tmp_path / "owner"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    if credentials is not None:
        (home / ".claude" / ".credentials.json").write_text(json.dumps(credentials), encoding="utf-8")
    return {"HOME": str(home), "PATH": os.environ.get("PATH", "")}


def never() -> str | None:
    return None


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class Notes(list):
    def __call__(self, text: str, **extra) -> None:
        self.append((text, extra))


def agent(tmp_path: Path, env: dict[str, str], *, runtime: str = "claude-code", leased=(), **spec) -> Agent:
    """An agent of a run in ``tmp_path / "worktree"`` whose daemon's environment is ``env``."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    run = SimpleNamespace(
        id=7,
        spec={"id": 7, "kind": "step", "runtime": runtime, **spec},
        runtime=runtime,
        project="demo",
        daemon=SimpleNamespace(
            env=env,
            home=SimpleNamespace(root=tmp_path / "home", run_dir=lambda run_id: tmp_path / "runs" / str(run_id)),
        ),
        credentials=SimpleNamespace(agent_vars={name: "leased-token" for name in leased}, withheld=frozenset(leased)),
        curator=None,
        venvs={},
        worktree=worktree,
        watchdog=SimpleNamespace(spent_usd=0.0, spent_before=0.0),
        agent_seconds=lambda: 0.0,
        note=Notes(),
    )
    return Agent(run)


def test_config_dir_lives_under_the_result_dir_and_is_never_committed(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    directory = claude_home.config_dir(worktree)
    assert directory == worktree / ".evo-run" / "claude-config"
    login = claude_home.prepare(directory, owner_env=owner(tmp_path), leased=False, server=None, keychain=never)
    assert login == f"a copy of {tmp_path / 'owner' / '.claude' / '.credentials.json'}"
    assert (directory / ".gitignore").read_text() == "*\n"
    assert gitops.excluded(f"{claude_home.CONFIG_DIR}/.credentials.json")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=worktree, capture_output=True, text=True
    )
    assert status.stdout == ""  # nothing of the dir shows, so nothing of it can be committed
    assert mode(directory) == 0o700


def test_home_copies_the_owners_login_file_0600_without_its_refresh_token(tmp_path):
    directory = tmp_path / "config"
    claude_home.prepare(directory, owner_env=owner(tmp_path), leased=False, server=None, keychain=never)
    copy = directory / ".credentials.json"
    assert mode(copy) == 0o600
    assert json.loads(copy.read_text()) == {"claudeAiOauth": {"accessToken": "access-1", "expiresAt": 1}}
    assert not (directory / "settings.json").exists()  # none of the owner's user settings
    # Claude Code started with the run's dir finds the login there
    assert subscription_login({"CLAUDE_CONFIG_DIR": str(directory)}, keychain=lambda: False) == str(copy)


def test_home_copies_the_login_again_at_each_agent_start(tmp_path):
    env, directory = owner(tmp_path), tmp_path / "config"
    claude_home.prepare(directory, owner_env=env, leased=False, server=None, keychain=never)
    fresh = {"claudeAiOauth": {"accessToken": "access-2", "refreshToken": "refresh-2"}}
    (Path(env["HOME"]) / ".claude" / ".credentials.json").write_text(json.dumps(fresh))
    claude_home.prepare(directory, owner_env=env, leased=False, server=None, keychain=never)
    assert json.loads((directory / ".credentials.json").read_text()) == {"claudeAiOauth": {"accessToken": "access-2"}}


def test_home_copies_the_owners_config_dir_named_by_claude_config_dir(tmp_path):
    configured = tmp_path / "elsewhere"
    configured.mkdir()
    (configured / ".credentials.json").write_text(json.dumps(CREDENTIALS))
    env = {**owner(tmp_path, None), "CLAUDE_CONFIG_DIR": str(configured)}
    login = claude_home.prepare(tmp_path / "config", owner_env=env, leased=False, server=None, keychain=never)
    assert login == f"a copy of {configured / '.credentials.json'}"


def test_home_copies_the_keychain_login_when_the_owner_has_no_file(tmp_path):
    directory = tmp_path / "config"
    login = claude_home.prepare(
        directory, owner_env=owner(tmp_path, None), leased=False, server=None, keychain=lambda: json.dumps(CREDENTIALS)
    )
    assert login == "a copy of the keychain item Claude Code-credentials"
    assert mode(directory / ".credentials.json") == 0o600
    assert "refreshToken" not in (directory / ".credentials.json").read_text()


def test_home_with_a_leased_token_copies_no_login(tmp_path):
    directory = tmp_path / "config"
    env = owner(tmp_path)
    claude_home.prepare(directory, owner_env=env, leased=False, server=None, keychain=never)
    login = claude_home.prepare(directory, owner_env=env, leased=True, server=None, keychain=never)
    assert login == "the lease's CLAUDE_CODE_OAUTH_TOKEN"
    assert not (directory / ".credentials.json").exists()  # the copy of an agent before a move to the lease


def test_home_without_any_login_has_none(tmp_path):
    directory = tmp_path / "config"
    assert (
        claude_home.prepare(directory, owner_env=owner(tmp_path, None), leased=False, server=None, keychain=never)
        is None
    )
    assert not (directory / ".credentials.json").exists()


def test_home_names_the_hub_server_in_its_claude_json_and_keeps_what_is_there(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / ".claude.json").write_text(json.dumps({"projects": {"/w": {"hasTrustDialogAccepted": True}}}))
    server = {"type": "stdio", "command": "python3", "args": ["-m", "evo_agents", "hub", "mcp"], "env": {}}
    claude_home.prepare(directory, owner_env=owner(tmp_path), leased=False, server=server, keychain=never)
    data = json.loads((directory / ".claude.json").read_text())
    assert data["mcpServers"] == {"evo-hub": server}
    assert data["hasCompletedOnboarding"] is True
    assert data["projects"] == {"/w": {"hasTrustDialogAccepted": True}}
    assert mode(directory / ".claude.json") == 0o600
    claude_home.prepare(directory, owner_env=owner(tmp_path / "again"), leased=True, server=None, keychain=never)
    assert json.loads((directory / ".claude.json").read_text())["mcpServers"] == {}


def test_own_home_agent_env_gets_a_config_dir_of_the_run_and_the_default_profile_none(tmp_path):
    env = {**owner(tmp_path), "CLAUDE_CONFIG_DIR": "/owner/claude"}
    own = agent(tmp_path, env, own_home=True, profile=profile())
    assert own.env()["CLAUDE_CONFIG_DIR"] == str(tmp_path / "worktree" / ".evo-run" / "claude-config")
    default = agent(tmp_path, env, own_home=False, profile=profile())
    assert default.env()["CLAUDE_CONFIG_DIR"] == "/owner/claude"  # unchanged: the owner's
    old_hub = agent(tmp_path, env, profile=profile())
    assert old_hub.env() == default.env()
    codex = agent(tmp_path, env, runtime="codex", own_home=True, profile=profile())
    assert codex.env()["CLAUDE_CONFIG_DIR"] == "/owner/claude"
    container = {"plugin": "p", "name": "n"}
    assert not claude_home.wanted({"own_home": True, "environment": container}, "claude-code")


def test_own_home_agent_writes_its_config_dir_and_says_where_its_login_comes_from(tmp_path):
    own = agent(tmp_path, owner(tmp_path), own_home=True, profile=profile())
    context = own.context("go", None)
    asyncio.run(own.prepare_home(context))
    asyncio.run(own.prepare_home(context))
    directory = Path(context.env["CLAUDE_CONFIG_DIR"])
    assert (directory / ".credentials.json").is_file()
    server = json.loads((directory / ".claude.json").read_text())["mcpServers"][hub_tools.SERVER]
    assert server["args"][-2:] == ["hub", "mcp"]
    assert server["env"]["EVO_RUN_ID"] == "7"
    notes = [text for text, _ in own.run.note]
    assert len(notes) == 1 and "config dir of its own" in notes[0] and "a copy of" in notes[0]
    # a curator run does not get the server twice from its adapter
    curator = SimpleNamespace(run={"curator": {"role": "reviewer"}}, env=context.env, worktree=context.worktree)
    curator.environment = context.environment
    assert hub_tools.claude_servers(curator) == {}


def test_own_home_agent_under_a_profile_without_the_hub_server_gets_none(tmp_path):
    own = agent(tmp_path, owner(tmp_path), own_home=True, profile=profile(tool=["Read", "Bash"]))
    context = own.context("go", None)
    asyncio.run(own.prepare_home(context))
    data = json.loads((Path(context.env["CLAUDE_CONFIG_DIR"]) / ".claude.json").read_text())
    assert data["mcpServers"] == {}
    allowed = agent(tmp_path / "b", owner(tmp_path / "b"), own_home=True, profile=profile(tool=["Read", "mcp:evo-hub"]))
    context = allowed.context("go", None)
    asyncio.run(allowed.prepare_home(context))
    data = json.loads((Path(context.env["CLAUDE_CONFIG_DIR"]) / ".claude.json").read_text())
    assert list(data["mcpServers"]) == ["evo-hub"]


def test_own_home_agent_with_a_leased_token_needs_no_login_of_the_owner(tmp_path):
    own = agent(tmp_path, owner(tmp_path, None), own_home=True, profile=profile(), leased=("CLAUDE_CODE_OAUTH_TOKEN",))
    context = own.context("go", None)
    asyncio.run(own.prepare_home(context))
    assert "the lease's CLAUDE_CODE_OAUTH_TOKEN" in own.run.note[0][0]


def test_own_home_agent_without_a_login_fails_before_it_starts_with_cause_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_home, "keychain_secret", never)
    own = agent(tmp_path, owner(tmp_path, None), own_home=True, profile=profile())
    with pytest.raises(RunFailed) as failed:
        asyncio.run(own.prepare_home(own.context("go", None)))
    assert failed.value.fields["cause"] == "credentials"
    assert "no login to give it" in str(failed.value)
    assert own.run.note[-1][1] == {"cause": "credentials"}


def test_own_home_codex_keeps_the_owners_home_and_says_so_once(tmp_path):
    codex = agent(tmp_path, owner(tmp_path), runtime="codex", own_home=True, profile=profile())
    context = codex.context("go", None)
    asyncio.run(codex.prepare_home(context))
    asyncio.run(codex.prepare_home(context))
    assert [text for text, _ in codex.run.note] == [claude_home.OWNER_HOME_NOTE.format(runtime="codex")]
    assert not (tmp_path / "worktree" / ".evo-run").exists()
    default = agent(tmp_path / "d", owner(tmp_path / "d"), runtime="codex", profile=profile())
    asyncio.run(default.prepare_home(default.context("go", None)))
    assert default.run.note == []
