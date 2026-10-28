"""The usage of a run the reaper ends: what its ``usage_update`` events add up to.

A worker reports a run's usage with the report that ends it, so a run the reaper ends (``run_state``: lost or failed
when its lease ran out, failed past its timeout, cancelled once parked too long) kept none, and the figures and the
cost of its plan counted it as free (finding #95 of the Curator: runs #626, #707, #818 and #820 of evo-agents spent
over $341 that way). ``move_run`` gives such a run, while its usage is null, the usage its events tell
(``keep_usage``), in the shape its worker would have reported (``usage_of_events``), so ``curator.run_cost`` reads its
cost. A usage the worker reported is kept. A move to ``done`` gives none: the reaper ends a run done only when a new
run resumes it in its session, whose total holds the parked run's spend. The next attempt of a lost plan run that goes
on in its session (``lost_attempt`` of its claim) reports a total that holds the lost run's spend too: the figures that
count a session once (``night_cost``) count it once, those that add runs up twice.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from itertools import pairwise

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

ENDS = ("failed", "lost", "cancelled")  # the moves of the reaper that give a run the usage of its events
CODEX, OPENCODE, CLAUDE_CODE = "codex", "opencode", "claude-code"
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")  # Codex's app-server names its tokens in camelCase


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _amount(cost) -> float | None:
    """The amount in USD of an event's ``cost``, ``{"amount", "currency": "USD"}``; None for any other."""
    if not isinstance(cost, Mapping) or cost.get("currency") != "USD":
        return None
    amount = cost.get("amount")
    return float(amount) if _number(amount) and amount >= 0 else None


def usage_of_events(runtime: str | None, bodies: Iterable) -> dict | None:
    """The usage the ``usage_update`` events of a run of ``runtime`` add up to (their bodies, oldest first), as its
    worker reports it at the end: Codex's tokens are the thread's total of the last event, in snake_case; any other
    runtime's are summed over the events. The cost is opencode's of each step added up, under ``cost``; else the
    session's running total, under ``total_cost_usd`` for Claude Code and ``cost`` for an ACP agent: the last amount,
    plus each amount it fell back from, a new session counting from 0. None when the events say nothing."""
    tokens: dict = {}
    amounts: list[float] = []
    for body in bodies:
        if not isinstance(body, Mapping):
            continue
        usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
        numbers = {key: value for key, value in usage.items() if _number(value)}
        if runtime == CODEX:
            tokens = {_CAMEL.sub("_", key).lower(): value for key, value in numbers.items()} or tokens
        else:
            for key, value in numbers.items():
                tokens[key] = tokens.get(key, 0) + value
        amount = _amount(body.get("cost"))
        if amount is not None:
            amounts.append(amount)
    if not amounts:
        return tokens or None
    if runtime == OPENCODE:
        return {**tokens, "cost": sum(amounts)}
    total = sum(before for before, after in pairwise(amounts) if after < before) + amounts[-1]
    return {**tokens, "total_cost_usd" if runtime == CLAUDE_CODE else "cost": total}


async def keep_usage(conn: AsyncConnection, run_id: int) -> bool:
    """Give run ``run_id`` the usage its ``usage_update`` events add up to (``usage_of_events``) while its usage is
    null, in the caller's transaction; whether it wrote one."""
    r, e = tables.runs, tables.run_events
    runtime = (await conn.execute(select(r.c.runtime).where(r.c.id == run_id, r.c.usage.is_(None)))).scalar()
    if runtime is None:  # a usage of its worker's, or no such run
        return False
    told = select(e.c.body).where(e.c.run_id == run_id, e.c.kind == "usage_update").order_by(e.c.seq)
    usage = usage_of_events(runtime, (await conn.execute(told)).scalars())
    if usage is None:
        return False
    await conn.execute(update(r).values(usage=usage).where(r.c.id == run_id, r.c.usage.is_(None)))
    return True
