"""The base of the worker's class commands (``evo_agents.clikit``): their context, their failures and the run an
agent's command acts for.

- :class:`WorkerCommand` runs in a :class:`WorkerContext` and turns the failures every worker command shares into one
  ``error:`` line: :class:`MissingExtra` (the worker extra is not installed) into exit status 2, a ``HubError`` into
  the status ``client.exit_status`` gives it, a ``WorkerStateError`` into 1.
- :class:`AgentRunContext` is the run of EVO_RUN_ID an agent's command acts for, read once: its id, the worker's state
  (``home``, ``config``, ``token``), the run's record and its kind, checked against the kinds the command serves.
  :class:`AgentCommand` builds it; a command names its ``words`` and ``kinds`` and, when the refusal of another kind
  reads differently, writes ``refusal``.

``RUN_VARIABLE`` and :class:`MissingExtra` are defined here, the one copy of each; :func:`hubapi` imports the worker
extra's hub client.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from functools import cached_property
from typing import Any, ClassVar, TypeVar

from evo_agents.clikit import Command, Context
from evo_agents.hub import chats
from evo_agents.hub.client import HubError, exit_status
from evo_agents.worker.extra import EXTRA_HINT
from evo_agents.worker.home import WorkerConfig, WorkerHome, WorkerStateError
from evo_agents.worker.run_kinds import kind_refusal, kinds_of

EXIT_FAILED = 1
EXIT_USAGE = 2
RUN_VARIABLE = "EVO_RUN_ID"
UNSET_HINT = "The worker sets it, with EVO_RUN_KIND and EVO_WORKER_HOME, for the agent of each run."
# How long a command tries again while the hub does not answer: a deploy swaps the hub's api in less, and an agent's
# shell gives a command about two minutes
OUTAGE_SECONDS = 90.0
RETRY_FIRST_DELAY = 1.0  # seconds before the second try, doubling after each

T = TypeVar("T")


class MissingExtra(Exception):
    """A module of the worker extra (aiohttp) failed to import; the message names it."""


def hubapi():
    """The module ``evo_agents.worker.hubapi``; MissingExtra without the worker extra (aiohttp)."""
    try:
        from evo_agents.worker import hubapi
    except ImportError as exc:
        raise MissingExtra(f"missing module {exc.name}") from None
    return hubapi


def text_of(value: str, most: int = chats.MAX_MESSAGE_BYTES) -> str:
    """TEXT, or stdin for ``-``: a message of at most ``most`` bytes of UTF-8 with some text."""
    text = sys.stdin.read() if value == "-" else value
    if not text.strip():
        raise WorkerStateError("the message has no text")
    if len(text.encode()) > most:
        raise WorkerStateError(f"a message is at most {most} bytes of UTF-8")
    return text


class WorkerContext(Context):
    """What a worker command works with: its arguments, and the worker's state on this machine, read on first use."""

    @cached_property
    def home(self) -> WorkerHome:
        return WorkerHome()


def failure(
    exc: Exception, word: str | None, problems: tuple[type[Exception], ...] = (), hub_failed: int | None = None
):
    """The exit status of a failure every worker command knows, its ``error:`` line written to stderr; None for any
    other. ``hub_failed`` is the status of every refusal of the hub, when a command does not tell them apart."""
    if isinstance(exc, MissingExtra):
        print(f"error: evo-agents worker {word} needs the worker extra ({exc}): {EXTRA_HINT}", file=sys.stderr)
        return EXIT_USAGE
    if isinstance(exc, HubError):
        print(f"error: {exc}", file=sys.stderr)
        return exit_status(exc) if hub_failed is None else hub_failed
    if isinstance(exc, (WorkerStateError, *problems)):
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return None


class WorkerCommand(Command[WorkerContext]):
    """A command of ``evo-agents worker`` on this machine. ``problems`` are more failures shown as ``error:
    <message>`` and exit status 1; ``hub_failed`` is the status of every refusal of the hub, when not None."""

    problems: ClassVar[tuple[type[Exception], ...]] = ()
    hub_failed: ClassVar[int | None] = None

    def context(self, args: argparse.Namespace) -> WorkerContext:
        return WorkerContext(args)

    def failed(self, exc: Exception) -> int | None:
        return failure(exc, self.name, self.problems, self.hub_failed)


class AgentRunContext(WorkerContext):
    """The run of EVO_RUN_ID on this worker that an agent's command acts for, while it runs; WorkerStateError on first
    use outside one, or in a run of a kind the command does not serve (``kinds``; None serves any kind)."""

    def __init__(
        self,
        args: argparse.Namespace,
        words: str,
        kinds: tuple[str, ...] | None = None,
        refusal: Callable[[int, str], str] | None = None,
    ):
        super().__init__(args)
        self.words = words
        self.kinds = kinds
        self._refusal = refusal

    @cached_property
    def run_id(self) -> int:
        raw = (os.environ.get(RUN_VARIABLE) or "").strip()
        if not raw.isdigit() or int(raw) < 1:
            where = kinds_of(self.kinds) if self.kinds else "a run"
            raise WorkerStateError(
                f"`evo-agents worker {self.words}` works only inside {where} of this worker: {RUN_VARIABLE} is not "
                f"set. {UNSET_HINT}"
            )
        return int(raw)

    @cached_property
    def record(self) -> dict:
        run_id = self.run_id
        record = self.home.load_run(run_id)
        if record is None or record.get("finished_at"):
            raise WorkerStateError(
                f"run {run_id} is not running on this worker (its state is under {self.home.root}): "
                f"`evo-agents worker {self.words}` works only inside it"
            )
        kind = record.get("kind") or "step"
        if self.kinds is not None and kind not in self.kinds:
            refusal = self._refusal or (lambda run, found: kind_refusal(run, found, self.kinds or (), self.words))
            raise WorkerStateError(refusal(run_id, kind))
        return record

    @property
    def kind(self) -> str:
        return self.record.get("kind") or "step"

    @cached_property
    def config(self) -> WorkerConfig:
        return self.home.load_config()

    @cached_property
    def token(self) -> str:
        return self.home.load_token()

    def check(self) -> None:
        """Read the run now, so a command refuses outside it before it does anything else."""
        self.record  # noqa: B018

    def with_hub(self, work: Callable[[Any], Awaitable[T]]) -> T:
        """``await work(hub)`` with the worker's client of the hub; the hub's refusal, or no answer, is a HubError."""
        api = hubapi()
        from evo_agents.worker import gitops

        async def go() -> T:
            async with api.new_session() as session:
                return await work(api.WorkerHub(self.config.url, self.token, session))

        try:
            return asyncio.run(go())
        except api.HubProblem as exc:
            raise exc.hub_error() from None
        except gitops.GitError as exc:
            raise WorkerStateError(str(exc)) from None

    def in_chats(self, route: str, body: dict) -> dict:
        """POST ``body`` to the hub's route of the run's agent in chats, with the worker's token; HubError when the
        hub refuses, or does not answer for OUTAGE_SECONDS. A call the hub does not answer, or answers with a 5xx or
        429 (a deploy swapping its api: run #8743's forward got a 500 of the proxy then), is sent again after a
        backoff, as the daemon does, each try said on stderr."""
        api = hubapi()
        run_id = self.run_id

        async def go() -> dict:
            backoff = api.Backoff(first=RETRY_FIRST_DELAY)
            deadline = time.monotonic() + OUTAGE_SECONDS
            async with api.new_session() as session:
                hub = api.WorkerHub(self.config.url, self.token, session)
                while True:
                    try:
                        return await hub.in_chats(run_id, route, body)
                    except api.Unreachable as exc:
                        delay = max(backoff.next(), min(exc.retry_after or 0.0, api.RETRY_AFTER_MOST))
                        if time.monotonic() + delay > deadline:
                            raise
                        print(f"the hub did not take it ({exc}): trying again in {delay:.0f}s", file=sys.stderr)
                        await asyncio.sleep(delay)

        try:
            return asyncio.run(go())
        except api.HubProblem as exc:
            raise HubError(str(exc), exc.status, exc.code) from None

    def workspaces(self) -> dict:
        """The run's worktrees by repo, from its record."""
        from evo_agents.worker import gitops

        found = {}
        for item in self.record.get("repos") or []:
            try:
                workspace = gitops.Workspace.from_record(item)
            except ValueError:
                continue
            found[workspace.repo] = workspace
        return found


class AgentCommand(Command[AgentRunContext]):
    """A command of a run's agent: it runs in an :class:`AgentRunContext` of the run of EVO_RUN_ID. ``words`` are the
    command's words after ``worker`` (``name`` when empty), ``kinds`` the kinds of run it serves (None: any);
    ``problems`` and ``hub_failed`` as :class:`WorkerCommand` has them."""

    words: ClassVar[str] = ""
    kinds: ClassVar[tuple[str, ...] | None] = ("plan",)
    problems: ClassVar[tuple[type[Exception], ...]] = ()
    hub_failed: ClassVar[int | None] = None

    def context(self, args: argparse.Namespace) -> AgentRunContext:
        return AgentRunContext(args, self.words or self.name, self.kinds, self.refuse)

    def refuse(self, run_id: int, kind: str) -> str:
        """Why the command refuses run ``run_id`` of ``kind``, which it does not serve."""
        return kind_refusal(run_id, kind, self.kinds or (), self.words or self.name)

    def failed(self, exc: Exception) -> int | None:
        return failure(exc, (self.words or self.name).split()[0], self.problems, self.hub_failed)
