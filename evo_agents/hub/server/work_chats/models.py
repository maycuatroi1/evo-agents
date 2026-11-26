"""What a work chat works with: the trace it is of (``Trace``), what a hand-off gave (``Handed``) and the limits. Who
hands work over (``Asker``) is the service's: it builds the chat runs' ``Hand``."""

from __future__ import annotations

from dataclasses import dataclass

TITLE = "Work: {what}"
TITLE_WORDS = 60  # characters of the trace's first message a work chat's title shows
STEP_THREADS_SCAN = 500  # the latest runs of an agent in a work chat an ask of a plan step looks through for its thread


@dataclass(frozen=True)
class Trace:
    """A trace as its work chat needs it: its system and project, the chat, message and thread root of the message
    that opened it, who wrote that message, the system's coordinator (its entry agent) and the work chat."""

    id: int
    system_id: int | None
    project_id: int
    chat_id: int | None
    message_id: int | None
    root_id: int | None
    opened_by: int | None
    coordinator_id: int | None
    work_chat_id: int | None


@dataclass(frozen=True)
class Handed:
    """A hand-off: the run the work went to and the question's message (None and why none), and the thread it is in."""

    child: int | None
    message_id: int | None
    why: str | None
    place: tuple[int, int] | None = None
