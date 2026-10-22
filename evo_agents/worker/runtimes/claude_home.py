"""A Claude Code config dir of its own for the agent of a run under an agent profile that is not its project's default
(the claim's ``own_home``, which the hub sends only to a daemon of feature ``runs.AGENT_HOME``).

Such an agent runs with CLAUDE_CONFIG_DIR set to CONFIG_DIR of the run's directory, so the session loads none of the
owner's user-level settings, hooks, plugins, skills, agents, CLAUDE.md or memory; what the hub delivers (the skills
under ``.claude/skills`` of the run's directory, the agent packs) and what the profile sets (tools, skills,
allowedMcpServers, availableModels, its PreToolUse hook) go to the session as before. The dir lives in the run's
directory, so it follows the session wherever the run's directory goes: a resumed session, another account, a chat
continuation run or a parked run that adopts the directory, the terminal UI of an interactive run or a takeover. It is
under ``runs.RESULT_DIR``, which never goes in a commit (``gitops.RUN_COMMIT_EXCLUDES``), and holds a ``.gitignore``
that ignores all of it, even when the run's directory is a step run's worktree.

``prepare`` writes it before each agent starts:

- ``.claude.json``: onboarding done, and the hub's MCP server (``hub_tools.home_server``) under ``mcpServers`` when the
  run's profile allows it, so headless and terminal UI sessions both get it;
- the login, in this order: the lease's CLAUDE_CODE_OAUTH_TOKEN, which needs nothing in the dir; else a copy of the
  owner's (the credentials file of the owner's config dir, CLAUDE_CONFIG_DIR of the daemon's environment or
  ~/.claude, or the macOS keychain item), mode 0600, written again at each agent start so it carries the owner's
  latest access token. The copy leaves out the refresh token: the owner's is single-use, and a refresh in the run's
  dir would spend it and leave the owner signed out. With neither, there is no login and the run fails before the
  agent starts, with cause credentials (``REFUSAL``).

The agent of a run in a container of a plugin's environment keeps the container's home; one of another runtime
(Codex, opencode) keeps the owner's home, which the run's log says once (``OWNER_HOME_NOTE``).
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from evo_agents.hub import runs

RUNTIME = "claude-code"
CONFIG_DIR = f"{runs.RESULT_DIR}/claude-config"  # in the run's directory
CREDENTIALS_FILE = ".credentials.json"
GLOBAL_FILE = ".claude.json"
KEYCHAIN_ITEM = "Claude Code-credentials"
KEYCHAIN_TIMEOUT = 10.0
SERVER = "evo-hub"
OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
REFUSAL = (
    "a run under an agent profile that is not its project's default runs Claude Code with a config dir of its own, "
    f"and this machine has no login to give it: no {OAUTH_TOKEN} leased to the run, no {CREDENTIALS_FILE} in the "
    f"owner's Claude Code config dir, no keychain item {KEYCHAIN_ITEM}. Sign Claude Code in on the worker, or give "
    f"the run a lease of {OAUTH_TOKEN} made by `claude setup-token`"
)
OWNER_HOME_NOTE = (
    "{runtime} runs with this machine's home and its configuration there: only a claude-code agent under a profile "
    "that is not its project's default gets a config dir of its own."
)


def wanted(spec: Mapping, runtime: str) -> bool:
    """Whether the agent of the run of claim ``spec`` on ``runtime`` gets a config dir of its own: a claude-code run
    the claim says ``own_home`` of, on this machine (not in a container)."""
    return bool(spec.get("own_home")) and runtime == RUNTIME and not spec.get("environment")


def keeps_owner_home(spec: Mapping, runtime: str) -> bool:
    """Whether the run is one ``own_home`` names whose runtime keeps the owner's home (OWNER_HOME_NOTE)."""
    return bool(spec.get("own_home")) and runtime != RUNTIME and not spec.get("environment")


def config_dir(worktree: Path | str) -> Path:
    return Path(worktree) / CONFIG_DIR


def owner_dir(env: Mapping[str, str]) -> Path:
    """The owner's Claude Code config dir: CLAUDE_CONFIG_DIR of ``env``, else ~/.claude."""
    configured = env.get("CLAUDE_CONFIG_DIR") or ""
    if os.path.isabs(configured):
        return Path(configured)
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".claude"


def keychain_secret() -> str | None:
    """The secret of the macOS keychain item of Claude Code's login; None elsewhere, or when there is none."""
    if platform.system() != "Darwin":
        return None
    user = os.environ.get("USER") or ""
    command = ["security", "find-generic-password", *(["-a", user] if user else []), "-w", "-s", KEYCHAIN_ITEM]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    secret = done.stdout.strip()
    return secret if done.returncode == 0 and secret else None


def owner_login(env: Mapping[str, str], keychain: Callable[[], str | None] | None = None) -> tuple[str, str] | None:
    """The owner's Claude Code login and where it was read: the credentials file of ``owner_dir``, else the keychain
    item (``keychain`` reads it; tests hand their own); None when there is neither."""
    path = owner_dir(env) / CREDENTIALS_FILE
    try:
        return path.read_text(encoding="utf-8"), str(path)
    except (OSError, UnicodeDecodeError):
        pass
    secret = (keychain or keychain_secret)()
    return (secret, f"the keychain item {KEYCHAIN_ITEM}") if secret else None


def without_refresh(credentials: str) -> str:
    """``credentials`` with ``claudeAiOauth.refreshToken`` left out (see the module)."""
    try:
        data = json.loads(credentials)
    except ValueError:
        return credentials
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if isinstance(oauth, dict) and "refreshToken" in oauth:
        del oauth["refreshToken"]
        return json.dumps(data)
    return credentials


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 0600, whole or not at all."""
    part = path.with_name(path.name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)
    os.replace(part, path)


def _global_config(directory: Path, server: dict | None) -> str:
    """The ``.claude.json`` of the dir: what it holds already (the terminal UI writes it too), onboarding done, and
    the hub's server under ``mcpServers``, or none when ``server`` is None."""
    try:
        data = json.loads((directory / GLOBAL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["hasCompletedOnboarding"] = True
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    servers.pop(SERVER, None)
    if server is not None:
        servers[SERVER] = server
    data["mcpServers"] = servers
    return json.dumps(data, indent=2)


def prepare(
    directory: Path,
    *,
    owner_env: Mapping[str, str],
    leased: bool,
    server: dict | None,
    keychain: Callable[[], str | None] | None = None,
) -> str | None:
    """Write the config dir ``directory`` for an agent that starts now (see the module): ``leased`` says a lease of
    the run sets CLAUDE_CODE_OAUTH_TOKEN, ``owner_env`` is the daemon's environment, whose config dir has the owner's
    login. Where the agent's login comes from; None when there is none (``REFUSAL``). OSError when the dir is not
    written."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / ".gitignore").write_text("*\n", encoding="utf-8")
    _write_private(directory / GLOBAL_FILE, _global_config(directory, server))
    copy = directory / CREDENTIALS_FILE
    if leased:
        copy.unlink(missing_ok=True)
        return f"the lease's {OAUTH_TOKEN}"
    found = None if owner_dir(owner_env).resolve() == directory.resolve() else owner_login(owner_env, keychain)
    if found is None:
        copy.unlink(missing_ok=True)
        return None
    credentials, source = found
    _write_private(copy, without_refresh(credentials))
    return f"a copy of {source}"


def note(directory: Path, login: str) -> str:
    """What the run's log says once of the agent's config dir."""
    return (
        f"Claude Code runs with a config dir of its own, {directory}, under a profile that is not the project's "
        f"default: none of the owner's user settings, hooks, plugins, skills, agents, CLAUDE.md or memory load. Its "
        f"login is {login}."
    )
