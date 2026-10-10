"""The exceptions that end a run, or a step of one, and the helpers every part of a run uses."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Collection
from datetime import UTC, datetime

from evo_agents.worker import gitops

log = logging.getLogger("evo_agents.worker")

HARD_STOP_TRIES = 3  # tries of a report once the daemon is stopping now


class RunGone(Exception):
    """The hub no longer takes reports of this run from this worker: lost, cancelled, taken by another attempt."""


class ReportRefused(RunGone):
    """The hub refused a report with a 4xx other than 401 and 403; ``status`` says which."""

    def __init__(self, message: str, status: int | None):
        super().__init__(message)
        self.status = status


class Parked(Exception):
    """The hub parked the plan run, or a new run resumes it: the agent's turn is over, and its session and worktrees
    stay for the run that resumes it."""


class RunFailed(Exception):
    """The run fails with ``error``; ``fields`` go with the report: ``verify``, ``usage``, ``cap`` (the cap of the
    budget that stopped the agent) and ``cause`` (``runs.FAILURE_CAUSES``, the report's failure_cause)."""

    def __init__(self, error: str, **fields):
        super().__init__(error)
        self.error = error
        self.fields = fields


class Stopped(Exception):
    """The run was asked to stop: ``reason`` is cancel, timeout, gone or shutdown."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def now() -> datetime:
    return datetime.now(UTC)


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def push_cause(exc: Exception) -> str | None:
    """The failure_cause of a push git failed: credentials when the remote refused its credential, push_conflict when
    it refused a push that is no fast-forward (the branch moved on), else none."""
    if isinstance(exc, gitops.GitAuthError):
        return "credentials"
    return "push_conflict" if gitops.rejected(exc) else None


def json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


async def wait_or(event: asyncio.Event, seconds: float) -> bool:
    """Wait ``seconds``, or less when ``event`` is set; whether it is."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(event.wait(), max(0.0, seconds))
    return event.is_set()


async def first_of(events: Collection[asyncio.Event], seconds: float) -> None:
    """Wait ``seconds``, or less when one of ``events`` is set."""
    waits = {asyncio.create_task(event.wait()) for event in events}
    try:
        await asyncio.wait(waits, timeout=max(0.0, seconds), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waits:
            task.cancel()
