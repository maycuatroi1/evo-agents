"""The Loop of a project: its state and cursor, its work items and their ledger.

Revision ID: 0087
Revises: 0086
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_loop`` and ``evo_agents.hub.server.loop.model`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

loop_states holds one row per project whose Loop was ever turned on (plan loop-engine, criterion a4): whether it is on,
when it was turned on last, the cursor on hub_events (0 or more), its breaker (armed or tripped) with why, when, and who
reset it when, and when its last tick ended. loop_items holds each work item: its kind (KINDS) and the cycle that kind
belongs to, its key (1 to MAX_KEY_CHARS, unique in the project), its ancestors (goal, claim, SRS document, use case and
version, frame task, plan, run, the work item it came of), its state (STATES) with a reason of REASONS, its detail (an
object), how often a run was handed out for it and how often one failed (0 or more), who it stands for, the engine
that made it, the event of hub_events that made it, and when it was made, changed and finished (exactly when done or
cancelled). loop_item_events is its ledger, only ever added to: a kind of EVENT_KINDS, the state and reason after it,
the run and the event it came of, and a detail object. A project's rows go with the project; a goal, claim, document,
task, run or event deleted under a work item leaves it, its link cleared.

No column of an older table changes. Going back drops the three tables: the hub of 0086 has no Loop.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

KINDS = ("brainstorm", "use_case", "build", "deliver")
BUSINESS_KINDS = ("brainstorm", "use_case")  # of cycle business; the others of cycle development
CYCLES = ("business", "development")
STATES = ("queued", "running", "watching", "waiting_human", "done", "cancelled")
FINISHED = ("done", "cancelled")
REASONS = (
    "breaker",
    "concurrency",
    "rate",
    "worker",
    "type_refused",
    "stable",
    "auto_tasks",
    "split",
    "merge_queue",
    "triage",
    "srs_review",
    "srs_finding",
    "srs_changes",
    "author_question",
    "run_decision",
    "loop_decision",
    "merge_blocked",
    "merge_manual",
    "claim_invalidated",
    "claim_retired",
    "goal_paused",
    "goal_closed",
    "loop_off",
    "task_closed",
    "no_frame_task",
    "person",
    "dropped",
    "manual",
)
EVENT_KINDS = (
    "created",
    "ready",
    "admitted",
    "dispatched",
    "waiting",
    "failed",
    "triaged",
    "asked",
    "done",
    "cancelled",
    "noted",
)
BREAKER_STATES = ("armed", "tripped")
MAX_KEY_CHARS = 500
MAX_REASON_CHARS = 1000
MAX_NAME_CHARS = 100  # of an engine and of an owner
TABLES = ("loop_item_events", "loop_items", "loop_states")  # in the order going back drops them

col = sa.column


def _key(table: str, column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def _object(name: str):
    return sa.func.jsonb_typeof(col(name, postgresql.JSONB)) == "object"


def _states() -> None:
    name = "loop_states"
    op.create_table(
        name,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        _when("enabled_at"),
        sa.Column("cursor", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("breaker", sa.Text(), server_default="armed", nullable=False),
        sa.Column("breaker_reason", sa.Text(), nullable=True),
        _when("tripped_at"),
        sa.Column("resumed_by", sa.BigInteger(), nullable=True),
        _when("resumed_at"),
        _when("ticked_at"),
        _stamp("updated_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "resumed_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("project_id", name="loop_states_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("cursor") >= 0,
                col("breaker").in_(BREAKER_STATES),
                sa.func.char_length(col("breaker_reason")).between(1, MAX_REASON_CHARS),
                sa.or_(col("enabled").is_(sa.false()), col("enabled_at").is_not(None)),
            ),
            name="loop_states_check",
        ),
    )


def _items() -> None:
    name, state = "loop_items", col("state")
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("cycle", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("goal_id", sa.BigInteger(), nullable=True),
        sa.Column("claim_id", sa.BigInteger(), nullable=True),
        sa.Column("srs_document_id", sa.BigInteger(), nullable=True),
        sa.Column("use_case", sa.Text(), nullable=True),
        sa.Column("srs_version", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), server_default="queued", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        _stamp("updated_at"),
        _when("finished_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "goal_id", "goals", ondelete="SET NULL"),
        _key(name, "claim_id", "canvas_claims", ondelete="SET NULL"),
        _key(name, "srs_document_id", "srs_documents", ondelete="SET NULL"),
        _key(name, "task_id", "tasks_items", ondelete="SET NULL"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "parent_id", "loop_items", ondelete="SET NULL"),
        _key(name, "event_id", "hub_events", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="loop_items_pkey"),
        sa.UniqueConstraint("project_id", "key", name="loop_items_key_key"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(KINDS),
                col("cycle").in_(CYCLES),
                col("kind").in_(BUSINESS_KINDS) == (col("cycle") == "business"),
                sa.func.char_length(col("key")).between(1, MAX_KEY_CHARS),
                state.in_(STATES),
                col("reason").in_(REASONS),
                _object("detail"),
                col("attempts") >= 0,
                col("failures") >= 0,
                sa.func.char_length(col("owner")).between(1, MAX_NAME_CHARS),
                sa.func.char_length(col("engine")).between(1, MAX_NAME_CHARS),
                state.in_(FINISHED) == col("finished_at").is_not(None),
            ),
            name="loop_items_check",
        ),
    )
    op.create_index("loop_items_open_idx", name, ["project_id", "state"], postgresql_where=state.not_in(FINISHED))
    op.create_index("loop_items_claim_idx", name, ["claim_id"], postgresql_where=col("claim_id").is_not(None))
    op.create_index("loop_items_run_idx", name, ["run_id"], postgresql_where=col("run_id").is_not(None))


def _events() -> None:
    name = "loop_item_events"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        _stamp("at"),
        _key(name, "item_id", "loop_items", ondelete="CASCADE"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "event_id", "hub_events", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="loop_item_events_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(EVENT_KINDS),
                col("state").in_(STATES),
                col("reason").in_(REASONS),
                _object("detail"),
            ),
            name="loop_item_events_check",
        ),
    )
    op.create_index("loop_item_events_item_idx", name, ["item_id", "id"])
    op.create_index("loop_item_events_run_idx", name, ["run_id"], postgresql_where=col("run_id").is_not(None))


def upgrade() -> None:
    _states()
    _items()
    _events()


def downgrade() -> None:
    for name in TABLES:
        op.drop_table(name)
