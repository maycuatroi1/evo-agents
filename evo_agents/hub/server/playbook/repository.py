"""The SQL of the playbook: its entries in curator_playbook, its deltas in curator_playbook_deltas, what a review
run's prompt showed, and the charter and the changes its maintenance reads."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import playbook, tables


def entries():
    pb = tables.curator_playbook
    return select(
        pb.c.id,
        pb.c.lens,
        pb.c.text,
        pb.c.evidence,
        pb.c.helpful,
        pb.c.harmful,
        pb.c.created_run_id,
        pb.c.updated_run_id,
        pb.c.retired_at,
        pb.c.retired_note,
        pb.c.until_proposal_id,
        pb.c.prompt_exposures,
        pb.c.created_at,
        pb.c.updated_at,
        pb.c.label,
    )


async def entry(conn: AsyncConnection, entry_id: int):
    pb = tables.curator_playbook
    return (await conn.execute(entries().where(pb.c.id == entry_id))).one()


async def entries_of(conn: AsyncConnection, project_id: int):
    """Every entry of the project, in order of id."""
    pb = tables.curator_playbook
    return (await conn.execute(entries().where(pb.c.project_id == project_id).order_by(pb.c.id))).all()


async def _count(conn: AsyncConnection, *where) -> int:
    return (await conn.execute(select(func.count()).where(*where))).scalar_one()


async def is_proposal_of(conn: AsyncConnection, proposal_id: int, project_id: int) -> bool:
    p = tables.proposals
    return bool(await _count(conn, p.c.id == proposal_id, p.c.project_id == project_id))


async def live_count(conn: AsyncConnection, project_id: int) -> int:
    pb = tables.curator_playbook
    return await _count(conn, pb.c.project_id == project_id, pb.c.retired_at.is_(None))


async def run_deltas(conn: AsyncConnection, run_id: int) -> int:
    """The deltas run ``run_id`` wrote."""
    return await _count(conn, tables.curator_playbook_deltas.c.run_id == run_id)


async def counted_already(conn: AsyncConnection, entry_id: int, run_id: int, op: str) -> bool:
    """Whether run ``run_id`` wrote the delta ``op`` of the entry already."""
    d = tables.curator_playbook_deltas
    return bool(await _count(conn, d.c.entry_id == entry_id, d.c.run_id == run_id, d.c.op == op))


async def insert_entry(conn: AsyncConnection, values: dict) -> int:
    pb = tables.curator_playbook
    return (await conn.execute(insert(pb).values(**values).returning(pb.c.id))).scalar_one()


async def lock_entry(conn: AsyncConnection, entry_id: int, project_id: int):
    """When the project's entry was retired, its row locked; None when the project has no such entry."""
    pb = tables.curator_playbook
    query = select(pb.c.retired_at).where(pb.c.id == entry_id, pb.c.project_id == project_id).with_for_update()
    return (await conn.execute(query)).one_or_none()


async def change_entry(conn: AsyncConnection, entry_id: int, run_id: int, op: str, note: str | None) -> None:
    """One more on the counter ``op`` of the entry, or its retirement with ``note``, by run ``run_id``."""
    pb = tables.curator_playbook
    values: dict = {"updated_run_id": run_id, "updated_at": func.now()}
    if op in playbook.TAGS:
        values[op] = pb.c[op] + 1
    else:
        values.update(retired_at=func.now(), retired_note=note)
    await conn.execute(update(pb).values(**values).where(pb.c.id == entry_id))


async def insert_delta(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.curator_playbook_deltas).values(**values))


async def last_shown(conn: AsyncConnection, run_id: int):
    """What the last ``playbook_shown`` of run ``run_id``'s log names; None without one."""
    e = tables.run_events
    last = (
        select(e.c.body["playbook_shown"])
        .where(e.c.run_id == run_id, e.c.kind == "system", e.c.body.has_key("playbook_shown"))
        .order_by(e.c.seq.desc())
        .limit(1)
    )
    return (await conn.execute(last)).scalar_one_or_none()


async def count_exposures(conn: AsyncConnection, project_id: int, ids: list[int]) -> None:
    """One more on ``prompt_exposures`` of each entry of ``ids`` of the project."""
    pb = tables.curator_playbook
    more = update(pb).values(prompt_exposures=pb.c.prompt_exposures + 1)
    await conn.execute(more.where(pb.c.project_id == project_id, pb.c.id.in_(ids)))


async def newest_charter_body(conn: AsyncConnection, project_id: int):
    ch = tables.charters
    query = select(ch.c.body).where(ch.c.project_id == project_id).order_by(ch.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()


async def lock_live(conn: AsyncConnection, project_id: int) -> list[dict]:
    """The live entries of the project, their rows locked, oldest first."""
    pb = tables.curator_playbook
    live = select(pb.c.id, pb.c.text, pb.c.until_proposal_id, pb.c.prompt_exposures, pb.c.helpful)
    live = live.where(pb.c.project_id == project_id, pb.c.retired_at.is_(None)).order_by(pb.c.id).with_for_update()
    return [dict(row._mapping) for row in await conn.execute(live)]


async def change_states(conn: AsyncConnection, project_id: int, proposal_ids: set[int]) -> dict[int, str]:
    """The state of the Curator's change of each proposal of ``proposal_ids`` that has one."""
    c = tables.curator_changes
    states = select(c.c.proposal_id, c.c.state).where(c.c.project_id == project_id, c.c.proposal_id.in_(proposal_ids))
    return {row.proposal_id: row.state for row in await conn.execute(states)}


async def retire(conn: AsyncConnection, entry_id: int, note: str) -> None:
    pb = tables.curator_playbook
    gone = update(pb).values(retired_at=func.now(), retired_note=note, updated_at=func.now())
    await conn.execute(gone.where(pb.c.id == entry_id))
