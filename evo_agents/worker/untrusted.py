"""Commands of code the worker does not trust: what a judge run runs of the change it judges, the plan's verify commands
and the project's hidden checks (``evo_agents.worker.run.JudgeRun``).

- ``scrubbed_env``: the environment such a command gets is the daemon's without the worker's own variables (EVO_*:
  no EVO_WORKER_HOME, no EVO_RUN_ID), git's configuration of the run's leases (GIT_CONFIG_*), the ssh agent, any
  variable whose name says it holds a credential (SECRET_NAME) or whose value is a URL with a password, and any value a
  run of this daemon holds as a lease. Nothing in it leads to the worker's token or to a run's credentials.
- ``start``: the command reaches ``/bin/sh -s`` on its standard input, never in an argument, so ``ps`` and
  ``/proc/<pid>/cmdline`` show ``/bin/sh -s`` alone; it runs in a session and process group of its own, and its
  environment carries a marker of its own (MARKER_VARIABLE).
- ``kill_leftovers``: once the command ended, was stopped or ran out of time, every process it left is killed: its
  process group and its session, and any process of this user whose environment holds its marker, which a process
  that left the session keeps. On Linux /proc gives a process's session and environment; elsewhere ``ps -E`` shows
  the environment of a process this user may read. [Inference] A process that both leaves the session and clears its
  environment, or on macOS one of a binary the system protects, escapes the marker; the judge's verdict does not
  depend on it (``evo_agents.hub.judge.verdict_from_message``).

Standard library only.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import signal
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

SHELL = "/bin/sh"
MARKER_VARIABLE = "EVO_UNTRUSTED_MARK"  # a random value per command: each process of it inherits it
SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASSW(?:OR)?D|PASSPHRASE|API_?KEY|PRIVATE_?KEY|CREDENTIAL|ACCESS_?KEY|COOKIE",
    re.IGNORECASE,
)
DROPPED_PREFIXES = ("EVO_", "GIT_CONFIG", "GIT_ASKPASS", "SSH_ASKPASS")
DROPPED = frozenset({"SSH_AUTH_SOCK", "GPG_AGENT_INFO", "SUDO_ASKPASS", "KUBECONFIG", "DOCKER_AUTH_CONFIG"})
URL_PASSWORD = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
SWEEP_ROUNDS = 5  # passes of kill_leftovers, as a process may fork while the one before is killed
PS_TIMEOUT = 10.0


def scrubbed_env(base: Mapping[str, str], held: Iterable[str] = ()) -> dict[str, str]:
    """``base`` without what leads to the worker's token or a run's credentials (see the module's docstring);
    ``held`` are the lease values this daemon holds now."""
    values = [value for value in held if value]
    env: dict[str, str] = {}
    for key, value in base.items():
        if key.startswith(DROPPED_PREFIXES) or key in DROPPED or SECRET_NAME.search(key):
            continue
        if URL_PASSWORD.search(value) or any(secret in value for secret in values):
            continue
        env[key] = value
    return env


def new_marker() -> str:
    return secrets.token_hex(16)


async def start(command: str, *, cwd: Path, env: Mapping[str, str], marker: str) -> asyncio.subprocess.Process:
    """``command`` run by ``/bin/sh -s`` in ``cwd``, given on its standard input, which then closes; stdout and stderr
    together on a pipe; a session of its own; ``marker`` in its environment."""
    proc = await asyncio.create_subprocess_exec(
        SHELL,
        "-s",
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**env, MARKER_VARIABLE: marker},
        start_new_session=True,
    )
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.write(command.encode() + b"\n")
        await proc.stdin.drain()
    proc.stdin.close()
    return proc


def _linux_leftovers(session: int, marker: bytes) -> set[int]:
    found: set[int] = set()
    mine = os.getpid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        pid = int(entry.name)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            fields = stat[stat.rindex(")") + 2 :].split()  # after the command's name, which may hold spaces
            if session in (int(fields[2]), int(fields[3])):  # its process group, or its session
                found.add(pid)
                continue
            if marker in Path(f"/proc/{pid}/environ").read_bytes():
                found.add(pid)
        except (OSError, ValueError, IndexError):
            continue
    return found


def _ps_leftovers(session: int, marker: str) -> set[int]:
    found: set[int] = set()
    try:
        done = subprocess.run(
            ["ps", "-E", "-ww", "-x", "-o", "pid=", "-o", "pgid=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return found
    mine = os.getpid()
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, group = int(parts[0]), int(parts[1])
        if pid != mine and (group == session or marker in line):
            found.add(pid)
    return found


def leftovers(session: int, marker: str) -> set[int]:
    """The processes of this user that ``session``'s command left: in its process group or session, or with its
    marker in their environment."""
    if Path("/proc/self/environ").exists():
        return _linux_leftovers(session, f"{MARKER_VARIABLE}={marker}".encode())
    return _ps_leftovers(session, f"{MARKER_VARIABLE}={marker}")


def _kill_leftovers(session: int, marker: str) -> int:
    killed = 0
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(session, signal.SIGKILL)
    for _ in range(SWEEP_ROUNDS):
        found = leftovers(session, marker)
        if not found:
            break
        for pid in found:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
                killed += 1
    return killed


async def kill_leftovers(session: int, marker: str) -> int:
    """Kill every process the command that led ``session`` left (see the module's docstring); how many."""
    return await asyncio.to_thread(_kill_leftovers, session, marker)
