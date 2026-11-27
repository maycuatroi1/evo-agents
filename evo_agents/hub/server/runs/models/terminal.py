"""The limits and the refusal of the web terminal (``evo_agents.hub.server.terminal``), and what of a terminal session
the service reads and sets when it opens one."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol

SESSION_MAX_AGE = timedelta(hours=12)  # a web session older than this opens no terminal: sign in again
OPEN_STATES = ("leased", "running", "interactive")  # a browser opens the terminal of a run in one of these
WORKER_STATES = ("interactive",)  # the worker connects its end only once a person may drive the agent


class Refusal(Exception):
    """Close the socket with ``code`` and ``reason``: the request may not open the terminal."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class Reserving(Protocol):
    def reserve(self, session) -> bool: ...


class TerminalSession(Protocol):
    """A browser's session on a run's terminal, as ``runs.service.terminal.open_session`` reads and sets it."""

    @property
    def terminals(self) -> Reserving: ...

    user: Any  # the Principal of the browser's web session
    run_id: int
    worker_id: int | None
    project_id: int | None
    target: str
