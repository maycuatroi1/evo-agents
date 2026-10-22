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
- before that copy, the owner's login refreshed first (owner decision #337, refresh_first) when it is the credentials
  file and its access token expires within REFRESH_BEFORE or has expired (``refresh_owner``): the daemon refreshes it
  itself, once, under a file lock its runs share (``REFRESH_LOCK`` in the worker's home), as Claude Code does
  (Claude Code 2.1.295: a POST of grant_type refresh_token, the stored refresh token, CLIENT_ID and the stored scopes
  to TOKEN_URL), and writes the new tokens back to the owner's credentials file, whole or not at all, keeping its
  mode, so the owner's own Claude Code goes on with them. A run waiting on the lock reads the file again and finds it
  fresh. A refresh that fails fails the run before the agent starts, with cause credentials (``RefreshFailed``),
  unless another process (the owner's Claude Code) made the file fresh meanwhile. A login of the keychain item is
  copied as it is: the daemon writes no keychain item. No token value is ever logged.
- ``gh-config``, an empty dir beside the config dir: the agent's GH_CONFIG_DIR (``gh_dir``), so ``gh`` in the run
  sees none of the owner's logins (``~/.config/gh``) and speaks with GH_TOKEN of the run's leases alone.

Claude Code, headless and in the terminal UI, gets the MCP servers the daemon names on its command line alone
(``--strict-mcp-config``, ``mcp_config``): the hub's server, when the profile allows it, and those of the agent packs;
never a project's ``.mcp.json`` in the run's directory or above it, nor one of the owner's.

The agent of a run in a container of a plugin's environment keeps the container's home; one of another runtime
(Codex, opencode) keeps the owner's home, which the run's log says once (``OWNER_HOME_NOTE``).
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
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
GH_DIR = f"{runs.RESULT_DIR}/gh-config"  # the agent's GH_CONFIG_DIR, empty
STRICT_MCP = "--strict-mcp-config"
OAUTH_KEY = "claudeAiOauth"
# How Claude Code 2.1.295 refreshes its claude.ai login (its bundled config: TOKEN_URL, CLIENT_ID, the scopes it asks)
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
SCOPES = (
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
    "user:plugins",
)
REFRESH_BEFORE = 3600.0  # seconds: an owner's access token expiring within this is refreshed before it is copied
REFRESH_TIMEOUT = 30.0
REFRESH_LOCK = (
    "claude-refresh.lock"  # in the worker's home: the runs of the daemon refresh the owner's login one at a time
)
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


class RefreshFailed(RuntimeError):
    """The owner's login expires within REFRESH_BEFORE and its refresh failed: the run fails with cause credentials."""


def wanted(spec: Mapping, runtime: str) -> bool:
    """Whether the agent of the run of claim ``spec`` on ``runtime`` gets a config dir of its own: a claude-code run
    the claim says ``own_home`` of, on this machine (not in a container)."""
    return bool(spec.get("own_home")) and runtime == RUNTIME and not spec.get("environment")


def keeps_owner_home(spec: Mapping, runtime: str) -> bool:
    """Whether the run is one ``own_home`` names whose runtime keeps the owner's home (OWNER_HOME_NOTE)."""
    return bool(spec.get("own_home")) and runtime != RUNTIME and not spec.get("environment")


def config_dir(worktree: Path | str) -> Path:
    return Path(worktree) / CONFIG_DIR


def gh_dir(worktree: Path | str) -> Path:
    return Path(worktree) / GH_DIR


def isolated(run: Mapping, runtime: str, env: Mapping[str, str], worktree: Path | str) -> bool:
    """Whether the agent of claim ``run`` on ``runtime`` with ``env`` runs in a config dir of its own (``wanted``, and
    CLAUDE_CONFIG_DIR is that dir), so it gets the MCP servers the daemon names alone (``mcp_config``)."""
    configured = env.get("CLAUDE_CONFIG_DIR") or ""
    return wanted(run, runtime) and bool(configured) and Path(configured) == config_dir(worktree)


def mcp_config(servers: Mapping[str, dict]) -> list[str]:
    """The arguments of a ``claude`` command that give the session ``servers`` and no other MCP server."""
    given = ["--mcp-config", json.dumps({"mcpServers": dict(servers)})] if servers else []
    return [*given, STRICT_MCP]


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


def _expires_at(credentials: str) -> float | None:
    """When the access token of ``credentials`` expires, in seconds since the epoch; None when it does not say."""
    try:
        oauth = json.loads(credentials).get(OAUTH_KEY)
    except (ValueError, AttributeError):
        return None
    value = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    return value / 1000.0 if isinstance(value, int | float) and not isinstance(value, bool) else None


def expiring(credentials: str, now: float | None = None) -> bool:
    """Whether the access token of ``credentials`` expires within REFRESH_BEFORE, or has expired."""
    expires = _expires_at(credentials)
    return expires is not None and expires - (time.time() if now is None else now) < REFRESH_BEFORE


def post_token(body: dict) -> dict:
    """POST ``body`` to TOKEN_URL as Claude Code does; the JSON it answers. RefreshFailed naming the HTTP status and
    the OAuth error code, never a token, when it fails."""
    request = urllib.request.Request(
        TOKEN_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "evo-agents"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REFRESH_TIMEOUT) as answer:  # noqa: S310 (a fixed https URL)
            return json.loads(answer.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            code = json.loads(exc.read().decode()).get("error")
        except (ValueError, AttributeError, OSError):
            code = None
        said = f" ({code})" if isinstance(code, str) and code.isprintable() and len(code) < 80 else ""
        raise RefreshFailed(f"{TOKEN_URL} answered HTTP {exc.code}{said}") from None
    except (OSError, ValueError) as exc:
        raise RefreshFailed(f"{TOKEN_URL} did not answer: {type(exc).__name__}") from None


def _refreshed(credentials: str, answer: Mapping, now: float) -> str:
    """``credentials`` with the tokens of ``answer``, the token endpoint's, in place of its own."""
    data = json.loads(credentials)
    oauth = data[OAUTH_KEY]
    token, lifetime = answer.get("access_token"), answer.get("expires_in")
    if not isinstance(token, str) or not token or not isinstance(lifetime, int | float):
        raise RefreshFailed(f"{TOKEN_URL} answered without an access token and its lifetime")
    oauth["accessToken"] = token
    oauth["expiresAt"] = int((now + float(lifetime)) * 1000)
    if isinstance(answer.get("refresh_token"), str) and answer["refresh_token"]:
        oauth["refreshToken"] = answer["refresh_token"]
    if isinstance(answer.get("refresh_token_expires_in"), int | float):
        oauth["refreshTokenExpiresAt"] = int((now + float(answer["refresh_token_expires_in"])) * 1000)
    if isinstance(answer.get("scope"), str) and answer["scope"].strip():
        oauth["scopes"] = answer["scope"].split()
    return json.dumps(data)


def _write_keeping_mode(path: Path, text: str) -> None:
    """Write ``text`` over ``path``, whole or not at all, with the mode ``path`` has."""
    mode = path.stat().st_mode & 0o777
    part = path.with_name(path.name + ".evo-part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


class _Lock:
    """An exclusive flock of ``path``, which the runs of a daemon (threads of one process, each its own open file)
    take one at a time."""

    def __init__(self, path: Path | None):
        self.path, self.fd = path, None

    def __enter__(self):
        if self.path is not None:
            import fcntl

            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)  # closing the file releases its lock
            self.fd = None


def refresh_owner(
    env: Mapping[str, str], lock: Path | None, post: Callable[[dict], dict] | None = None, now: float | None = None
) -> bool:
    """Refresh the owner's login in the credentials file of ``owner_dir`` when its access token expires within
    REFRESH_BEFORE (see the module), under the lock of file ``lock``; whether it did. RefreshFailed when it should and
    could not, and the file is not fresh by now either. ``post`` sends the request (tests hand their own)."""
    path = owner_dir(env) / CREDENTIALS_FILE
    with _Lock(lock):
        try:
            credentials = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False  # no file: the keychain item, or no login, as before
        if not expiring(credentials, now):
            return False
        try:
            oauth = json.loads(credentials)[OAUTH_KEY]
            refresh = oauth.get("refreshToken")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RefreshFailed(f"{path} holds no {OAUTH_KEY} login to refresh") from None
        if not isinstance(refresh, str) or not refresh:
            raise RefreshFailed(f"the login in {path} has no refresh token")
        scopes = oauth.get("scopes")
        scopes = scopes if isinstance(scopes, list) and scopes and all(isinstance(x, str) for x in scopes) else SCOPES
        body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": CLIENT_ID,
            "scope": " ".join(scopes),
        }
        try:
            answer = (post_token if post is None else post)(body)
            fresh = _refreshed(credentials, answer, time.time() if now is None else now)
        except RefreshFailed:
            try:  # the owner's Claude Code may have refreshed it meanwhile, spending the same refresh token
                if not expiring(path.read_text(encoding="utf-8"), now):
                    return False
            except (OSError, UnicodeDecodeError):
                pass
            raise
        _write_keeping_mode(path, fresh)
        return True


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
    lock: Path | None = None,
    post: Callable[[dict], dict] | None = None,
) -> str | None:
    """Write the config dir ``directory`` for an agent that starts now (see the module): ``leased`` says a lease of
    the run sets CLAUDE_CODE_OAUTH_TOKEN, ``owner_env`` is the daemon's environment, whose config dir has the owner's
    login, refreshed first under the lock of file ``lock`` when it expires soon (``refresh_owner``). Where the
    agent's login comes from; None when there is none (``REFUSAL``). OSError when the dir is not written,
    RefreshFailed when the owner's login expires soon and was not refreshed."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / ".gitignore").write_text("*\n", encoding="utf-8")
    _write_private(directory / GLOBAL_FILE, _global_config(directory, server))
    (directory.parent / Path(GH_DIR).name).mkdir(mode=0o700, exist_ok=True)
    copy = directory / CREDENTIALS_FILE
    if leased:
        copy.unlink(missing_ok=True)
        return f"the lease's {OAUTH_TOKEN}"
    own = owner_dir(owner_env).resolve() == directory.resolve()
    refreshed = False if own else refresh_owner(owner_env, lock, post)
    found = None if own else owner_login(owner_env, keychain)
    if found is None:
        copy.unlink(missing_ok=True)
        return None
    credentials, source = found
    _write_private(copy, without_refresh(credentials))
    return f"a copy of {source}" + (", refreshed first: its access token expired within the hour" if refreshed else "")


def note(directory: Path, login: str) -> str:
    """What the run's log says once of the agent's config dir."""
    return (
        f"Claude Code runs with a config dir of its own, {directory}, under a profile that is not the project's "
        f"default: none of the owner's user settings, hooks, plugins, skills, agents, CLAUDE.md or memory load. Its "
        f"login is {login}."
    )
