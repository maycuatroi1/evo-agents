"""What the daemon's heartbeat says of the machine's health (plan hub-steward, criterion a3): the free disk of the drive
that holds its worktrees, and whether each runtime is signed in.

A runtime's login comes from its own status command, run with a timeout: ``claude auth status`` (JSON, ``loggedIn``),
``codex login status`` (its exit status), ``opencode auth list`` (the credentials and environment variables it
counts). Only the exit status and the shape of the answer are read: no credential is read, kept or sent, and the
answer itself never leaves this module. ``ok``: signed in; ``unauthorized``: the command says it is not; ``missing``:
the program is not on PATH; ``unknown``: no answer within LOGIN_TIMEOUT, or one this module cannot read.

The daemon looks at the logins with the runtimes, every ``daemon.RUNTIMES_EVERY`` seconds in a thread, so a heartbeat
never waits for them; the disk is read at each look, which costs one statfs. Standard library only.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from evo_agents.hub import runs

LOGIN_TIMEOUT = 10.0  # seconds a runtime's status command may take
BINARIES = {"claude-code": "claude", "codex": "codex", "opencode": "opencode"}
STATUS_COMMANDS = {
    "claude-code": ("auth", "status", "--json"),
    "codex": ("login", "status"),
    "opencode": ("auth", "list"),
}
GB = 1024**3
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_OPENCODE_COUNT = re.compile(r"(\d+)\s+(?:credentials?|environment variables?)\b", re.I)


def disk(path: Path) -> dict | None:
    """The free space of the drive that holds ``path`` (its nearest existing parent): {free_gb, free_pct}; None when
    it cannot be read."""
    where = Path(path)
    while not where.exists() and where != where.parent:
        where = where.parent
    try:
        usage = shutil.disk_usage(where)
    except OSError:
        return None
    pct = 100.0 * usage.free / usage.total if usage.total else 0.0
    return {"free_gb": round(usage.free / GB, 2), "free_pct": round(pct, 1)}


def _claude(done: subprocess.CompletedProcess) -> str:
    try:
        answer = json.loads(done.stdout or "")
    except ValueError:
        answer = None
    if isinstance(answer, dict) and isinstance(answer.get("loggedIn"), bool):
        return "ok" if answer["loggedIn"] else "unauthorized"
    return "unauthorized" if done.returncode == 1 else "unknown"


def _codex(done: subprocess.CompletedProcess) -> str:
    return "ok" if done.returncode == 0 else "unauthorized"


def _opencode(done: subprocess.CompletedProcess) -> str:
    if done.returncode != 0:
        return "unknown"
    text = _ANSI.sub("", f"{done.stdout or ''}\n{done.stderr or ''}")
    counts = [int(found) for found in _OPENCODE_COUNT.findall(text)]
    if not counts:
        return "unknown"
    return "ok" if any(counts) else "unauthorized"


READERS: dict[str, Callable[[subprocess.CompletedProcess], str]] = {
    "claude-code": _claude,
    "codex": _codex,
    "opencode": _opencode,
}


def login(runtime: str, *, path: str | None = None, timeout: float = LOGIN_TIMEOUT) -> str:
    """The login of ``runtime`` on this machine, from its status command: ok, unauthorized, missing or unknown."""
    found = shutil.which(BINARIES[runtime], path=path)
    if found is None:
        return "missing"
    try:
        done = subprocess.run(
            [found, *STATUS_COMMANDS[runtime]],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return "unknown"
    return READERS[runtime](done)


def logins(names: Iterable[str] = runs.RUNTIMES, *, path: str | None = None) -> dict[str, str]:
    """The login of each runtime of ``names`` this module knows how to ask."""
    return {name: login(name, path=path) for name in names if name in BINARIES}


def report(worktrees: Path, found_logins: dict[str, str]) -> dict:
    """The heartbeat's ``health``: the free disk of ``worktrees``' drive and the logins last found."""
    return {"disk": disk(worktrees), "logins": dict(found_logins)}


__all__ = ["BINARIES", "LOGIN_TIMEOUT", "disk", "login", "logins", "report"]
