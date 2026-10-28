"""The usage of the runs the reaper ended before this revision, from their usage_update events.

Revision ID: 0183
Revises: 0182
Create Date: 2026-10-29

The names and the function below are copies of ``evo_agents.hub.server.reaped_usage`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

A worker reports a run's usage with the report that ends it, so a run the reaper ended (lost or failed once its lease
ran out, failed past its timeout, cancelled once parked too long) kept none, and the figures counted it as free. The
hub now gives such a run the usage its ``usage_update`` events add up to as it ends it; this revision does the same
once for every run that ended ``failed``, ``lost`` or ``cancelled`` before it with a null usage and events of that
kind still kept (the events of a run that ended more than EVO_HUB_RUN_LOG_DAYS ago are pruned). It writes only nulls,
so a usage a worker reported stays and a second pass changes nothing. A lost run whose next attempt went on in its
session is left out: that attempt's total holds its spend already.

Going back changes nothing: a usage written here is data the hub of 0182 reads as it reads any other.
"""

import re
from itertools import pairwise

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0183"
down_revision = "0182"
branch_labels = None
depends_on = None

ENDS = ("failed", "lost", "cancelled")
CODEX, OPENCODE, CLAUDE_CODE = "codex", "opencode", "claude-code"
CAMEL = re.compile(r"(?<!^)(?=[A-Z])")

col = sa.column


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _amount(cost) -> float | None:
    if not isinstance(cost, dict) or cost.get("currency") != "USD":
        return None
    amount = cost.get("amount")
    return float(amount) if _number(amount) and amount >= 0 else None


def _usage_of_events(runtime: str, bodies) -> dict | None:
    """``reaped_usage.usage_of_events`` as of this revision."""
    tokens: dict = {}
    amounts: list[float] = []
    for body in bodies:
        if not isinstance(body, dict):
            continue
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        numbers = {key: value for key, value in usage.items() if _number(value)}
        if runtime == CODEX:
            tokens = {CAMEL.sub("_", key).lower(): value for key, value in numbers.items()} or tokens
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


def upgrade() -> None:
    bind = op.get_bind()
    runs = sa.table(
        "runs",
        col("id"),
        col("state"),
        col("runtime"),
        col("usage", postgresql.JSONB),
        col("parent_run_id"),
        col("session_id"),
    )
    events = sa.table("run_events", col("run_id"), col("seq"), col("kind"), col("body", postgresql.JSONB))
    later = runs.alias("later")
    told = sa.exists().where(events.c.run_id == runs.c.id, events.c.kind == "usage_update")
    went_on = sa.exists().where(later.c.parent_run_id == runs.c.id, later.c.session_id == runs.c.session_id)
    bare = sa.select(runs.c.id, runs.c.runtime).where(runs.c.state.in_(ENDS), runs.c.usage.is_(None), told, ~went_on)
    for run_id, runtime in bind.execute(bare.order_by(runs.c.id)).all():
        of_run = sa.select(events.c.body).where(events.c.run_id == run_id, events.c.kind == "usage_update")
        usage = _usage_of_events(runtime, bind.execute(of_run.order_by(events.c.seq)).scalars())
        if usage is not None:
            bind.execute(runs.update().values(usage=usage).where(runs.c.id == run_id, runs.c.usage.is_(None)))


def downgrade() -> None:
    """Nothing to undo: a usage written here is data the hub of 0182 reads as it reads any other."""
