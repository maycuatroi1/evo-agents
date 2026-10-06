"""The interface between the daemon and an agent runtime (``claude-code``, ``opencode``, ``codex``).

The daemon knows runs, the hub and git; an adapter knows one runtime: how to find it, start its agent on a prompt in
a worktree, hand it a message, stop it, and turn what it prints into the hub's event kinds. The daemon drives an
adapter in this order:

1. ``Adapter.detect()`` (a class method) before any run, for the heartbeat: whether the runtime is there, which
   version, and why it cannot take runs when it cannot.
2. ``adapter = Cls(context)`` for one run, then ``await adapter.start()``, which starts the agent on
   ``context.prompt`` in ``context.worktree`` and returns once it runs.
3. ``async for event in adapter.events()``: the agent's events as ``AgentEvent``, in order, until the agent process
   has ended. The kinds are those of ``evo_agents.hub.runs.WORKER_EVENT_KINDS``: ``agent_message_chunk``,
   ``agent_thought_chunk``, ``tool_call``, ``tool_call_update``, ``plan``, ``usage_update``, and ``output`` for a
   runtime event the adapter does not map, kept raw. ``system`` is the daemon's own; an adapter does not emit it.
4. Meanwhile, any number of times: ``await adapter.send(text)`` with a message of the owner, which the agent gets in
   this turn or the next (an adapter of a runtime that cannot take a message mid-turn queues it and delivers it
   when the turn ends); ``await adapter.stop_at_turn_boundary()``, which lets the turn finish and then ends the
   agent; ``await adapter.interrupt()``, which stops the agent now, its tools included.
5. ``outcome = await adapter.wait()`` once the events have ended: whether the last turn completed, the error when it
   did not, usage, and the agent's last message.

``session_id`` is the runtime's id of the agent's session once it is known (from the start for a runtime that takes
an id, from its first event otherwise); the daemon reports it, and a later takeover resumes that session.

A plan run (``context.run["kind"] == "plan"``, ``evo_agents.worker.run.PlanRun``) drives every adapter the same way,
one adapter per turn of the agent, so opencode, Codex and Claude Code need nothing of their own for it:
``context.worktree`` is the run's directory, which holds a worktree of each repo and ``.evo-run/``. A turn that ends
with a decision of the run open is followed by no new adapter until the owner answers: the daemon reports the run
``waiting``, and the answer, from the inbox, is the prompt of a new adapter with ``context.resume_session`` set to the
same session, which reports the run ``running`` again. When the hub parks the run, ``stop_at_turn_boundary`` ends the
agent at the end of its turn and the session and the worktrees stay; the run that resumes it, claimed with
``resume_of_run_id``, starts its adapter in the same directory and session.

An adapter whose ``interactive`` is true can also hand the session to a person: ``tui(context, session_id)`` gives the
runtime's own terminal UI on that session (a new one on ``context.prompt`` when ``session_id`` is None), which the
daemon runs in tmux (``evo_agents.worker.interactive``). Once the person hands the run back, a new adapter goes on
headless with ``context.resume_session`` set to the same session.

Rules every adapter keeps, from what the runtimes do (research_notes/worker-runtimes.md of the harness):

- The state of a turn comes from the events, never from the exit code: a runtime may exit 0 when interrupted.
- The agent runs with the full permissions of the machine's owner, in its own process group
  (``start_new_session=True``), so a Ctrl-C at the daemon's terminal does not reach it and ``interrupt`` can stop
  its tools too.
- stdin is the message channel or ``/dev/null``, never a pipe left open by accident: some runtimes wait for its EOF.
- ``context.env`` is the environment of the agent; it has no hub token.

Adapters are found, by runtime name, in ``BUILTIN`` (the runtimes this package supports), in the entry points of
the group ``evo_agents.worker.adapters``, and in ``EVO_WORKER_ADAPTERS`` (``runtime=module:Class`` separated by
commas), each later source winning: tests and development point a runtime at an adapter of their own that way.
A runtime with no adapter is reported as unavailable, with the reason. Standard library only.
"""

from __future__ import annotations

import abc
import importlib
import logging
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from evo_agents import __version__
from evo_agents.hub import runs

if TYPE_CHECKING:
    from evo_agents.worker.interactive import Tui

log = logging.getLogger(__name__)

ADAPTERS_VARIABLE = "EVO_WORKER_ADAPTERS"
ENTRY_POINT_GROUP = "evo_agents.worker.adapters"
# runtime -> "module:Class": the adapters of evo_agents.worker.runtimes, which import their SDKs only when they run
BUILTIN: dict[str, str] = {
    "claude-code": "evo_agents.worker.runtimes.claude_code:ClaudeCodeAdapter",
    "opencode": "evo_agents.worker.runtimes.opencode:OpencodeAdapter",
    "codex": "evo_agents.worker.runtimes.codex:CodexAdapter",
}
BINARIES = {"claude-code": "claude", "opencode": "opencode", "codex": "codex"}
VERSION_TIMEOUT = 15.0  # seconds for `<binary> --version`
_VERSION = re.compile(r"\d+(?:\.\d+)+(?:-[0-9A-Za-z.]+)?")
MAX_VERSION_CHARS = 100
MAX_REASON_CHARS = 500


def _line(text: str, limit: int) -> str:
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:limit]


@dataclass(frozen=True)
class Detection:
    """A runtime as the heartbeat reports it: ``{available, version, reason}``."""

    available: bool
    version: str | None = None
    reason: str | None = None

    def report(self) -> dict:
        version = _line(self.version, MAX_VERSION_CHARS) if self.version else None
        reason = _line(self.reason, MAX_REASON_CHARS) if self.reason else None
        return {"available": self.available, "version": version or None, "reason": reason or None}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AgentEvent:
    """One event of the agent, in one of the worker's event kinds, with a JSON object as its body."""

    kind: str
    body: dict
    at: datetime = field(default_factory=_now)

    def __post_init__(self):
        if self.kind not in runs.WORKER_EVENT_KINDS or self.kind == "system":
            raise ValueError(f"an adapter emits one of {', '.join(runs.ACP_EVENT_KINDS)} or output, not {self.kind!r}")
        if not isinstance(self.body, dict):
            raise ValueError("an event's body is a JSON object")
        if self.at.tzinfo is None:
            raise ValueError("an event's time needs its time zone")


@dataclass(frozen=True)
class RunContext:
    """What an adapter gets for one run."""

    run: Mapping  # the run as the claim answered it: id, project, plan_id, step_key, title, runtime, mode, ...
    worktree: Path  # where the agent works; its cwd
    prompt: str
    env: Mapping[str, str]  # the agent's environment
    resume_session: str | None = None  # a session of this runtime to go on with, instead of a new one

    @property
    def run_id(self) -> int:
        return int(self.run["id"])


class AgentFinished(RuntimeError):
    """``send`` after the agent took its last input: nothing would read the message, so it stays in the inbox."""


@dataclass
class Outcome:
    """How the agent ended."""

    completed: bool  # its last turn ended normally (the runtime's end-of-turn event came), not interrupted or failed
    error: str | None = None  # why not, in a sentence
    usage: dict | None = None  # tokens and cost as the runtime counts them, for the run's record
    summary: str | None = None  # the agent's last message


class Adapter(abc.ABC):
    """One runtime. A subclass names its ``runtime`` and ``binary``, and implements the run methods."""

    runtime: ClassVar[str]
    binary: ClassVar[str]
    interactive: ClassVar[bool] = False  # whether it can hand its session to a person in a terminal (``tui``)

    @classmethod
    def detect(cls) -> Detection:
        """The runtime on this machine: ``binary`` on PATH and its ``--version``."""
        return probe_binary(cls.binary)

    @classmethod
    def tui(cls, context: RunContext, session_id: str | None) -> Tui:
        """The runtime's terminal UI on the session ``session_id``, or on a new session that starts on
        ``context.prompt`` when it is None; only for an adapter whose ``interactive`` is true."""
        raise NotImplementedError(f"the {cls.runtime} adapter cannot hand its session to a terminal")

    def __init__(self, context: RunContext):
        self.context = context

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the agent on the prompt in the worktree; return once it runs."""

    @abc.abstractmethod
    async def send(self, text: str) -> None:
        """Hand the agent a message of the owner, now or at the end of its turn; AgentFinished once the agent takes
        no more input."""

    @abc.abstractmethod
    async def stop_at_turn_boundary(self) -> None:
        """Let the current turn finish, then end the agent."""

    @abc.abstractmethod
    async def interrupt(self) -> None:
        """Stop the agent and its tools now."""

    @property
    @abc.abstractmethod
    def session_id(self) -> str | None:
        """The runtime's id of the session, once known."""

    @abc.abstractmethod
    def events(self) -> AsyncIterator[AgentEvent]:
        """The agent's events in order, until its process has ended."""

    @abc.abstractmethod
    async def wait(self) -> Outcome:
        """How the agent ended; called once the events have ended."""


def probe_binary(binary: str, *, path: str | None = None, timeout: float = VERSION_TIMEOUT) -> Detection:
    """Whether ``binary`` is on PATH and answers ``--version``; available when it does."""
    found = shutil.which(binary, path=path)
    if found is None:
        return Detection(False, None, f"{binary} is not on PATH")
    try:
        done = subprocess.run(
            [found, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return Detection(False, None, f"{binary} --version gave no answer within {timeout:g}s")
    except OSError as exc:
        return Detection(False, None, f"{binary} --version failed: {exc}")
    match = _VERSION.search(done.stdout or "") or _VERSION.search(done.stderr or "")
    if done.returncode != 0:
        return Detection(False, match.group(0) if match else None, f"{binary} --version exited {done.returncode}")
    return Detection(True, match.group(0) if match else None, None)


def _parse_spec(spec: str) -> tuple[str, str]:
    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError(f"{spec!r} is not module:Class")
    return module, name


def adapter_specs(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """runtime -> "module:Class" from BUILTIN, the entry points and EVO_WORKER_ADAPTERS, later ones winning."""
    env = os.environ if env is None else env
    specs = dict(BUILTIN)
    try:
        for point in entry_points(group=ENTRY_POINT_GROUP):
            specs[point.name] = point.value
    except Exception:  # a broken distribution's metadata must not stop the daemon
        log.warning("cannot read the adapter entry points", exc_info=True)
    for item in (env.get(ADAPTERS_VARIABLE) or "").split(","):
        runtime, _, spec = item.strip().partition("=")
        if runtime and spec:
            specs[runtime.strip()] = spec.strip()
    return {runtime: spec for runtime, spec in specs.items() if runtime in runs.RUNTIMES}


def load_adapters(env: Mapping[str, str] | None = None) -> dict[str, type[Adapter]]:
    """The adapter class of each runtime that has one; a spec that does not load is logged and left out."""
    loaded: dict[str, type[Adapter]] = {}
    for runtime, spec in adapter_specs(env).items():
        try:
            module, name = _parse_spec(spec)
            cls = getattr(importlib.import_module(module), name)
        except (ImportError, AttributeError, ValueError) as exc:
            log.warning("adapter not loaded", extra={"runtime": runtime, "spec": spec, "error": str(exc)})
            continue
        if not (isinstance(cls, type) and issubclass(cls, Adapter)):
            log.warning("adapter is not an Adapter", extra={"runtime": runtime, "spec": spec})
            continue
        loaded[runtime] = cls
    return loaded


def detect_runtimes(adapters: Mapping[str, type[Adapter]], *, path: str | None = None) -> dict[str, dict]:
    """The heartbeat's ``runtimes``: each of the three, from its adapter's ``detect`` when it has one; a runtime
    without an adapter is unavailable even when its binary is there."""
    found: dict[str, dict] = {}
    for runtime in runs.RUNTIMES:
        cls = adapters.get(runtime)
        if cls is not None:
            try:
                detection = cls.detect()
            except Exception as exc:  # an adapter's bug must not stop the heartbeat
                log.warning("runtime detection failed", extra={"runtime": runtime}, exc_info=True)
                detection = Detection(False, None, f"detecting it failed: {type(exc).__name__}")
        else:
            binary = BINARIES[runtime]
            probe = probe_binary(binary, path=path)
            reason = f"evo-agents {__version__} has no adapter for {runtime} yet"
            detection = Detection(False, probe.version, reason if probe.available else probe.reason)
        found[runtime] = detection.report()
    return found
