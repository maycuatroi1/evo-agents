"""What the agent of a run was shown, as an audience (``evo_agents.kg.policy``), so that a memory it writes carries the
audience of the chats it read (criterion a6 of plan chat-history-in-kg).

The hub notes in ``runs.seen_audience`` the principals of everything it returns to the agent of a run that has an
audience: the label of each kg_* result (``kg.tool_result``), the messages chat_message and chat_thread read on /mcp and
`evo-agents worker chat read` reads (``chat_read``), and the memories memory_search and memory_get return
(``memories``). A note only adds principals, in one UPDATE that merges them into the row as it is then, so two calls of
the same run at once lose none; nothing removes one while the run lives.

``seen`` is what memory_write of the run's agent joins into the memory's audience (``memories.put``): the notes of the
run, and of every run whose session it goes on in (``runs.resume_of_run_id``), whose agent's context it keeps; and for a
chat run, ``chat:<id>`` of its chat, whose messages its prompt shows, without a note.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import BigInteger, Text, bindparam, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.kg.policy import audience_of
from evo_agents.kg.sessions import READ_META

CHAT_RUN = "chat"  # the kind of a chat run (``runs.kind``)


def _noted():
    """The UPDATE that merges :principals into the seen audience of run :noted_run, sorted and without repeats, when
    they are not all there; its parameters name no column of runs (docs/hub.md, Data access)."""
    r = tables.runs
    given = bindparam("principals", type_=ARRAY(Text))
    principal = func.unnest(func.array_cat(r.c.seen_audience, given)).column_valued("principal")
    merged = select(func.array_agg(aggregate_order_by(principal.distinct(), principal))).scalar_subquery()
    return (
        update(r)
        .values(seen_audience=merged)
        .where(r.c.id == bindparam("noted_run", type_=BigInteger), ~r.c.seen_audience.contains(given))
    )


def _chain():
    """Run :run_id and the runs it goes on from, through ``resume_of_run_id``: each one's kind, chat and notes."""
    r, earlier = tables.runs, tables.runs.alias("earlier")
    start = select(r.c.id, r.c.resume_of_run_id, r.c.kind, r.c.chat_id, r.c.seen_audience)
    chain = start.where(r.c.id == bindparam("run_id", type_=BigInteger)).cte("chain", recursive=True)
    back = select(earlier.c.id, earlier.c.resume_of_run_id, earlier.c.kind, earlier.c.chat_id, earlier.c.seen_audience)
    back = back.join_from(earlier, chain, earlier.c.id == chain.c.resume_of_run_id).where(earlier.c.id < chain.c.id)
    chain = chain.union_all(back)
    return select(chain.c.kind, chain.c.chat_id, chain.c.seen_audience)


# Built once (docs/hub.md, Data access): every tool call of a run's agent may note, and every memory_write reads.
_NOTE = _noted()
_SEEN = _chain()


async def note(conn: AsyncConnection, run_id: int, principals: Iterable[str]) -> None:
    """Add ``principals`` to what the agent of run ``run_id`` was shown, in ``conn``'s transaction."""
    given = sorted(set(principals))
    if given:
        await conn.execute(_NOTE, {"noted_run": run_id, "principals": given})


async def seen(conn: AsyncConnection, run_id: int) -> frozenset[str]:
    """The principals of what the agent of run ``run_id`` was shown (see the module)."""
    found: set[str] = set()
    for kind, chat_id, principals in await conn.execute(_SEEN, {"run_id": run_id}):
        found.update(principals)
        if kind == CHAT_RUN and chat_id is not None:
            found.add(f"chat:{chat_id}")
    return frozenset(found)


def revealed(result: dict) -> frozenset[str]:
    """The audience a kg_* tool result says it reveals: the label of its structuredContent, else of its text's
    READ_META (``kg serve``); none for an error or a result that names no label. An audience that cannot be read
    is the highest (``audience_of``), which no reader holds."""
    data = result.get("structuredContent")
    label = data.get("label") if isinstance(data, dict) else None
    if label is None:
        for block in result.get("content") or []:
            meta = block.get("_meta") if isinstance(block, dict) else None
            read = meta.get(READ_META) if isinstance(meta, dict) else None
            label = read.get("label") if isinstance(read, dict) else label
    return audience_of(label.get("audience")) if isinstance(label, dict) else frozenset()
