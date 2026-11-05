"""The entity maps runs posted to the hub (``evo_agents.kg.entity_map``), kept for the builds of strategy
entity-graph-agent.

A run of the project posts a map (POST /v1/kg/P/entity-map, ``evo_agents.hub.server.kg_entity_map``), whose entries
the hub checks against its latest build as the run's sink reads it; the hub keeps what passed as one blob of kind
MAP_KIND, as it keeps the entities step's answers (``kg_entity_relay``), and queues a build. Every build reads every
map the project holds (``map_hashes``): ``write_maps`` puts them as the files of ``entity_map.MAPS_DIR`` under the
project's root in the cache, where the build's entity_map step reads them, so the cache can still be deleted.
"""

from __future__ import annotations

import io
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.blobs import BlobStore, blob_key
from evo_agents.kg import entity_map

MAP_KIND = "kg-entity-map"
MAX_MAP_BYTES = entity_map.MAX_BYTES  # one map kept


async def map_hashes(conn: AsyncConnection, project_id: int) -> list[str]:
    """The maps the project holds, by hash."""
    b = tables.blobs
    rows = await conn.execute(
        select(b.c.sha256).where(b.c.project_id == project_id, b.c.kind == MAP_KIND).order_by(b.c.sha256)
    )
    return list(rows.scalars())


def write_maps(store: BlobStore, root: Path, hashes: list[str]) -> int:
    """Put the maps ``hashes`` under ``root`` as ``<sha256>.json``, fetching those not there, and remove the files of
    a map the project no longer holds; a blob missing is left out. The count written. Blocking."""
    folder = entity_map.maps_dir(root)
    wanted = set(hashes)
    if folder.is_dir():
        for path in folder.glob("*.json"):
            if path.stem not in wanted:
                path.unlink(missing_ok=True)
    count = 0
    for sha in hashes:
        path = folder / f"{sha}.json"
        if path.exists():
            count += 1
            continue
        sink = io.BytesIO()
        got = store.fetch(blob_key(sha), sink, MAX_MAP_BYTES)
        if got is None or got[0] != sha:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(sink.getvalue())
        tmp.replace(path)
        count += 1
    return count


__all__ = ["MAP_KIND", "MAX_MAP_BYTES", "map_hashes", "write_maps"]
