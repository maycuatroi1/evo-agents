"""The guard of private names in a member's Claude Code session (core plugin guard, ``docs/guard.md``).

- ``take``, at SessionStart: the hub's policy (GET /v1/guard/policy) kept in ``<hub dir>/guard-policy.json``; a hub
  without the plugin answers 404 and the file is removed, so nothing is guarded.
- ``evo-agents hub hook pre-tool-use`` (PreToolUse of Bash): a command that commits, pushes or writes text with ``gh``
  (``pr``, ``issue``, ``release``, ``api``) to a guarded repo is denied when its text, a file it names for a message
  or a body, or for a push the commits it sends, holds a private name. The reason names each name as the session
  wrote it, so the agent rewrites it with a fictional one. A command the hook cannot read for a repo goes on: the
  hook guards what it can see and never blocks a session for its own failure.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

from evo_agents import guard_check
from evo_agents.hub.client import Hub, HubError, hub_dir
from evo_agents.private_names import Policy

POLICY_FILE = "guard-policy.json"
COMMIT = re.compile(r"(?:^|[\s;&|(])git(?:\s+-C\s+\S+|\s+-c\s+\S+)*\s+commit\b")
PUSH = re.compile(r"(?:^|[\s;&|(])git(?:\s+-C\s+\S+|\s+-c\s+\S+)*\s+push\b(?P<rest>[^;&|\n]*)")
GH = re.compile(r"(?:^|[\s;&|(])gh\s+(?:pr|issue|release|api)\b")
CD = re.compile(r"(?:^|[;&|(]\s*)cd\s+(?P<dir>\"[^\"]+\"|'[^']+'|[^\s;&|]+)")
GIT_DIR = re.compile(r"\bgit\s+-C\s+(?P<dir>\"[^\"]+\"|'[^']+'|\S+)")
REPO_FLAG = re.compile(r"(?:--repo[= ]|-R\s*)(?P<repo>[\w.-]+/[\w.-]+|\S+://\S+)")
FILE_FLAG = re.compile(r"(?:--body-file|--notes-file|--file|-F)[= ](?P<file>\"[^\"]+\"|'[^']+'|[^\s;&|]+)")


def policy_path() -> Path:
    return hub_dir() / POLICY_FILE


def take(hub: Hub) -> str | None:
    """Keep the hub's policy on this machine; a line for the session when the hub answered one this release cannot
    read, else None. A hub that does not answer leaves the policy kept here, which goes on applying."""
    path = policy_path()
    try:
        data = hub.call("GET", "/v1/guard/policy")
    except HubError as exc:
        if exc.status == 404:  # a hub without the guard plugin's route
            path.unlink(missing_ok=True)
        return None
    try:
        Policy.from_json(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return f"guard policy not kept: {exc}"
    return None


def _unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value


def _where(command: str, cwd: str) -> str:
    """The directory the command's git or gh runs in: the last ``cd`` before it, or ``git -C``, from ``cwd``."""
    found = cwd
    for match in CD.finditer(command):
        found = os.path.join(found, os.path.expanduser(_unquote(match["dir"])))
    if match := GIT_DIR.search(command):
        found = os.path.join(found, os.path.expanduser(_unquote(match["dir"])))
    return found


def _files(command: str, where: str) -> list[str]:
    texts = []
    for match in FILE_FLAG.finditer(command):
        name = os.path.expanduser(_unquote(match["file"]))
        if name == "-":
            continue
        try:
            texts.append(Path(where, name).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def _push_source(rest: str) -> str:
    """The commit a ``git push`` sends: the source of its first refspec after the remote, else HEAD."""
    try:
        words = [w for w in shlex.split(rest) if not w.startswith("-")]
    except ValueError:
        return "HEAD"
    if len(words) < 2:
        return "HEAD"
    return words[1].split(":", 1)[0].lstrip("+") or "HEAD"


def check_command(policy: Policy, command: str, cwd: str) -> str | None:
    """The reason to deny ``command``, or None to let it run."""
    committing, pushing, writing = COMMIT.search(command), PUSH.search(command), GH.search(command)
    if not (committing or pushing or writing):
        return None
    where = _where(command, cwd)
    named = REPO_FLAG.search(command) if writing else None
    url = named["repo"] if named and policy.guards(named["repo"]) else None
    if url is None and (committing or pushing or not named):  # gh without --repo writes to the checkout's repo
        url = guard_check.guarded_remote(policy, where)
    if url is None:
        return None
    texts = [command, *_files(command, where)]
    if pushing:
        words = pushing["rest"].split()
        remote = next((w for w in words if not w.startswith("-")), None)
        texts.append(guard_check.outgoing(where, _push_source(pushing["rest"]), remote=remote))
    try:
        guard_check.check_text(policy, "\n".join(texts), f"this command (it writes to {url})")
    except guard_check.Refused as exc:
        return str(exc)
    return None


def cmd_hook_pre_tool_use(args) -> int:
    """PreToolUse of Bash: deny a command that would put a private name in a guarded repo; always exit 0."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool_input = payload.get("tool_input") or {}
        command = tool_input.get("command") if payload.get("tool_name") == "Bash" else None
        policy = guard_check.load(policy_path()) if isinstance(command, str) else None
        reason = check_command(policy, command, str(payload.get("cwd") or os.getcwd())) if policy else None
    except Exception as exc:  # the hook never blocks a session for its own failure
        print(f"evo-hub: the guard of private names did not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if reason:
        decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}
        print(json.dumps({"hookSpecificOutput": decision}, ensure_ascii=False))
    return 0
