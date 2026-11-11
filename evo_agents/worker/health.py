"""What the daemon's heartbeat says of the machine's health (plan hub-steward, criterion a3): the free disk of the drive
that holds its worktrees, and whether each runtime is signed in.

A runtime's login comes from its own status command, run with a timeout: ``claude auth status`` (JSON, ``loggedIn``),
``codex login status`` (its exit status), ``opencode auth list`` (the credentials and environment variables it
counts). Only the exit status and the shape of the answer are read: no credential is read, kept or sent, and the
answer itself never leaves this module. ``ok``: signed in; ``unauthorized``: the command says it is not; ``missing``:
the program is not on PATH; ``unknown``: no answer within LOGIN_TIMEOUT, or one this module cannot read.

A worker whose runs authenticate with a model credential the hub leases them has no login of its own to show.
``leased``: the machine's login is not ok, but the last agent of the runtime on this worker ran on such a lease and its
account did not fail to authenticate (``with_leases``). The daemon keeps that per runtime in LEASED_FILE of its home,
so a restart does not report the machine's login again; an agent whose lease was refused, or the next one with no
lease, puts the machine's login back.

The daemon looks at the logins with the runtimes, every ``daemon.RUNTIMES_EVERY`` seconds in a thread, so a heartbeat
never waits for them; the disk is read at each look, which costs one statfs. Standard library only.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from evo_agents.hub import runs

LOGIN_TIMEOUT = 10.0  # seconds a runtime's status command may take
LEASED = "leased"  # what the heartbeat says of a runtime whose runs authenticate with the hub's lease
LEASED_FILE = "leased-logins.json"  # runtime -> whether its last agent authenticated with a lease
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


def with_leases(found_logins: Mapping[str, str], leased: Mapping[str, bool]) -> dict[str, str]:
    """``found_logins`` with LEASED for each runtime whose login is not ok and whose last agent authenticated with a
    lease (``leased``)."""
    return {name: LEASED if state != "ok" and leased.get(name) else state for name, state in found_logins.items()}


def load_leased(root: Path) -> dict[str, bool]:
    """What LEASED_FILE in ``root`` says, by runtime; empty when it is missing or unreadable."""
    try:
        found = json.loads((Path(root) / LEASED_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(found, dict):
        return {}
    return {str(name): value for name, value in found.items() if isinstance(value, bool)}


def save_leased(root: Path, leased: Mapping[str, bool]) -> None:
    """Write ``leased`` to LEASED_FILE in ``root``: runtime names and booleans, no credential."""
    (Path(root) / LEASED_FILE).write_text(json.dumps(dict(sorted(leased.items()))))


def report(worktrees: Path, found_logins: dict[str, str], reserve_gb: float | None = None) -> dict:
    """The heartbeat's ``health``: the free disk of ``worktrees``' drive, with the gigabytes the daemon keeps free
    there (``reserve_gb``, under which the hub gives it no run), and the logins last found."""
    found = disk(worktrees)
    if found is not None and reserve_gb is not None:
        found["reserve_gb"] = round(float(reserve_gb), 2)
    return {"disk": found, "logins": dict(found_logins)}


__all__ = [
    "BINARIES",
    "LEASED",
    "LEASED_FILE",
    "LOGIN_TIMEOUT",
    "disk",
    "load_leased",
    "login",
    "logins",
    "report",
    "save_leased",
    "with_leases",
]
