"""What the Inbox shows of the ops queue (plan hub-steward, criterion a12; ``docs/hub.md``, Agent systems): which of a
member's notifications the Orchestrator of the project's default system holds, in a batch of the queue
(``evo_agents.hub.ops_queue``) that is gathering (``open``), waiting for its thread (``ready``), or in its thread
(``running``), and the path of that thread. A notification no such batch holds waits on a person.

A decision is held when an item of such a batch names it and is still pending; a notice when an item names its run.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

HELD_STATES = ("open", "ready", "running")  # ops_queue.BATCH_STATES of a batch the Orchestrator still has to settle
HANDLERS = ("ops", "person")  # the Inbox's filter: held by a batch, or by none


class OpsHolding(BaseModel):
    batch_id: int
    state: Literal[HELD_STATES] = Field(description="open while it gathers, ready until its thread opens, running then")
    thread: str | None = Field(description="the hub web's path of the batch's ops thread; null before it opened")


def thread_path(chat_id: int | None, root_message_id: int | None) -> str | None:
    """The web's path of a chat thread, as ``web/src/lib/work.ts`` threadHref writes it; None without one."""
    if chat_id is None or root_message_id is None:
        return None
    return f"/chats/{chat_id}?thread={root_message_id}"


def _matches(stored):
    """An item of a batch the Orchestrator still holds that names notification ``stored``: its decision, or for a
    notice its run."""
    i, b = tables.ops_items, tables.ops_batches
    return and_(
        i.c.state == "pending",
        b.c.state.in_(HELD_STATES),
        b.c.project_id == stored.c.project_id,
        or_(
            and_(stored.c.decision_id.is_not(None), i.c.decision_id == stored.c.decision_id),
            and_(stored.c.kind == "notice", stored.c.run_id.is_not(None), i.c.run_id == stored.c.run_id),
        ),
    )


def held(stored=None):
    """The SQL condition that a batch of the ops queue holds notification ``stored`` (tables.notifications when
    None)."""
    stored = tables.notifications if stored is None else stored
    i, b = tables.ops_items, tables.ops_batches
    return exists(select(i.c.id).select_from(i.join(b, b.c.id == i.c.batch_id)).where(_matches(stored)))


async def by_notification(conn: AsyncConnection, ids: list[int]) -> dict[int, OpsHolding]:
    """The batch that holds each of notifications ``ids``, by notification id; the newest when more than one does."""
    if not ids:
        return {}
    n, i, b, t = tables.notifications, tables.ops_items, tables.ops_batches, tables.orchestrator_threads
    query = (
        select(n.c.id, b.c.id.label("batch_id"), b.c.state, t.c.chat_id, t.c.root_message_id)
        .select_from(i.join(b, b.c.id == i.c.batch_id).join(n, _matches(n)).outerjoin(t, t.c.id == b.c.thread_id))
        .where(n.c.id.in_(ids))
        .order_by(b.c.id)
    )
    found: dict[int, OpsHolding] = {}
    for row in await conn.execute(query):
        found[row.id] = OpsHolding(
            batch_id=row.batch_id, state=row.state, thread=thread_path(row.chat_id, row.root_message_id)
        )
    return found
