"""The SQL of the branding of the hub (``evo_agents.hub.server.branding``): the blob row of an image the hub holds."""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.hub_admin.models.branding import IMAGE_KIND


async def add_image_blob(conn: AsyncConnection, user_id: int, sha256: str, size: int) -> None:
    """Record that the hub holds blob ``sha256`` of kind branding-image, unless it does already."""
    b = tables.blobs
    await conn.execute(
        pg_insert(b)
        .values(project_id=None, sha256=sha256, size=size, kind=IMAGE_KIND, created_by=user_id)
        .on_conflict_do_nothing()
    )
