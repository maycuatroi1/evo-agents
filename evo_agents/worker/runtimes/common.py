"""What the three runtime adapters share.

- The versions each adapter was checked with, and ``detect_runtime``, which reports a runtime unsupported, with the
  reason, when its binary is missing or older than that, or when the SDK the adapter drives it with is missing or
  older.
- ``new_session_argv`` and ``write_launcher``: a runtime is started through a tiny Python launcher that makes it the
  leader of a session of its own (``setsid``) and writes its pid to a file before it executes the runtime. The SDKs
  start their runtime themselves and take no ``start_new_session``; the launcher gives them the rule of the adapter
  interface anyway: a Ctrl-C at the daemon's terminal does not reach the agent, and the daemon can kill the agent's
  process group, once the agent ended or did not stop when interrupted, and with it the processes the runtime started
  in groups of their own (``descendants``, ``kill_tree``).
- ``QueueAdapter``: an agent driven by a task of its own, which puts the agent's events on a queue that ``events``
  reads, and ends with an ``Outcome``.
- ``run_setting``: the model and the reasoning effort of a run, from the run as the claim answered it (``model``,
  ``effort``), else from ``EVO_WORKER_<RUNTIME>_MODEL`` and ``EVO_WORKER_<RUNTIME>_EFFORT`` in the daemon's
  environment, else the runtime's own default.
- The time cap of a run the night shift queued: ``QueueAdapter`` interrupts the agent once it has used what is left of
  the budget's ``max_seconds``, and its outcome says the run stopped at its time cap (``cap`` ``time``). Codex reports
  no cost, so this is the cap that stops a Codex run.
- The bodies of the events, in the shapes of the Agent Client Protocol's ``session/update`` (``docs/workers.md``).

Standard library only.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterable, Mapping
from importlib import metadata
from pathlib import Path

from evo_agents.hub import curator
from evo_agents.worker.adapter import (  # noqa: F401 (AgentFinished: the adapters raise it from here)
    Adapter,
    AgentEvent,
    AgentFinished,
    Detection,
    Outcome,
    RunContext,
    probe_binary,
)

log = logging.getLogger("evo_agents.worker")

EXTRA = "evo-ak[worker]"
# Said to every agent, on top of its runtime's own system prompt: nobody answers it during a run.
HEADLESS_NOTE = (
    "You are running unattended on an evo-agents worker, on behalf of the owner of this machine. Nobody watches this "
    "session or answers questions while it runs: decide reasonably on your own and finish the task. The owner may "
    "send you messages while you work; take them into account."
)
INTERRUPT_TIMEOUT = 10.0  # seconds for the runtime to answer an interrupt request
KILL_AFTER = 20.0  # seconds an interrupted agent has to end before its process group is killed
CLOSE_TIMEOUT = 30.0  # seconds for an SDK client to close
MAX_ERROR_CHARS = 1000
SETTING_PREFIXES = {
    "claude-code": "EVO_WORKER_CLAUDE_CODE_",
    "opencode": "EVO_WORKER_OPENCODE_",
    "codex": "EVO_WORKER_CODEX_",
}
TOOL_KINDS = ("read", "edit", "delete", "move", "search", "execute", "think", "fetch", "switch_mode", "other")
PLAN_STATUSES = {"pending": "pending", "inprogress": "in_progress", "in_progress": "in_progress"}

_VERSION = re.compile(r"\s*v?(\d+(?:\.\d+)*)")
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# The launcher: a new session (so a process group of its own), its pid in the file of argv[1], then the runtime.
_NEW_SESSION = (
    "import os, sys\n"
    "try:\n"
    "    os.setsid()\n"
    "except OSError:\n"
    "    pass\n"
    "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
    "os.write(fd, str(os.getpid()).encode())\n"
    "os.close(fd)\n"
    "os.execv(sys.argv[2], sys.argv[2:])\n"
)


# Versions and detection


def version_tuple(text: str | None) -> tuple[int, ...] | None:
    """``(2, 1, 289)`` for ``"2.1.289"`` or ``"2.1.289 (Claude Code)"``; None when ``text`` starts with no number."""
    match = _VERSION.match(text or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def at_least(version: str | None, minimum: str) -> bool:
    found, floor = version_tuple(version), version_tuple(minimum)
    if found is None or floor is None:
        return False
    width = max(len(found), len(floor))
    return found + (0,) * (width - len(found)) >= floor + (0,) * (width - len(floor))


def package_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def detect_runtime(
    runtime: str, binary: str, minimum: str, *, packages: Iterable[tuple[str, str]] = (), path: str | None = None
) -> Detection:
    """``binary`` on PATH at ``minimum`` or newer, and each ``(distribution, minimum)`` of ``packages`` installed at
    that version or newer; unavailable with the reason otherwise."""
    probe = probe_binary(binary, path=path)
    if not probe.available:
        return probe
    if probe.version is None:
        return Detection(False, None, f"{binary} --version printed no version")
    if not at_least(probe.version, minimum):
        return Detection(
            False,
            probe.version,
            f"{runtime} {probe.version} is older than {minimum}, the oldest version this worker was checked with: "
            "upgrade it",
        )
    for distribution, floor in packages:
        installed = package_version(distribution)
        if installed is None:
            return Detection(
                False, probe.version, f"{distribution} is not installed: install the worker extra, {EXTRA}"
            )
        if not at_least(installed, floor):
            return Detection(
                False,
                probe.version,
                f"{distribution} {installed} is older than {floor}, the oldest version this worker was checked with: "
                f"upgrade the worker extra, {EXTRA}",
            )
    return probe


def which(binary: str, env: Mapping[str, str]) -> str:
    """The path of ``binary`` on the agent's PATH."""
    found = shutil.which(binary, path=env.get("PATH"))
    if found is None:
        raise RuntimeError(f"{binary} is not on PATH")
    return found


# Settings of a run


def run_setting(context: RunContext, runtime: str, key: str) -> str | None:
    """``key`` (``model`` or ``effort``) of the run, else ``EVO_WORKER_<RUNTIME>_<KEY>``, else None."""
    value = context.run.get(key) if isinstance(context.run, Mapping) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    value = context.env.get(SETTING_PREFIXES[runtime] + key.upper())
    return value.strip() if isinstance(value, str) and value.strip() else None


# The launcher and the process group


def new_session_argv(pid_file: Path, program: str, *args: str) -> list[str]:
    """The argv that runs ``program args`` as the leader of a new session, its pid written to ``pid_file`` first."""
    return [sys.executable or "python3", "-I", "-c", _NEW_SESSION, str(pid_file), program, *args]


def write_launcher(directory: Path, program: str, *, unset: Iterable[str] = ()) -> Path:
    """An executable ``directory/launch`` that runs ``program`` with its own arguments as ``new_session_argv`` does,
    the pid going to ``directory/pid``: for an SDK that takes the path of one executable. The variables in ``unset``
    are removed first, from whatever environment the SDK gives it (its own process's, as a rule)."""
    path = directory / "launch"
    command = " ".join(shlex.quote(part) for part in new_session_argv(directory / "pid", program))
    removed = "".join(f"unset {name}\n" for name in unset if _VARIABLE.match(name))
    path.write_text(f'#!/bin/sh\n{removed}exec {command} "$@"\n', encoding="utf-8")
    path.chmod(0o700)
    return path


def read_pid(pid_file: Path | None) -> int | None:
    if pid_file is None:
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 1 else None


def kill_group(pid: int | None, sig: int = signal.SIGKILL) -> None:
    """Signal the process group ``pid`` leads, unless it is the daemon's own."""
    if pid is None or pid <= 1:
        return
    with contextlib.suppress(OSError):
        if pid == os.getpgrp():
            return
        os.killpg(pid, sig)


def descendants(root: int | None) -> list[tuple[int, int]]:
    """``(pid, process group)`` of each process under ``root``, as ``ps`` lists them. A runtime may start its tools
    in process groups of their own (codex's commands lead theirs), which a signal to the runtime's group misses."""
    if root is None or root <= 1:
        return []
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    groups: dict[int, int] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 3 and all(field.isdigit() for field in fields):
            pid, parent, group = (int(field) for field in fields)
            children.setdefault(parent, []).append(pid)
            groups[pid] = group
    found, stack = [], [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            found.append((child, groups[child]))
            stack.append(child)
    return found


def kill_tree(processes: Iterable[tuple[int, int]]) -> None:
    """Kill the processes ``descendants`` found, and the process groups they were in (never the daemon's own)."""
    processes = list(processes)
    for group in {group for _, group in processes}:
        kill_group(group)
    for pid, _ in processes:
        if pid > 1 and pid != os.getpid():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


# Event bodies, as the session/update notifications of ACP shape them


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def message_chunk(text: str) -> AgentEvent:
    return AgentEvent("agent_message_chunk", {"content": text_block(text)})


def thought_chunk(text: str) -> AgentEvent:
    return AgentEvent("agent_thought_chunk", {"content": text_block(text)})


def tool_call(call_id: str, title: str, kind: str, status: str, raw_input=None) -> AgentEvent:
    body = {"toolCallId": call_id, "title": title, "kind": kind if kind in TOOL_KINDS else "other", "status": status}
    if raw_input is not None:
        body["rawInput"] = raw_input
    return AgentEvent("tool_call", body)


def tool_update(call_id: str, status: str, text: str | None = None, raw_output=None) -> AgentEvent:
    body: dict = {"toolCallId": call_id, "status": status}
    if text:
        body["content"] = [{"type": "content", "content": text_block(text)}]
    if raw_output is not None:
        body["rawOutput"] = raw_output
    return AgentEvent("tool_call_update", body)


def plan_status(value) -> str:
    key = str(value or "").replace("-", "_").lower()
    return PLAN_STATUSES.get(key, "completed" if key in ("completed", "cancelled", "done") else "pending")


def plan(entries: Iterable[tuple[str, str, str]]) -> AgentEvent:
    """``entries`` as (content, status, priority)."""
    return AgentEvent(
        "plan",
        {
            "entries": [
                {"content": content, "status": plan_status(status), "priority": priority or "medium"}
                for content, status, priority in entries
            ]
        },
    )


def usage_update(usage: Mapping, cost: float | None = None, **extra) -> AgentEvent:
    body: dict = {"usage": dict(usage)}
    if cost is not None:
        body["cost"] = {"amount": cost, "currency": "USD"}
    body.update({key: value for key, value in extra.items() if value is not None})
    return AgentEvent("usage_update", body)


def json_value(value):
    """``value`` as plain JSON: what json cannot encode becomes its str."""
    return json.loads(json.dumps(value, default=str))


def raw(event) -> AgentEvent:
    """A runtime event the adapter does not map, kept raw."""
    return AgentEvent("output", {"raw": json_value(event)})


def add_numbers(total: dict, values: Mapping | None) -> None:
    """Add the numbers of ``values`` to ``total``, key by key (nested objects and lists are left out)."""
    for key, value in (values or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


def cut(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# The adapter


class QueueAdapter(Adapter):
    """An agent driven by a task of its own (``_drive``), which puts the agent's events on a queue; ``events`` reads
    them until the task has ended and closed what ``_open`` started.

    A subclass implements ``_open`` (start the runtime and hand it the prompt; ``start`` returns once it did),
    ``_drive`` (follow the agent until it took its last input and ended, then set ``self.outcome``), ``_close``
    (release what ``_open`` took; called once, also after a failed ``_open``), ``_interrupt``, ``_stop_at_boundary``
    and ``send``. ``pid_file`` names the file the launcher wrote the runtime's pid to: the process group it leads is
    killed after ``_close``, and when an interrupted agent has not ended within KILL_AFTER seconds."""

    def __init__(self, context: RunContext):
        super().__init__(context)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._time_cap: asyncio.Task | None = None
        self._lock = asyncio.Lock()  # between send and the decision that the agent takes no more input
        self.pid_file: Path | None = None
        self.interrupted = False
        self.stopping = False
        self.finished = False
        self.capped: str | None = None  # the cap of the run's budget that stopped the agent
        self.budget = curator.budget_of(context.run)
        self.outcome = Outcome(False, f"{self.runtime} did not start")

    # For subclasses

    @abc.abstractmethod
    async def _open(self) -> None: ...

    @abc.abstractmethod
    async def _drive(self) -> None: ...

    @abc.abstractmethod
    async def _close(self) -> None: ...

    @abc.abstractmethod
    async def _interrupt(self) -> None: ...

    async def _stop_at_boundary(self) -> None:
        """Let the turn finish and take no more input; ``stopping`` is set already."""

    def _cleanup(self) -> None:
        """Remove what is left on disk, once the process group is gone."""

    def emit(self, events: Iterable[AgentEvent]) -> None:
        for event in events:
            self._queue.put_nowait(event)

    def group_pid(self) -> int | None:
        return read_pid(self.pid_file)

    # The interface

    async def start(self) -> None:
        try:
            await self._open()
        except BaseException:
            pid = self.group_pid()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._close(), CLOSE_TIMEOUT)
            kill_group(pid)
            self._cleanup()
            raise
        self._task = asyncio.create_task(self._main())
        left = None if self.budget is None else curator.seconds_left(self.budget, self.context.spent_seconds)
        if left is not None:
            self._time_cap = asyncio.create_task(self._stop_at_time_cap(left))

    async def _stop_at_time_cap(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self.finished:
            return
        log.info("the agent used the run's agent time; interrupting it", extra={"runtime": self.runtime})
        self.capped = "time"
        await self.interrupt()

    def time_cap_error(self) -> str:
        minutes = (self.budget or {}).get("max_seconds") or 0
        return f"{self.runtime} stopped at the run's time cap of {minutes / 60:g} minutes of agent time"

    async def _main(self) -> None:
        try:
            await self._drive()
            if self.capped == "time":
                usage, summary = self.outcome.usage, self.outcome.summary
                self.outcome = Outcome(False, self.time_cap_error(), usage, summary, cap="time")
        except asyncio.CancelledError:
            self.outcome = Outcome(False, f"{self.runtime} was stopped", self.outcome.usage, self.outcome.summary)
            raise
        except Exception as exc:
            log.warning("the agent's driver failed", extra={"runtime": self.runtime}, exc_info=True)
            error = cut(f"{self.runtime} failed: {type(exc).__name__}: {exc}")
            self.outcome = Outcome(False, error, self.outcome.usage, self.outcome.summary)
        finally:
            self.finished = True
            pid = self.group_pid()
            tools = await asyncio.to_thread(descendants, pid)  # while the runtime still holds them
            try:
                await asyncio.wait_for(self._close(), CLOSE_TIMEOUT)
            except Exception:
                log.warning("closing the agent failed", extra={"runtime": self.runtime}, exc_info=True)
            finally:
                kill_group(pid)
                kill_tree(tools)
                self._cleanup()
                if self._watchdog is not None:
                    self._watchdog.cancel()
                if self._time_cap is not None:
                    self._time_cap.cancel()
                self._queue.put_nowait(None)

    async def events(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def wait(self) -> Outcome:
        if self._task is not None:
            await self._task  # cancelled with this wait, which then closes the agent and kills its process group
        return self.outcome

    async def interrupt(self) -> None:
        if self.finished or self._task is None:
            return
        self.interrupted = True
        if self._watchdog is None:
            self._watchdog = asyncio.create_task(self._kill_later())
        await self._interrupt()

    async def stop_at_turn_boundary(self) -> None:
        if self.finished or self.stopping:
            return
        self.stopping = True
        await self._stop_at_boundary()

    async def _kill_later(self) -> None:
        await asyncio.sleep(KILL_AFTER)
        if self.finished:
            return
        log.warning("the agent did not end after an interrupt; killing its processes", extra={"runtime": self.runtime})
        pid = self.group_pid()
        kill_tree(await asyncio.to_thread(descendants, pid))
        kill_group(pid)
        await asyncio.sleep(KILL_AFTER)
        if self._task is not None and not self._task.done():
            self._task.cancel()
