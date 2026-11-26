"""The events a turn of the hub runtime writes to its run's log (``hub_runtime``), of the ACP kinds a worker's run writes
(docs/workers.md, section Event kinds), so GET .../runs/{id}/stream shows the turn as it goes.

A round of the model writes one batch when its answer arrives: the answer's text as ``agent_message_chunk`` events, in
parts of at most MESSAGE_PART characters cut at a paragraph or a line where one is near, then a ``tool_call`` for each
call it asks (``status`` in_progress, ``rawInput`` its arguments), then one ``usage_update`` with the round's tokens.
Each call's result is a ``tool_call_update`` as the call ends: ``completed``, or ``failed`` when ``hub_tools.call``
answered with an error or a refusal. A turn writes so a few events a round, never one a token.

The bodies are the worker's: the ACP update without its ``sessionUpdate``. The hub numbers them as it numbers the
events of a worker (``runs.event_seq``, under the run's row lock), masks every value of the agent's accounts and of the
run's leases (``agent_account_mask``), and cuts a body over MAX_EVENT_BODY_BYTES (``runs.fit_event_body``). A run that
holds MAX_RUN_EVENTS events takes no more of them. ``run_spend`` reads a ``usage_update`` of runtime hub as an ACP
agent's; the turn's own row (``spend_store.write_figures``) replaces what it read once the turn ends.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import runs, tables
from evo_agents.hub.server.agent_account_mask import masked, profile_values, run_values
from evo_agents.hub.server.run_state import notify_events
from evo_agents.hub.server.sealing import Sealer

log = logging.getLogger(__name__)

MESSAGE_PART = 16 * 1024  # characters of one agent_message_chunk, a quarter of MAX_EVENT_BODY_BYTES
FAILED_PREFIXES = ("error:", "refused:")  # how hub_tools.call answers a call that did not go through


def message_parts(text: str, size: int = MESSAGE_PART) -> list[str]:
    """``text`` in parts of at most ``size`` characters, each cut after the last paragraph break, else line break, of
    its second half when it has one; nothing for an empty text."""
    parts = []
    while len(text) > size:
        cut = -1
        for mark in ("\n\n", "\n"):
            found = text.rfind(mark, size // 2, size)
            if found >= 0:
                cut = found + len(mark)
                break
        cut = cut if cut > 0 else size
        parts.append(text[:cut])
        text = text[cut:]
    if text:
        parts.append(text)
    return parts


def chunks(text: str | None) -> list[tuple[str, dict]]:
    """The ``agent_message_chunk`` events of an answer's ``text``."""
    return [("agent_message_chunk", {"content": {"type": "text", "text": part}}) for part in message_parts(text or "")]


def tool_call(call: dict) -> tuple[str, dict]:
    """The ``tool_call`` event of a call the model asked (an item of its ``tool_calls``)."""
    function = call.get("function") or {}
    raw = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError:
        arguments = raw
    body = {
        "toolCallId": call.get("id") or "",
        "title": function.get("name") or "",
        "kind": "other",
        "status": "in_progress",
        "rawInput": arguments,
    }
    return "tool_call", body


def tool_result(call: dict, result: str) -> tuple[str, dict]:
    """The ``tool_call_update`` event of a call's ``result``, as ``hub_tools.call`` answered it."""
    status = "failed" if result.startswith(FAILED_PREFIXES) else "completed"
    content = [{"type": "content", "content": {"type": "text", "text": result}}]
    return "tool_call_update", {"toolCallId": call.get("id") or "", "status": status, "content": content}


def usage(model: str | None, data: dict) -> tuple[str, dict]:
    """The ``usage_update`` event of one answer of the model, the gateway's ``data``: that round's tokens."""
    used = data.get("usage") or {}
    tokens = {
        "input_tokens": int(used.get("prompt_tokens") or 0),
        "output_tokens": int(used.get("completion_tokens") or 0),
    }
    return "usage_update", {"usage": tokens, "model": model}


async def write(
    engine: AsyncEngine, sealer: Sealer | None, run_id: int, profile_id: int, events: list[tuple[str, dict]]
) -> int:
    """Append ``events`` (kind and body) to the log of run ``run_id`` in a transaction of their own and wake its
    streams; how many it stored. A failure to write is logged and costs the turn nothing."""
    if not events:
        return 0
    try:
        return await _write(engine, sealer, run_id, profile_id, events)
    except Exception:
        log.exception("hub turn events not written", extra={"run_id": run_id})
        return 0


async def _write(
    engine: AsyncEngine, sealer: Sealer | None, run_id: int, profile_id: int, events: list[tuple[str, dict]]
) -> int:
    r, e = tables.runs, tables.run_events
    async with engine.begin() as conn:
        last = (await conn.execute(select(r.c.event_seq).where(r.c.id == run_id).with_for_update())).scalar_one()
        events = events[: max(0, runs.MAX_RUN_EVENTS - last)]
        if not events:
            return 0
        values = sorted(
            set(await profile_values(conn, sealer, profile_id)) | set(await run_values(conn, sealer, run_id)),
            key=len,
            reverse=True,
        )
        rows = []
        for seq, (kind, body) in enumerate(events, start=last + 1):
            stored, cut = runs.fit_event_body(runs.without_nul(masked(body, values)))
            rows.append({"run_id": run_id, "seq": seq, "kind": kind, "body": stored, "truncated": cut})
        await conn.execute(insert(e), rows)
        await conn.execute(update(r).values(event_seq=r.c.event_seq + len(rows)).where(r.c.id == run_id))
        await notify_events(conn, run_id)
    return len(rows)
