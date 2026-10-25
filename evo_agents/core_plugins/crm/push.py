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

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import store
from evo_agents.core_plugins.crm.tables import STAGES
from evo_agents.hub.server import core_plugin_api as hub

log = logging.getLogger(__name__)

KEY_NAME = "crm"  # the name of the project's ingest key: the plugin's id, as push_signal names it
SOURCE = "crm"
DEFAULT_DAYS = 90
NO_KEY = 409  # what push_signal answers when no admin can own the key or the admins revoked it


def key_of(stage: str) -> str:
    """The Signal key of a stage: ``crm.lead``."""
    return f"crm.{stage}"


async def push_counts(conn: AsyncConnection, project: str, project_id: int, days: int = DEFAULT_DAYS) -> dict:
    """Push the stage counts of the project; what the hub stored, found twice and refused."""
    held = await store.counts(conn, project_id)
    today = datetime.now(UTC).date().isoformat()
    done = {"pushed": True, "accepted": 0, "duplicates": 0, "rejected": 0}
    for stage in STAGES:
        try:
            pushed = await hub.push_signal(
                conn,
                store.handle(),
                project,
                key_of(stage),
                float(held[stage]),
                external_id=f"crm:{stage}:{today}:{held[stage]}",
                days=days,
            )
        except hub.Refused as exc:
            if exc.status_code != NO_KEY:
                raise
            return {"pushed": False, "reason": f"no ingest key {KEY_NAME}"}
        done["accepted"] += pushed.stored
        done["duplicates"] += pushed.duplicate
        done["rejected"] += pushed.refused is not None
    return done


__all__ = ["KEY_NAME", "key_of", "push_counts"]
