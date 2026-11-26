"""The events the plugin canvas emits (criterion a6 of plan living-lean-canvas; ``evo_agents.hub.plugin_events``):
``canvas.claim`` on every rev of a claim, whatever wrote it (an edit, an undo, an import, an accepted proposal, the
decision on an item of a baseline), and ``canvas.baseline`` when a baseline is approved, with the changes of status it
makes take effect. Plugins granted ``canvas:read`` get them, at least once; the hub keeps them in hub_events, where the
Loop reads them by cursor. Each is emitted in the transaction that made it.

A payload carries ids, codes and statuses, never the text of a claim or of evidence: a plugin that needs more reads the
canvas through the API with its grant. ``v`` is the version of its shape.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, plugin_events
from evo_agents.hub.server.canvas.models.content import code
from evo_agents.hub.server.canvas.repository import people

CLAIM = hub_events.CLAIM
BASELINE = hub_events.BASELINE


async def claim_rev(
    conn: AsyncConnection,
    project_id: int,
    author,
    *,
    canvas_id: int,
    claim_id: int,
    number: int,
    rev: int,
    cell: str,
    change: str,
    before: str | None,
    after: str,
) -> None:
    """Emit ``canvas.claim`` of rev ``rev`` of a claim, which ``author`` (``writing.Author``) wrote, moving its status
    from ``before`` (None for a new claim) to ``after``."""
    slug = await people.canvas_slug(conn, canvas_id)
    login = None if author.user_id is None else await people.login_of(conn, author.user_id)
    payload = {
        "v": hub_events.VERSION,
        "canvas": slug,
        "canvas_id": canvas_id,
        "claim_id": claim_id,
        "claim": code(number),
        "rev": rev,
        "cell": cell,
        "change": change,
        "from": before,
        "to": after,
        "actor": author.actor,
        "login": login,
        "run_id": author.run_id,
    }
    await plugin_events.emit(conn, CLAIM, project_id, payload)


async def baseline_approved(conn: AsyncConnection, project_id: int, payload: dict) -> None:
    """Emit ``canvas.baseline`` of a baseline just approved (``baselines.approve`` builds ``payload``)."""
    await plugin_events.emit(conn, BASELINE, project_id, {"v": hub_events.VERSION, **payload})


__all__ = ["BASELINE", "CLAIM", "baseline_approved", "claim_rev"]
