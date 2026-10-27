"""The ledger of what is delivered of an SRS document (plan srs-backlog-loop, criterion a2): the rows of
``srs_delivery`` (``evo_agents.hub.tables_delivery``) as functions. The states, sources and the use of the ledger in a
backlog are in ``evo_agents.hub.srs_backlog``.

``record`` puts a use case of a document in a state, from a source, with its evidence, and says whether anything
changed; a second call with the same state, source and evidence writes nothing, so a tick or a retry never moves
``updated_at``. ``read`` gives a document's rows by ``ref_key``, ``states`` only their states, as ``backlog`` takes them.
Nothing here decides who may write: the caller (the Loop, a route of the admin) did.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.srs_backlog import SOURCES, STATES, ref_key


@dataclass(frozen=True)
class Delivery:
    """A use case of a document in the ledger."""

    code: str
    state: str
    source: str
    evidence: dict
    goal_id: int | None
    item_id: int | None
    updated_by: int | None
    updated_at: datetime


async def record(
    conn: AsyncConnection,
    project_id: int,
    document_id: int,
    code: str,
    state: str,
    source: str,
    evidence: dict | None = None,
    *,
    goal_id: int | None = None,
    item_id: int | None = None,
    user_id: int | None = None,
) -> bool:
    """Put use case ``code`` (its ref: the code, else the key) of document ``document_id`` in ``state`` from ``source``
    with ``evidence``; whether the row was made or changed."""
    if state not in STATES or source not in SOURCES:
        raise ValueError(f"a ledger row has a state of {STATES} and a source of {SOURCES}, not {state} from {source}")
    s = tables.srs_delivery
    values = {
        "state": state,
        "source": source,
        "evidence": evidence or {},
        "goal_id": goal_id,
        "item_id": item_id,
        "updated_by": user_id,
    }
    query = insert(s).values(
        {"project_id": project_id, "document_id": document_id, "code_key": ref_key(code), "code": code, **values}
    )
    changed = (
        s.c.state.is_distinct_from(query.excluded.state)
        | s.c.source.is_distinct_from(query.excluded.source)
        | s.c.evidence.is_distinct_from(query.excluded.evidence)
        | s.c.goal_id.is_distinct_from(query.excluded.goal_id)
        | s.c.item_id.is_distinct_from(query.excluded.item_id)
    )
    query = query.on_conflict_do_update(
        index_elements=[s.c.document_id, s.c.code_key], set_={**values, "updated_at": func.now()}, where=changed
    ).returning(s.c.code_key)
    return (await conn.execute(query)).first() is not None


async def read(conn: AsyncConnection, document_id: int) -> dict[str, Delivery]:
    """The ledger of document ``document_id`` by ``ref_key`` of the use case's code."""
    s = tables.srs_delivery
    query = select(s).where(s.c.document_id == document_id).order_by(s.c.code_key)
    return {
        row.code_key: Delivery(
            row.code,
            row.state,
            row.source,
            dict(row.evidence),
            row.goal_id,
            row.item_id,
            row.updated_by,
            row.updated_at,
        )
        for row in await conn.execute(query)
    }


async def states(conn: AsyncConnection, document_id: int) -> dict[str, str]:
    """The state of each use case of document ``document_id`` the ledger knows, by ``ref_key``."""
    return {key: row.state for key, row in (await read(conn, document_id)).items()}


__all__ = ["Delivery", "read", "record", "states"]
