"""Reading the outputs of runs: of a run, of the questions and chat messages that carry them, one by its id, and the
messages that carry one."""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.db import one_of


def columns():
    """The columns of an output, named as ``RunOutput`` names them (``url`` and ``viewer`` aside)."""
    o = tables.run_outputs
    return (
        o.c.id,
        o.c.run_id,
        o.c.source,
        o.c.name,
        o.c.media_type,
        o.c.size,
        o.c.sha256,
        o.c.caption,
        o.c.tool_call_id,
        o.c.created_at,
        o.c.interactive,
    )


async def of_run(conn: AsyncConnection, run_id: int, sources: list[str] | None, after: int) -> list:
    """The rows of the outputs of run ``run_id`` after id ``after``, of ``sources`` (every source when empty or None),
    oldest first."""
    o = tables.run_outputs
    query = select(*columns()).where(o.c.run_id == run_id, o.c.id > after)
    if sources:
        query = query.where(o.c.source.in_(sources))
    return list((await conn.execute(query.order_by(o.c.id))).all())


async def by_decision(conn: AsyncConnection, decision_ids: list[int]) -> list:
    """The rows of the outputs each of ``decision_ids`` carries, with ``decision_id``, oldest first."""
    o, k = tables.run_outputs, tables.run_output_links
    query = (
        select(k.c.decision_id, *columns())
        .join_from(k, o, o.c.id == k.c.output_id)
        .where(one_of(k.c.decision_id, decision_ids))
        .order_by(o.c.id)
    )
    return list((await conn.execute(query)).all())


async def by_message(conn: AsyncConnection, message_ids: list[int]) -> list:
    """The rows of the outputs each of ``message_ids`` carries, with ``chat_message_id``, oldest first."""
    o, k = tables.run_outputs, tables.run_output_links
    query = (
        select(k.c.chat_message_id, *columns())
        .join_from(k, o, o.c.id == k.c.output_id)
        .where(one_of(k.c.chat_message_id, message_ids))
        .order_by(o.c.id)
    )
    return list((await conn.execute(query)).all())


async def by_ids(conn: AsyncConnection, output_ids: list[int]) -> dict:
    """The rows of outputs ``output_ids``, by id."""
    o = tables.run_outputs
    return {row.id: row for row in (await conn.execute(select(*columns()).where(one_of(o.c.id, output_ids)))).all()}


async def one(conn: AsyncConnection, output_id: int, *where):
    """The row of output ``output_id`` that ``where`` keeps; None when there is none."""
    o = tables.run_outputs
    return (await conn.execute(select(*columns()).where(o.c.id == output_id, *where))).one_or_none()


def of_the_run(run_id: int):
    """Whether the output is of run ``run_id``."""
    return tables.run_outputs.c.run_id == run_id


def carried_by_decision(output_id: int, decision_id: int):
    """Whether decision ``decision_id`` carries output ``output_id``."""
    k = tables.run_output_links
    return exists().where(k.c.output_id == output_id, k.c.decision_id == decision_id)


async def message_labels(conn: AsyncConnection, chat_id: int, output_id: int) -> list:
    """The labels of the messages of chat ``chat_id`` that carry output ``output_id``."""
    k, cm = tables.run_output_links, tables.chat_messages
    labels = select(cm.c.label).join_from(k, cm, cm.c.id == k.c.chat_message_id)
    labels = labels.where(k.c.output_id == output_id, cm.c.chat_id == chat_id)
    return list((await conn.execute(labels)).scalars())


async def carried_by(conn: AsyncConnection, output_id: int) -> list:
    """(chat_message_id, chat_id) of each chat message that carries output ``output_id``."""
    k, cm = tables.run_output_links, tables.chat_messages
    query = select(k.c.chat_message_id, cm.c.chat_id).join_from(k, cm, cm.c.id == k.c.chat_message_id)
    return list((await conn.execute(query.where(k.c.output_id == output_id))).all())
