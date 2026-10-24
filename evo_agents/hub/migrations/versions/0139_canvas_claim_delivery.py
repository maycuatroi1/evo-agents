"""How a claim of a canvas is delivered: by code or by an action.

Revision ID: 0139
Revises: 0138
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.tables_canvas`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

canvas_claims gains ``delivery`` (plan problem-intake, criterion a3): ``code`` or ``action``, ``code`` for every claim
made before and for a claim that does not say. A rev's content is JSON, so it holds the field with no change of
canvas_claim_revs; a rev written before reads as ``code``.

loop_items.kind takes ``act``, of cycle business: the work item of the engine rules/2 for a solution claim whose
delivery is ``action`` (a task of the plugin tasks). The kinds, cycles, states and reasons of 0136 stand, so every row of
the older revision still satisfies the new check.

Going back deletes the work items of kind act (their ledger rows go with them), puts the check of 0136 again, and drops
the column, and with it every claim's delivery; the revs keep theirs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None

TABLE = "canvas_claims"
COLUMN = "delivery"
CHECK = "canvas_claims_delivery_check"
DELIVERIES = ("code", "action")  # tables_canvas.DELIVERIES
ACT = "act"
ITEMS = "loop_items"
ITEMS_CHECK = "loop_items_check"
OLD_KINDS = (
    *("brainstorm", "use_case", "build", "deliver", "discover"),
    *("growth_idea", "growth_draft", "growth_publish", "growth_invite", "growth_review"),
)
OLD_BUSINESS = ("brainstorm", "use_case", "discover", *OLD_KINDS[5:])
CYCLES = ("business", "development")
STATES = ("queued", "running", "watching", "waiting_human", "done", "cancelled")
FINISHED = ("done", "cancelled")
REASONS = (
    *("breaker", "concurrency", "rate", "worker", "type_refused"),
    *("stable", "auto_tasks", "split", "merge_queue", "triage"),
    *("srs_review", "srs_finding", "srs_changes", "author_question", "run_decision", "loop_decision"),
    *("merge_blocked", "merge_manual"),
    *("claim_invalidated", "claim_retired", "goal_paused", "goal_closed", "loop_off", "task_closed"),
    *("no_frame_task", "person", "dropped", "manual", "plugin_off"),
)
MAX_KEY_CHARS = 500
MAX_NAME_CHARS = 100

col = sa.column


def _items(kinds: tuple, business: tuple) -> sa.ColumnElement:
    state = col("state")
    return sa.and_(
        col("kind").in_(kinds),
        col("cycle").in_(CYCLES),
        col("kind").in_(business) == (col("cycle") == "business"),
        sa.func.char_length(col("key")).between(1, MAX_KEY_CHARS),
        state.in_(STATES),
        col("reason").in_(REASONS),
        sa.func.jsonb_typeof(col("detail", postgresql.JSONB)) == "object",
        col("attempts") >= 0,
        col("failures") >= 0,
        sa.func.char_length(col("owner")).between(1, MAX_NAME_CHARS),
        sa.func.char_length(col("engine")).between(1, MAX_NAME_CHARS),
        state.in_(FINISHED) == col("finished_at").is_not(None),
    )


def _kinds(kinds: tuple, business: tuple) -> None:
    op.drop_constraint(ITEMS_CHECK, ITEMS, type_="check")
    op.create_check_constraint(ITEMS_CHECK, ITEMS, _items(kinds, business))


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.Text(), server_default="code", nullable=False))
    op.create_check_constraint(CHECK, TABLE, col(COLUMN).in_(DELIVERIES))
    _kinds((*OLD_KINDS, ACT), (*OLD_BUSINESS, ACT))


def downgrade() -> None:
    items = sa.table(ITEMS, col("id"), col("kind"))
    op.execute(items.delete().where(items.c.kind == ACT))
    _kinds(OLD_KINDS, OLD_BUSINESS)
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
