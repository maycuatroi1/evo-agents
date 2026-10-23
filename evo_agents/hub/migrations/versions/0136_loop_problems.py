"""The problem a work item of the Loop goes from, and the kind discover: the engine rules/2.

Revision ID: 0136
Revises: 0135
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.tables_loop`` and ``evo_agents.hub.server.loop.model`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

loop_items gains ``problem_id`` (plan problem-oriented-hub, criterion a6): the problem of the project the work item's
goal answered when the engine rules/2 made it, or NULL (every work item made before, and one whose goal answers none).
Its key goes to problems with ON DELETE SET NULL, and an index holds the work items of each problem. loop_items.kind
takes discover, of cycle business: the work of @strategist on a claim of the Problem cell in testing. The kinds,
cycles and reasons of 0123 stand, so every row of the older revision still satisfies the new check.

Going back deletes the work items of kind discover (their ledger rows go with them), puts the check of 0123 again and
drops the index, the key and the column, and with them the problem of each work item.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None

CORE_KINDS = ("brainstorm", "use_case", "build", "deliver")
PLUGIN_KINDS = ("growth_idea", "growth_draft", "growth_publish", "growth_invite", "growth_review")
OLD_KINDS = (*CORE_KINDS, *PLUGIN_KINDS)
OLD_BUSINESS = ("brainstorm", "use_case", *PLUGIN_KINDS)
DISCOVER = "discover"
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
TABLE = "loop_items"
CHECK = "loop_items_check"
COLUMN = "problem_id"
KEY = "loop_items_problem_id_fkey"
INDEX = "loop_items_problem_idx"

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


def _check(kinds: tuple, business: tuple) -> None:
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.create_check_constraint(CHECK, TABLE, _items(kinds, business))


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.BigInteger(), nullable=True))
    op.create_foreign_key(KEY, TABLE, "problems", [COLUMN], ["id"], ondelete="SET NULL")
    op.create_index(INDEX, TABLE, [COLUMN], postgresql_where=col(COLUMN).is_not(None))
    _check((*OLD_KINDS, DISCOVER), (*OLD_BUSINESS, DISCOVER))


def downgrade() -> None:
    items = sa.table(TABLE, col("id"), col("kind"))
    op.execute(items.delete().where(items.c.kind == DISCOVER))
    _check(OLD_KINDS, OLD_BUSINESS)
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_constraint(KEY, TABLE, type_="foreignkey")
    op.drop_column(TABLE, COLUMN)
