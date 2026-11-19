"""What is left of a command of a claude-code session once Claude Code stopped it, and how the worker ends it.

Claude Code 2.1 runs each Bash command in a shell of its own: a child of the CLI that leads a process group of its own
and writes the command's output to ``<tmp>/claude-<uid>/<cwd>/<session>/tasks/<task id>.output`` (its fd 1 and 2;
Claude Code 2.1.319, in the foreground and the background alike). When the CLI stops a command, because the agent
asked (TaskStop) or by itself (``task_updated`` ``killed``, ``task_notification`` ``stopped``), a tool that runs its
children in process groups of their own outlives it: turbo does so for the task of each package. On a worker of 14 GB
a stopped ``pnpm check`` left three or four ``tsc`` of a typecheck running, 4 GB in all, for close to an hour after
the agent was told it had stopped (2026-11-18), and every run of the machine stalled until a person killed them.

``TaskReaper`` keeps, for each command the CLI runs, the processes under its shell, each with its start time: ``note``
reads the processes under the CLI (``ps``) and, for each, the output file it writes (``/proc/<pid>/fd/1``), and the
adapter calls it every SNAPSHOT_EVERY seconds while a command runs and as one stops. Once the CLI reports a command
stopped, ``reap(task)`` kills the processes noted for it that still run (the same pid, started at the same time),
what runs under them now and their process groups: twice, REAP_AFTER seconds after the stop, for a tool that starts
one more child as it shuts down. ``reap_all`` does the same for every command at the end of the session, once the CLI
exited: a command that ended by itself but left a process behind (``nohup``, ``&``) ends with the session. Neither
signals the CLI's process group nor the daemon's.

Linux only: elsewhere there is no /proc to tell which command a process runs for, and nothing is noted; the agent's
whole process tree is still killed at the end of the session (``common.QueueAdapter``). A cgroup would hold even what
left the tree, but the CLI, not the worker, starts each command, so the worker could only put the whole session in
one: what ``reap_all`` and that kill cover already.

Standard library only.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

SNAPSHOT_EVERY = 15.0  # seconds between two readings of the commands that run
REAP_AFTER = (2.0, 10.0)  # seconds after a command stopped when what is left of it is killed
STOPPED = frozenset({"killed", "stopped"})  # task_updated says killed, task_notification stopped, for one stop
ENDED = frozenset({"completed", "failed", "killed", "stopped"})
TASK_OUTPUT = re.compile(r"/tasks/([A-Za-z0-9_-]+)\.output$")
PS_TIMEOUT = 10.0


@dataclass(frozen=True)
class Process:
    parent: int
    group: int
    started: str  # as ``ps -o lstart=`` prints it


def processes() -> dict[int, Process]:
    """Every process of the machine that runs, as ``ps`` lists them (a zombie, which waits for its parent to read
    its end, does not run); empty when it cannot."""
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,lstart="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, Process] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) > 4 and all(word.isdigit() for word in fields[:3]) and not fields[3].startswith("Z"):
            found[int(fields[0])] = Process(int(fields[1]), int(fields[2]), " ".join(fields[4:]))
    return found


def task_of(pid: int) -> str | None:
    """The id of the command of Claude Code whose output file process ``pid`` writes; None when it writes none, or
    it cannot be read."""
    for fd in (1, 2):
        with contextlib.suppress(OSError):
            found = TASK_OUTPUT.search(os.readlink(f"/proc/{pid}/fd/{fd}"))
            if found:
                return found.group(1)
    return None


def under(table: Mapping[int, Process], roots) -> set[int]:
    """The processes of ``roots`` that run, with every process under them in ``table``."""
    children: dict[int, list[int]] = {}
    for pid, process in table.items():
        children.setdefault(process.parent, []).append(pid)
    found: set[int] = set()
    stack = [pid for pid in roots if pid in table]
    while stack:
        pid = stack.pop()
        if pid not in found:
            found.add(pid)
            stack += children.get(pid, ())
    return found


def _signal_group(group: int) -> None:
    with contextlib.suppress(OSError):
        os.killpg(group, signal.SIGKILL)


def _signal(pid: int) -> None:
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


@dataclass
class TaskReaper:
    """The processes of each command of one session (the module's docstring). ``root`` gives the pid of the CLI,
    whose process group the launcher made; the others are the module's functions, for a test to replace."""

    root: Callable[[], int | None]
    table: Callable[[], dict[int, Process]] = processes
    task_of: Callable[[int], str | None] = task_of
    kill_group: Callable[[int], None] = _signal_group
    kill: Callable[[int], None] = _signal
    enabled: bool = field(default_factory=lambda: Path("/proc/self/fd").is_dir())
    noted: dict[str, dict[int, str]] = field(default_factory=dict)  # task id -> pid -> its start
    running: set[str] = field(default_factory=set)  # the commands the CLI runs, from its events
    stopped: set[str] = field(default_factory=set)  # the commands it reported stopped
    lock: threading.Lock = field(default_factory=threading.Lock)  # note and reap run in threads of their own

    def see(self, subtype: str, data) -> list[str]:
        """Follow a system message of the CLI; the commands it reports stopped, whose processes ``reap`` kills."""
        if not isinstance(data, Mapping):
            return []
        task = data.get("task_id")
        if not isinstance(task, str) or not task:
            return []
        if subtype == "task_started":
            self.running.add(task)
            return []
        status = data.get("status") if subtype == "task_notification" else None
        if subtype == "task_updated" and isinstance(data.get("patch"), Mapping):
            status = data["patch"].get("status")
        if status in ENDED:
            self.running.discard(task)
        if status not in STOPPED or not self.enabled or task in self.stopped:
            return []
        self.stopped.add(task)
        return [task]

    def note(self) -> None:
        """Note the processes under the shell of each command that runs (the module's docstring)."""
        root = self.root()
        if not self.enabled or root is None:
            return
        table = self.table()
        if root not in table:
            return
        with self.lock:
            for pid in under(table, [root]) - {root}:
                task = self.task_of(pid)
                if task is not None:
                    noted = self.noted.setdefault(task, {})
                    for each in under(table, [pid]):
                        noted[each] = table[each].started

    def reap(self, task: str, last: bool = True) -> int:
        """Kill what is left of the command ``task``; the processes signalled. ``last`` forgets what was noted."""
        with self.lock:
            found = self.noted.pop(task, None) if last else self.noted.get(task)
            noted = dict(found or {})
        return self._kill(noted)

    def reap_all(self) -> int:
        """Kill what is left of every command noted, at the end of the session; the processes signalled."""
        noted: dict[int, str] = {}
        with self.lock:
            for processes_of_task in self.noted.values():
                noted.update(processes_of_task)
            self.noted.clear()
            self.running.clear()
        return self._kill(noted)

    def _kill(self, noted: Mapping[int, str]) -> int:
        if not noted:
            return 0
        table = self.table()
        alive = [pid for pid, started in noted.items() if pid in table and table[pid].started == started]
        root = self.root()
        victims = under(table, alive) - {root, os.getpid()}
        spared = {os.getpgrp()}
        if root is not None:
            spared |= {root} | ({table[root].group} if root in table else set())
        for group in sorted({table[pid].group for pid in victims} - spared):
            self.kill_group(group)
        for pid in sorted(victims):
            self.kill(pid)
        return len(victims)
