"""What the Inbox shows of a batch of the ops queue that holds a notification (``ops.repository.views`` reads it): the
batch, its state and the path of its ops thread."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

HELD_STATES = ("open", "ready", "running")  # ops_queue.BATCH_STATES of a batch the Orchestrator still has to settle
HANDLERS = ("ops", "person")  # the Inbox's filter: held by a batch, or by none


class OpsHolding(BaseModel):
    batch_id: int
    state: Literal[HELD_STATES] = Field(description="open while it gathers, ready until its thread opens, running then")  # pyright: ignore[reportInvalidTypeForm]
    thread: str | None = Field(description="the hub web's path of the batch's ops thread; null before it opened")


def thread_path(chat_id: int | None, root_message_id: int | None) -> str | None:
    """The web's path of a chat thread, as ``web/src/lib/work.ts`` threadHref writes it; None without one."""
    if chat_id is None or root_message_id is None:
        return None
    return f"/chats/{chat_id}?thread={root_message_id}"
