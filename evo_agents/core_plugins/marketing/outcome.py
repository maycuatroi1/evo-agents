"""Where a post ends: its state, its reason, the result of each layer and one audit row (``marketing.post.<state>``)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import store
from evo_agents.hub.server import core_plugin_api as hub


async def finish(
    conn: AsyncConnection, project_id: int, post, state: str, reason: str | None, layers: dict, **more
) -> str:
    await store.save_post(
        conn, post.id, state=state, reason=reason, layers=layers, decided_at=datetime.now(UTC), **more
    )
    project = await hub.read_project(conn, store.handle(), project_id)
    await hub.record_audit(
        conn, store.handle(), project.name, f"marketing.post.{state}", f"post:{post.id}", actor=post.proposed_by
    )
    return state
