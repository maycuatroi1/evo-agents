"""The kinds and the reason of the work items a core plugin's Loop rules make: extension point loop_rules.

Revision ID: 0123
Revises: 0122
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.server.loop.model`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

loop_items.kind takes the five kinds of the growth plugin (growth_idea, growth_draft, growth_publish, growth_invite,
growth_review), all of cycle business; loop_items.reason and loop_item_events.reason take plugin_off, the reason of a
work item cancelled because the plugin that made it is turned off in the project. The core kinds, cycles and reasons
stand as 0093 has them, so every row of the older revision still satisfies the new checks.

Going back puts the checks of 0093 again and first deletes the work items of the new kinds (their ledger rows go with
them), and the ledger rows and work items with the reason plugin_off take reason manual.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None

OLD_KINDS = ("brainstorm", "use_case", "build", "deliver")
PLUGIN_KINDS = ("growth_idea", "growth_draft", "growth_publish", "growth_invite", "growth_review")
CYCLES = ("business", "development")
STATES = ("queued", "running", "watching", "waiting_human", "done", "cancelled")
FINISHED = ("done", "cancelled")
OLD_REASONS = (
    *("breaker", "concurrency", "rate", "worker", "type_refused"),
    *("stable", "auto_tasks", "split", "merge_queue", "triage"),
    *("srs_review", "srs_finding", "srs_changes", "author_question", "run_decision", "loop_decision"),
    *("merge_blocked", "merge_manual"),
    *("claim_invalidated", "claim_retired", "goal_paused", "goal_closed", "loop_off", "task_closed"),
    *("no_frame_task", "person", "dropped", "manual"),
)
REASONS = (*OLD_REASONS, "plugin_off")
EVENT_KINDS = (
    *("created", "ready", "admitted", "dispatched", "waiting", "failed"),
    *("triaged", "asked", "done", "cancelled", "noted"),
)
MAX_KEY_CHARS = 500
MAX_NAME_CHARS = 100

col = sa.column


def _object(name: str):
    return sa.func.jsonb_typeof(col(name, postgresql.JSONB)) == "object"


def _items(kinds: tuple, business: tuple, reasons: tuple) -> sa.ColumnElement:
    state = col("state")
    return sa.and_(
        col("kind").in_(kinds),
        col("cycle").in_(CYCLES),
        col("kind").in_(business) == (col("cycle") == "business"),
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
    _swap(
        "loop_items",
        "loop_items_check",
        _items((*OLD_KINDS, *PLUGIN_KINDS), ("brainstorm", "use_case", *PLUGIN_KINDS), REASONS),
    )
    _swap("loop_item_events", "loop_item_events_check", _events(REASONS))


def downgrade() -> None:
    items = sa.table("loop_items", col("id"), col("kind"), col("reason"))
    events = sa.table("loop_item_events", col("id"), col("reason"))
    op.execute(items.delete().where(items.c.kind.in_(PLUGIN_KINDS)))
    op.execute(items.update().where(items.c.reason == "plugin_off").values(reason="manual"))
    op.execute(events.update().where(events.c.reason == "plugin_off").values(reason="manual"))
    _swap("loop_items", "loop_items_check", _items(OLD_KINDS, ("brainstorm", "use_case"), OLD_REASONS))
    _swap("loop_item_events", "loop_item_events_check", _events(OLD_REASONS))
