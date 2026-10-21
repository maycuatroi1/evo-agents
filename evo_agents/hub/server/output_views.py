"""How the outputs of runs read (``evo_agents.hub.outputs``): the ``RunOutput`` every answer carries, and the outputs of
a run, of questions and of chat messages, each with the URL its reader fetches the bytes from.

An output's ``url`` is a path of this hub that answers its bytes to whoever may read what listed it: the run's
readers for the outputs of a run (GET /v1/projects/{p}/runs/{id}/outputs/{oid}/content), the decision's readers for
the outputs a question carries (GET /v1/projects/{p}/decisions/{id}/outputs/{oid}/content), and the chat's members
who read the message for those a chat message carries (GET /v1/chats/{id}/outputs/{oid}/content). ``?download=true``
asks for it as an attachment (``evo_agents.hub.server.output_content``).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import outputs, tables
from evo_agents.hub.db import one_of


class RunOutput(BaseModel):
    id: int
    run_id: int
    source: Literal[outputs.SOURCES] = Field(
        description="sent: a file the agent sent; tool: an image taken out of a tool's result; outputs: a file of "
        "the run's .evo-run/outputs/"
    )
    name: str = Field(description="the file's name, or its path below .evo-run/outputs/")
    media_type: str = Field(description="type/subtype, as the machine that sent it named it")
    viewer: Literal[outputs.VIEWERS] = Field(description="what the web opens it with; none offers a download only")
    size: int = Field(description="bytes")
    sha256: str
    caption: str | None
    tool_call_id: str | None = Field(description="the tool call of the run's trace it came from, when one")
    created_at: datetime
    url: str = Field(
        description="where this hub answers its bytes to the reader of what listed it; ?download=true for a download"
    )


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
    )


def run_url(project: str, run_id: int, output_id: int) -> str:
    return f"/v1/projects/{quote(project, safe='')}/runs/{run_id}/outputs/{output_id}/content"


def decision_url(project: str, decision_id: int, output_id: int) -> str:
    return f"/v1/projects/{quote(project, safe='')}/decisions/{decision_id}/outputs/{output_id}/content"


def chat_url(chat_id: int, output_id: int) -> str:
    return f"/v1/chats/{chat_id}/outputs/{output_id}/content"


def view(row, url: str) -> RunOutput:
    fields = {name: getattr(row, name) for name in RunOutput.model_fields if name not in ("url", "viewer")}
    return RunOutput(**fields, viewer=outputs.viewer(row.media_type), url=url)


async def of_run(
    conn: AsyncConnection, project: str, run_id: int, sources: list[str] | None = None, after: int = 0
) -> list[RunOutput]:
    """The outputs of run ``run_id`` of ``project`` after id ``after``, of ``sources`` (every source when empty or
    None), oldest first, each with the run's URL of its bytes."""
    o = tables.run_outputs
    query = select(*columns()).where(o.c.run_id == run_id, o.c.id > after)
    if sources:
        query = query.where(o.c.source.in_(sources))
    rows = (await conn.execute(query.order_by(o.c.id))).all()
    return [view(row, run_url(project, run_id, row.id)) for row in rows]


async def by_decision(conn: AsyncConnection, project: str, decision_ids: list[int]) -> dict[int, list[RunOutput]]:
    """The outputs each of ``decision_ids`` carries, oldest first, each with that decision's URL of its bytes."""
    if not decision_ids:
        return {}
    o, k = tables.run_outputs, tables.run_output_links
    query = (
        select(k.c.decision_id, *columns())
        .join_from(k, o, o.c.id == k.c.output_id)
        .where(one_of(k.c.decision_id, decision_ids))
        .order_by(o.c.id)
    )
    found: dict[int, list[RunOutput]] = defaultdict(list)
    for row in (await conn.execute(query)).all():
        found[row.decision_id].append(view(row, decision_url(project, row.decision_id, row.id)))
    return found


async def by_message(conn: AsyncConnection, chat_id: int, message_ids: list[int]) -> dict[int, list[RunOutput]]:
    """The outputs each of ``message_ids`` of chat ``chat_id`` carries, oldest first, each with the chat's URL of its
    bytes."""
    if not message_ids:
        return {}
    o, k = tables.run_outputs, tables.run_output_links
    query = (
        select(k.c.chat_message_id, *columns())
        .join_from(k, o, o.c.id == k.c.output_id)
        .where(one_of(k.c.chat_message_id, message_ids))
        .order_by(o.c.id)
    )
    found: dict[int, list[RunOutput]] = defaultdict(list)
    for row in (await conn.execute(query)).all():
        found[row.chat_message_id].append(view(row, chat_url(chat_id, row.id)))
    return found
