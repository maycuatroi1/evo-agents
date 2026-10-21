"""The notice ``canvas_baseline``: a baseline of a canvas waits for the review of the project's writers (criterion a6 of
plan living-lean-canvas).

The hub sends it into the Inbox of each writer and admin of the project who may read the canvas's label through the
project's hub sink and may decide at least one waiting item of the baseline (one who changed the status of every item
may not). Telegram, a members' channel like the web, forwards it as it forwards every notice; the link opens the
baseline's page on the hub's web (``web/src/app/(app)/p/[project]/x/canvas``), where each item is decided. A decision
needs no run, so the baseline is no decision of the hub (``decisions.run_id`` is NOT NULL); it is a notice, as
@builder's proposals are.
"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.canvas import store
from evo_agents.core_plugins.canvas.baselines import BaselineRow, ItemRow
from evo_agents.core_plugins.canvas.store import CanvasRow
from evo_agents.hub import signals, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.projects import ProjectAccess

NOTICE = "canvas_baseline"  # evo_agents.hub.runs.NOTICE_KINDS


def link(project: str, slug: str, number: int) -> str:
    """The page of baseline ``number`` of canvas ``slug`` on the hub's web."""
    return f"/p/{quote(project, safe='')}/x/canvas/canvases/{quote(slug, safe='')}/baselines/{number}"


async def tell_writers(
    conn: AsyncConnection, project_id: int, canvas: CanvasRow, row: BaselineRow, found: list[ItemRow]
) -> list[int]:
    """Send the notice of baseline ``row`` to the writers who may read it and decide one of its waiting items
    ``found``; the ids of the members told."""
    waiting = [item for item in found if item.waiting]
    if not waiting:
        return []
    rules = await signals.project_rules(conn, project_id)
    g = tables.grants
    grants = await conn.execute(
        select(g.c.user_id, g.c.role, g.c.max_level).where(g.c.project_id == project_id).order_by(g.c.user_id)
    )
    told = []
    for user_id, role, max_level in grants:
        access = ProjectAccess(project_id, rules, role, max_level)
        if not has_role(role, "writer") or not store.visible(access, canvas.label, None):
            continue
        if all(user_id in item.proposers for item in waiting):
            continue
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=NOTICE,
            title=f"Baseline {row.number} of canvas {canvas.slug} in {rules.name} waits for your review",
            project_id=project_id,
            details={"canvas": canvas.slug, "baseline": row.number, "waiting": len(waiting)},
            link=link(rules.name, canvas.slug, row.number),
        )
        told.append(user_id)
    return told


__all__ = ["NOTICE", "link", "tell_writers"]
