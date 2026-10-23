"""The stage counts the plugin pushes to the project's Signal: one number of each stage, ``crm.lead``,
``crm.invited``, ``crm.activated`` and ``crm.retained``, the contacts in that stage now (``docs/crm-plugin.md``).

The hub takes a signal of a key an admin declared (``evo-agents hub signal key add``): the plugin declares none, and a
key that is not declared is counted in ``rejected`` of the job's answer, nothing more. The numbers are written as the
ingest key ``crm`` of the project, which the plugin makes once with a token nobody sees (an admin who revokes the key
stops the pushes; one who makes a key of that name with a token of their own keeps them going). A number is sent once
a day for each value it takes (``external_id``), so a job that runs every few minutes adds a row only when a count
moves. A signal carries no login: only the counts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import store
from evo_agents.hub import signals, tables
from evo_agents.hub.server.signals import SignalIn
from evo_agents.hub.tables_crm import STAGES

log = logging.getLogger(__name__)

KEY_NAME = "crm"  # the name of the project's ingest key
SOURCE = "crm"
DEFAULT_DAYS = 90


def key_of(stage: str) -> str:
    """The Signal key of a stage: ``crm.lead``."""
    return f"crm.{stage}"


async def ingest_pusher(conn: AsyncConnection, project_id: int) -> signals.Pusher | None:
    """The pusher of the key ``crm`` of the project, made on first use; None when no admin can own the key or the
    admins revoked the one it made and made no other."""
    k, g = tables.signal_ingest_keys, tables.grants
    rules = await signals.project_rules(conn, project_id)
    found = (
        await conn.execute(
            select(k.c.id, k.c.max_level, k.c.revoked_at).where(k.c.project_id == project_id, k.c.name == KEY_NAME)
        )
    ).all()
    live = next((row for row in found if row.revoked_at is None), None)
    if live is None and found:
        return None
    if live is None:
        owner = await conn.scalar(
            select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id).limit(1)
        )
        if owner is None:
            return None
        level = rules.default_label.get("level")
        token = signals.new_ingest_token()  # nobody reads it: the plugin writes in this process
        made = await conn.execute(
            insert(k)
            .values(
                project_id=project_id,
                name=KEY_NAME,
                max_level=level,
                token_hash=signals.hash_token(token),
                created_by=owner,
            )
            .returning(k.c.id, k.c.max_level, k.c.revoked_at)
        )
        live = made.one()
    return signals.Pusher(project_id, rules, "ingest", live.id, KEY_NAME, live.max_level)


async def push_counts(conn: AsyncConnection, project_id: int, days: int = DEFAULT_DAYS) -> dict:
    """Push the stage counts of the project; what the hub stored, found twice and refused."""
    pusher = await ingest_pusher(conn, project_id)
    if pusher is None:
        return {"pushed": False, "reason": "no ingest key crm"}
    held = await store.counts(conn, project_id)
    today = datetime.now(UTC).date().isoformat()
    items = [
        SignalIn(
            key=key_of(stage),
            value=float(held[stage]),
            source=SOURCE,
            external_id=f"crm:{stage}:{today}:{held[stage]}",
        )
        for stage in STAGES
    ]
    done = await signals.push(conn, pusher, items, days)
    return {"pushed": True, "accepted": done.accepted, "duplicates": done.duplicates, "rejected": len(done.rejected)}


__all__ = ["KEY_NAME", "ingest_pusher", "key_of", "push_counts"]
