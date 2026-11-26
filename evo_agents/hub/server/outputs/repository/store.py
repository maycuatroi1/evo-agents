"""Recording the outputs of runs and tying them to questions and chat messages: the run outputs go to, a project's
switch of tool images, what a run holds, the uploads and blobs an output names, the rows of outputs, their links, and
deleting one."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import exists, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, output_prune, outputs, tables
from evo_agents.hub.db import one_of

# The run and its project


async def run_of(conn: AsyncConnection, run_id: int):
    """The project_id, kind and state of run ``run_id``."""
    r = tables.runs
    return (await conn.execute(select(r.c.project_id, r.c.kind, r.c.state).where(r.c.id == run_id))).one()


async def run_kind(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return (await conn.execute(select(r.c.kind).where(r.c.id == run_id))).scalar_one_or_none()


async def locked_run(conn: AsyncConnection, run_id: int):
    """Who dispatched run ``run_id``, its plan and step, its row locked."""
    r = tables.runs
    locked = select(r.c.dispatched_by, r.c.plan_id, r.c.step_key).where(r.c.id == run_id).with_for_update()
    return (await conn.execute(locked)).one()


async def tool_images(conn: AsyncConnection, project_id: int) -> bool:
    """Whether project ``project_id`` takes the images its machines take out of tools' results."""
    p = tables.projects
    return bool((await conn.execute(select(p.c.output_tool_images).where(p.c.id == project_id))).scalar_one())


async def set_tool_images(conn: AsyncConnection, project_id: int, on: bool) -> None:
    p = tables.projects
    await conn.execute(update(p).values(output_tool_images=on).where(p.c.id == project_id))


async def usage(conn: AsyncConnection, run_id: int) -> tuple[int, int]:
    """(outputs, bytes) run ``run_id`` holds."""
    o = tables.run_outputs
    query = select(func.count(), func.coalesce(func.sum(o.c.size), 0)).where(o.c.run_id == run_id)
    count, size = (await conn.execute(query)).one()
    return int(count), int(size)


# The bytes an output names


async def held_hashes(conn: AsyncConnection, run_id: int, hashes: list[str]) -> set[str]:
    """Those of ``hashes`` run ``run_id`` holds an output of."""
    o = tables.run_outputs
    held = select(o.c.sha256).where(o.c.run_id == run_id, one_of(o.c.sha256, hashes))
    return set((await conn.execute(held)).scalars())


async def uploads(conn: AsyncConnection, upload_ids: list[str]) -> dict:
    """The rows (sha256, size, kind) of the blob uploads ``upload_ids``, by id."""
    u = tables.blob_uploads
    query = select(u.c.upload_id, u.c.sha256, u.c.size, u.c.kind).where(
        u.c.upload_id.in_([UUID(i) for i in upload_ids])
    )
    return {str(row.upload_id): row for row in (await conn.execute(query)).all()}


async def project_blobs(conn: AsyncConnection, project_id: int, hashes: list[str]) -> dict[str, int]:
    """{sha256: size} of the blobs of ``hashes`` project ``project_id`` holds."""
    b = tables.blobs
    query = select(b.c.sha256, b.c.size).where(b.c.project_id == project_id, blob_gc.hashes_in(b.c.sha256, hashes))
    return {row.sha256: row.size for row in (await conn.execute(query)).all()}


async def hold_blob(conn: AsyncConnection, project_id: int, sha256: str, size: int, user_id: int) -> None:
    """Have project ``project_id`` hold the blob of ``sha256`` as an output's, unless it does already."""
    held = {"project_id": project_id, "sha256": sha256, "size": size, "kind": outputs.KIND}
    await conn.execute(pg_insert(tables.blobs).values(**held, created_by=user_id).on_conflict_do_nothing())


# The rows of outputs


async def held_outputs(conn: AsyncConnection, run_id: int, hashes: list[str]) -> dict:
    """The outputs of run ``run_id`` of ``hashes``, by SHA-256, locked: their id, source, caption, tool call and
    mark."""
    o = tables.run_outputs
    query = select(o.c.id, o.c.sha256, o.c.source, o.c.caption, o.c.tool_call_id, o.c.interactive).where(
        o.c.run_id == run_id, one_of(o.c.sha256, hashes)
    )
    return {row.sha256: row for row in (await conn.execute(query.with_for_update())).all()}


async def change_output(conn: AsyncConnection, output_id: int, changed: dict) -> None:
    o = tables.run_outputs
    await conn.execute(update(o).values(**changed).where(o.c.id == output_id))


async def add_output(conn: AsyncConnection, run_id: int, project_id: int, fields: dict) -> int:
    o = tables.run_outputs
    added = insert(o).values(run_id=run_id, project_id=project_id, **fields).returning(o.c.id)
    return (await conn.execute(added)).scalar_one()


async def sources(conn: AsyncConnection, run_id: int, output_ids: list[int]) -> dict[int, str]:
    """{id: source} of those of ``output_ids`` that are outputs of run ``run_id``."""
    o = tables.run_outputs
    rows = (
        await conn.execute(select(o.c.id, o.c.source).where(o.c.run_id == run_id, one_of(o.c.id, output_ids)))
    ).all()
    return {row.id: row.source for row in rows}


async def drop_output(conn: AsyncConnection, output_id: int) -> None:
    """Delete output ``output_id`` and its links; its bytes go once nothing refers to them (``output_prune``)."""
    await output_prune.drop(conn, tables.run_outputs.c.id == output_id)


# Questions and chat messages


async def link(conn: AsyncConnection, column: str, target_id: int, output_ids: list[int]) -> None:
    """Have the question or chat message ``target_id`` (``column``: decision_id or chat_message_id) carry
    ``output_ids``."""
    if not output_ids:
        return
    k = tables.run_output_links
    rows = [{"output_id": output_id, column: target_id} for output_id in output_ids]
    key = [k.c.output_id, k.c[column]]
    await conn.execute(pg_insert(k).values(rows).on_conflict_do_nothing(index_elements=key))


async def pending(conn: AsyncConnection, run_id: int) -> list[int]:
    """The outputs of run ``run_id`` that go under its chat messages and none carries yet: those it sent or left in
    its outputs directory, oldest first."""
    o, k = tables.run_outputs, tables.run_output_links
    carried = exists().where(k.c.output_id == o.c.id, k.c.chat_message_id.is_not(None))
    query = select(o.c.id).where(o.c.run_id == run_id, o.c.source != outputs.TOOL, ~carried).order_by(o.c.id)
    return list((await conn.execute(query)).scalars())


async def last_message(conn: AsyncConnection, run_id: int):
    """The id and chat of the last message chat run ``run_id`` wrote in its chat, a forward left out; None when it
    wrote none."""
    r, cm = tables.runs, tables.chat_messages
    last = (
        select(cm.c.id, cm.c.chat_id)
        .join_from(cm, r, r.c.chat_id == cm.c.chat_id)
        .where(r.c.id == run_id, cm.c.run_id == run_id)
        .where(or_(cm.c.author_profile_id.is_(None), cm.c.author_profile_id == r.c.profile_id))  # not a forward
        .order_by(cm.c.seq.desc())
        .limit(1)
    )
    return (await conn.execute(last)).one_or_none()
