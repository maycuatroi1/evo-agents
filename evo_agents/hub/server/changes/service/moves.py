"""A write of a Curator change (``evo_agents.hub.server.changes.service.job`` describes the design): its values set,
and a move to another state written to its proposal's ledger."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server.changes.repository.views import update_change


async def set_change(conn: AsyncConnection, change_id: int, *, ledger_extra: dict | None = None, **values) -> None:
    """Set ``values`` on change ``change_id``; a move to another state adds its lines to the proposal's ledger
    (``ledger.change_moved``, with ``ledger_extra``)."""
    old = await update_change(conn, change_id, values)
    if old is not None and old != values["state"]:
        await ledger_lines.change_moved(conn, change_id, old, values["state"], ledger_extra)
