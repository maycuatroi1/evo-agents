"""The reason of a build an admin's fix of a goal's baseline cancelled.

Revision ID: 0269
Revises: 0268
Create Date: 2026-11-25

The names below are copies of ``evo_agents.hub.server.loop.model`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

loop_items.reason and loop_item_events.reason take ``baseline``: the reason of a build of the Loop that an admin's fix of
a goal's baseline cancelled because it took the build's use cases (a use case built outside the project, set unknown or
delivered with ``cancel_builds``). The kinds, cycles, states and reasons of 0139 (loop_items) and 0123
(loop_item_events) stand, so every row of the older revision still satisfies the new checks.

Going back gives the work items and ledger rows of reason baseline the reason manual, which 0123 has, and puts the
older checks again.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0269"
down_revision = "0268"
branch_labels = None
depends_on = None

ITEMS, ITEMS_CHECK = "loop_items", "loop_items_check"
EVENTS, EVENTS_CHECK = "loop_item_events", "loop_item_events_check"
KINDS = (
    *("brainstorm", "use_case", "build", "deliver", "discover"),
    *("growth_idea", "growth_draft", "growth_publish", "growth_invite", "growth_review"),
    "act",
)
BUSINESS = ("brainstorm", "use_case", "discover", *KINDS[5:])
CYCLES = ("business", "development")
STATES = ("queued", "running", "watching", "waiting_human", "done", "cancelled")
FINISHED = ("done", "cancelled")
OLD_REASONS = (
    *("breaker", "concurrency", "rate", "worker", "type_refused"),
    *("stable", "auto_tasks", "split", "merge_queue", "triage"),
    *("srs_review", "srs_finding", "srs_changes", "author_question", "run_decision", "loop_decision"),
    *("merge_blocked", "merge_manual"),
    *("claim_invalidated", "claim_retired", "goal_paused", "goal_closed", "loop_off", "task_closed"),
    *("no_frame_task", "person", "dropped", "manual", "plugin_off"),
)
BASELINE = "baseline"
REASONS = (*OLD_REASONS, BASELINE)
EVENT_KINDS = (
    *("created", "ready", "admitted", "dispatched", "waiting", "failed"),
    *("triaged", "asked", "done", "cancelled", "noted"),
)
MAX_KEY_CHARS = 500
MAX_NAME_CHARS = 100

col = sa.column


def _object(name: str):
    return sa.func.jsonb_typeof(col(name, postgresql.JSONB)) == "object"


def _items(reasons: tuple) -> sa.ColumnElement:
    state = col("state")
    return sa.and_(
        col("kind").in_(KINDS),
        col("cycle").in_(CYCLES),
        col("kind").in_(BUSINESS) == (col("cycle") == "business"),
        sa.func.char_length(col("key")).between(1, MAX_KEY_CHARS),
        state.in_(STATES),
        col("reason").in_(reasons),
        _object("detail"),
        col("attempts") >= 0,
        col("failures") >= 0,
        sa.func.char_length(col("owner")).between(1, MAX_NAME_CHARS),
        sa.func.char_length(col("engine")).between(1, MAX_NAME_CHARS),
        state.in_(FINISHED) == col("finished_at").is_not(None),
    )


def _events(reasons: tuple) -> sa.ColumnElement:
    return sa.and_(
        col("kind").in_(EVENT_KINDS), col("state").in_(STATES), col("reason").in_(reasons), _object("detail")
    )


def _swap(table: str, name: str, check: sa.ColumnElement) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, check)


def upgrade() -> None:
    _swap(ITEMS, ITEMS_CHECK, _items(REASONS))
    _swap(EVENTS, EVENTS_CHECK, _events(REASONS))


def downgrade() -> None:
    items = sa.table(ITEMS, col("id"), col("reason"))
    events = sa.table(EVENTS, col("id"), col("reason"))
    op.execute(items.update().where(items.c.reason == BASELINE).values(reason="manual"))
    op.execute(events.update().where(events.c.reason == BASELINE).values(reason="manual"))
    _swap(ITEMS, ITEMS_CHECK, _items(OLD_REASONS))
    _swap(EVENTS, EVENTS_CHECK, _events(OLD_REASONS))
