"""The decision board of a project (plan loop-engine, criterion a8; the report's section Trang Quyết định): GET
/v1/projects/{p}/decision-board gathers everything of the project that waits for a person, which the web shows at
``/p/<project>/decide`` and the Loop's notices link to.

The three gates come first (``board_gates``): the goals (gate 1), the canvases (gate 2) and the policy (gate 3); then
what the Loop's chain waits on a person for (``board_chain``): the exceptions and the SRS. Each item says what waits,
since when, who may decide it and where; every button of the web calls the object's own route, so who may do what is
as it was. The board reads and never writes, for anyone with a grant on the project, and holds only what that member
may read; a project whose Loop is off still has its gates.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.autonomy_policies import effective_policy
from evo_agents.hub.server.loop import board_chain, board_gates
from evo_agents.hub.server.loop.board_items import GROUPS, DecisionBoard, Reading, ordered, people, sink_of
from evo_agents.hub.server.loop.view import loop_zone
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.runs.service.views import sees_unplanned, visible_plans


async def gather(conn: AsyncConnection, access: ProjectAccess, sink: str | None) -> DecisionBoard:
    """The decision board of project ``access`` as the member it names reads it through ``sink`` (the project's hub
    sink when None); 403 from ``visible_plans`` without a grant."""
    plans = frozenset(await visible_plans(conn, access, sink))
    reading = Reading(access, plans, sees_unplanned(access, sink), sink_of(access, sink))
    crowd = await people(conn, access.project_id)
    policy = (await effective_policy(conn, access.project_id))["loop"]
    zone = await loop_zone(conn, access.project_id, policy)
    today = (await conn.scalar(select(func.timezone(zone, func.now())))).date()
    items = [
        *await board_gates.goal_items(conn, reading, crowd, zone, today),
        *await board_gates.canvas_items(conn, reading, crowd),
        *await board_gates.policy_items(conn, reading, crowd),
        *await board_chain.chain_items(conn, reading, crowd),
    ]
    counted = Counter(found.group for found in items)
    return DecisionBoard(
        project=access.name,
        loop=bool(policy.get("enabled")),
        counts={group: counted[group] for group in GROUPS},
        items=ordered(items),
    )


__all__ = ["gather"]
