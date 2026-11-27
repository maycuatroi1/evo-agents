"""What the threads of a failed or stale run pass and say (``triage.service.threads`` is the design): their limits,
the thread, what a thread is about when it is no run, and the lines of its first message that need no query."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from evo_agents.hub import chats, runs, triage_rules

KINDS = triage_rules.KINDS
ERROR_CHARS = 1200
SHOWN = 3  # candidates of each sort the message names
STALE_MINUTES = 45  # a run running or verifying with no event this long, while its worker heartbeats, opens a thread
STALE_STATES = ("running", "verifying")  # the held states a stale run is in: a waiting or interactive run is not stuck
STALE_CAUSE = "stale"  # the cause run_triage holds for the thread of a stale run
STALE_BATCH = 50  # stale runs one pass of the reaper opens a thread for at most; the next pass takes the rest
EVENT_CHARS = 300  # how much of the last event the diagnosis quotes


@dataclass(frozen=True)
class Thread:
    """The thread of a failed run: its chat, its first message and where the web shows it."""

    chat_id: int
    root_id: int
    link: str


@dataclass(frozen=True)
class About:
    """What the chat helpers read of what a thread is about, when it is no run: its project."""

    project_id: int
    project: str
    plan_id: str | None = None
    run_id: int | None = None


def thread_at(chat_id: int, root_id: int) -> Thread:
    """The thread of message ``root_id`` of chat ``chat_id``."""
    return Thread(chat_id, root_id, chats.chat_link(chat_id, root_id))


def line(text: str) -> str:
    """``text`` on one line."""
    return " ".join(str(text).split())


def when(at: datetime | None, now: datetime) -> str:
    """``at`` in UTC and how many minutes before ``now``; ``never`` for None."""
    if at is None:
        return "never"
    minutes = max(0, int((now - at).total_seconds() // 60))
    return f"{at.astimezone(UTC):%Y-%m-%d %H:%M} UTC, {minutes} min ago"


def event_text(kind: str, body) -> str:
    """What a run event of ``kind`` says, on one line, clipped to EVENT_CHARS."""
    body = body if isinstance(body, dict) else {}
    if kind == "state":
        text = f"{body.get('from')} -> {body.get('to')}" + (f": {body['reason']}" if body.get("reason") else "")
    else:
        text = next((str(body[key]) for key in ("text", "message", "summary", "tool") if body.get(key)), "")
    return runs.clip(line(text), EVENT_CHARS)


def install_lines(worker: str, tried: list[str]) -> list[str]:
    """The lines that put the agent on ``worker`` to install the program a run lacks, the run tried on ``tried``."""
    return [
        f"No worker with the program is left: the run was tried on {', '.join(tried)}.",
        f"The agent of this thread runs on {worker}: install what the error names there, on the PATH its shell has, "
        "which is the one the worker gives its runs; check each with `command -v`; then run it again with "
        "`evo-agents worker triage rerun`. A word of the error that is no program is a verify to fix, not to install.",
    ]
