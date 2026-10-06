"""The agents a daemon leaves behind when it dies, and how the next daemon stops them.

A run's agent runs in a process group of its own (the rule of ``evo_agents.worker.adapter``), so it outlives a daemon
killed with SIGKILL, by a person or by a service manager whose stop timeout ran out while the daemon waited for its
runs. Nothing reads such an agent any more, yet it can go on working in its worktree, and a new attempt of its run may
start next to it. So the daemon notes, in ``runs/<run>/agent.json`` (``WorkerHome.save_agent``), the agent each run
starts: the pid of the leader of its process group, the group, and the leader's start time as ``ps`` prints it
(``describe``); the file stays until the run ends on this machine. A daemon that starts treats every such file as an
orphan, asks the hub about those runs in its first heartbeat (``daemon.Daemon``), and stops the group (``stop``):
SIGTERM to the group and to the groups of the leader's descendants, then SIGKILL to what is left after STOP_GRACE
seconds. A leader whose pid now belongs to a process that started at another time is not the agent: nothing is sent.

Standard library only.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from datetime import datetime, timezone

from evo_agents.worker.runtimes.common import descendants, kill_group

STOP_GRACE = 10.0  # seconds an orphan's process group has to end on SIGTERM before SIGKILL
POLL = 0.1
PS_TIMEOUT = 10.0


def process_start(pid: int) -> str | None:
    """When process ``pid`` started, as ``ps -o lstart=`` prints it (to the second); None when there is no such
    process or ``ps`` cannot tell."""
    if pid <= 1:
        return None
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(done.stdout.split())
    return text if done.returncode == 0 and text else None


def describe(pid: int | None) -> dict:
    """What agent.json holds for an agent whose process group ``pid`` leads (None when the adapter does not say):
    ``pid``, ``pgid`` and ``started``, each null when unknown, and when this was noted."""
    pgid = started = None
    if isinstance(pid, int) and pid > 1:
        with contextlib.suppress(OSError):
            pgid = os.getpgid(pid)
        started = process_start(pid)
    else:
        pid = None
    return {
        "pid": pid,
        "pgid": pgid if pgid is not None and pgid > 1 else None,
        "started": started,
        "noted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def group_alive(pgid: int) -> bool:
    """Whether the process group ``pgid`` has a process this user may signal."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _target(agent: dict) -> tuple[int | None, str | None]:
    """The process group to stop for ``agent`` (an agent.json), or None with why not."""
    pid, pgid, started = agent.get("pid"), agent.get("pgid"), agent.get("started")
    if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
        return None, "no process group was noted"
    if isinstance(pid, int) and pid > 1:
        now = process_start(pid)
        if now is not None and isinstance(started, str) and now != started:
            return None, "the pid now belongs to another process"
    if not group_alive(pgid):
        return None, "it had ended already"
    return pgid, None


async def stop(agent: dict, grace: float = STOP_GRACE) -> str:
    """Stop the process group of the agent ``agent`` (an agent.json) describes, its leader's descendants in other
    groups included: SIGTERM, then SIGKILL after ``grace`` seconds. What happened, as a phrase for the log."""
    pgid, why = await asyncio.to_thread(_target, agent)
    if pgid is None:
        return f"nothing to stop: {why}"
    leader = agent.get("pid") if isinstance(agent.get("pid"), int) else pgid
    tools = await asyncio.to_thread(descendants, leader)
    groups = {pgid, *(group for _, group in tools if group > 1)}
    for group in groups:
        kill_group(group, signal.SIGTERM)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace
    while loop.time() < deadline:
        if not any(group_alive(group) for group in groups) and not any(_alive(pid) for pid, _ in tools):
            return f"process group {pgid} stopped on SIGTERM"
        await asyncio.sleep(POLL)
    for group in groups:
        kill_group(group, signal.SIGKILL)
    for pid, _ in tools:
        if pid > 1 and pid != os.getpid():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
    return f"process group {pgid} killed with SIGKILL after {grace:g}s"
