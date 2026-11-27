"""The rows of the profiles of browser identities: an identity locked after the blob lock, the columns of a version
written or cleared, and the blobs recorded for deletion (``blob_gc``)."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.server.browser.models.profiles import KIND


async def locked(conn: AsyncConnection, identity_id: int):
    """The row of identity ``identity_id`` with its project's name, locked, after the blob lock taken exclusively:
    the order every writer of the identity's blobs takes them in. None when there is no such identity."""
    await blob_gc.lock_exclusive(conn)
    b, p = tables.browser_identities, tables.projects
    found = (
        select(b, p.c.name.label("project"))
        .join(p, p.c.id == b.c.project_id)
        .where(b.c.id == identity_id)
        .with_for_update(of=b)
    )
    return (await conn.execute(found)).one_or_none()


async def forget(conn: AsyncConnection, *versions: tuple[str | None, int | None]) -> None:
    """Record for deletion the blobs of ``versions`` (sha256, size), those with none left out."""
    blobs = {sha256: (size, KIND) for sha256, size in versions if sha256 is not None}
    if blobs:
        await blob_gc.forget(conn, blobs)  # pyright: ignore[reportArgumentType]


async def write(conn: AsyncConnection, identity_id: int, values: dict) -> None:
    """Write ``values`` on identity ``identity_id``'s row; a value may name the row's columns (``shifted``)."""
    b = tables.browser_identities
    await conn.execute(update(b).values(**values).where(b.c.id == identity_id))


def shifted(names: tuple[str, ...]) -> dict:
    """The values that copy each of the columns ``names`` to its ``previous_`` column."""
    b = tables.browser_identities
    return {f"previous_{name}": b.c[name] for name in names}


async def current(conn: AsyncConnection, identity_id: int):
    """The row of identity ``identity_id`` as a mapping; None for none."""
    b = tables.browser_identities
    return (await conn.execute(select(b).where(b.c.id == identity_id))).mappings().one_or_none()
