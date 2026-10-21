"""The goals of a project, kept as revisions, and @builder's proposals of one.

Revision ID: 0091
Revises: 0090
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_goals``, ``evo_agents.hub.goals`` and
``evo_agents.hub.tables_settings`` as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions.

goals holds one row per goal of a project (plan loop-engine, criterion a2): its number (the code G-<number>, 1 and up,
unique in the project), the alias it keeps when the hub made it from a goal of the Curator's charter (a charter goal's
id, unique in the project), and its newest revision. goal_revisions holds every revision of each goal, which the hub
only inserts: the statement (1 to MAX_STATEMENT_CHARS), the metric (an object or NULL), the deadline, the canvas it
drives, the rank (1 to MAX_RANK), the owner, the status (STATUSES) with its reason (CLOSE_REASONS) exactly when it is
closed, the writer's note (1 to MAX_NOTE_CHARS, or NULL), who wrote it and when. A goal has no budget. A project's goals
go with the project; a canvas or a member a revision names is not deleted under it.

builder_proposals.kind takes ``goal``: @builder proposes a goal or a change of one, which an admin of the project
approves before the hub writes it as the goal's next revision. No column of an older table changes.

Going back deletes the proposals of kind goal and their Inbox notices, puts the check of 0079 back on
builder_proposals and drops both tables: the hub of 0090 reads the goals of a charter alone.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None

KINDS_0079 = ("branding", "features", "plugin_config", "agent", "policy")
GOAL = "goal"
KINDS = (*KINDS_0079, GOAL)  # evo_agents.hub.tables_settings.PROPOSAL_KINDS
STATES = ("pending", "applied", "rejected", "failed", "rolled_back")  # evo_agents.hub.tables_settings.PROPOSAL_STATES
MAX_SUMMARY_CHARS = 500
MAX_ERROR_CHARS = 2000
PROPOSAL_CHECK = "builder_proposals_check"
NOTICE = "builder_proposal"
GOAL_STATUSES = ("active", "paused", "closed")  # evo_agents.hub.goals.STATUSES
CLOSE_REASONS = ("achieved", "dropped", "expired")  # evo_agents.hub.goals.CLOSE_REASONS
ALIAS = "^[a-z0-9][a-z0-9-]{0,63}$"  # evo_agents.hub.goals.ALIAS
MAX_STATEMENT_CHARS = 2000  # evo_agents.hub.goals.MAX_STATEMENT_CHARS
MAX_NOTE_CHARS = 1000  # evo_agents.hub.goals.MAX_NOTE_CHARS
MAX_RANK = 10_000  # evo_agents.hub.goals.MAX_RANK

col = sa.column


def _proposal_check(kinds) -> None:
    """The check of builder_proposals as 0043 wrote it, its kind one of ``kinds``."""
    state = col("state")
    op.drop_constraint(PROPOSAL_CHECK, "builder_proposals", type_="check")
    op.create_check_constraint(
        PROPOSAL_CHECK,
        "builder_proposals",
        sa.and_(
            col("kind").in_(kinds),
            state.in_(STATES),
            sa.func.char_length(col("summary")).between(1, MAX_SUMMARY_CHARS),
            sa.func.char_length(col("error")) <= MAX_ERROR_CHARS,
            col("base_version") >= 0,
            (state == "pending") == col("decided_at").is_(None),
            (state == "pending") == col("decided_by").is_(None),
            state.in_(("applied", "rolled_back")) == col("applied_version").is_not(None),
            (state == "rolled_back") == col("rollback_version").is_not(None),
        ),
    )


def _key(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _goals() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("goals", "project_id", "projects", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name="goals_pkey"),
        sa.UniqueConstraint("project_id", "number", name="goals_number_key"),
        sa.UniqueConstraint("project_id", "alias", name="goals_alias_key"),
        sa.CheckConstraint(
            sa.and_(col("number") >= 1, col("revision") >= 1, col("alias").op("~")(ALIAS)), name="goals_check"
        ),
    )


def _revisions() -> None:
    name, status = "goal_revisions", col("status")
    op.create_table(
        name,
        sa.Column("goal_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("metric", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("canvas_id", sa.BigInteger(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(name, "goal_id", "goals", "CASCADE"),
        _key(name, "canvas_id", "canvas_canvases", "RESTRICT"),
        _key(name, "owner_id", "users", "RESTRICT"),
        _key(name, "created_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("goal_id", "revision", name="goal_revisions_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("revision") >= 1,
                sa.func.char_length(sa.func.btrim(col("statement"))).between(1, MAX_STATEMENT_CHARS),
                sa.func.jsonb_typeof(col("metric", postgresql.JSONB)) == "object",
                col("rank").between(1, MAX_RANK),
                status.in_(GOAL_STATUSES),
                col("close_reason").in_(CLOSE_REASONS),
                (status == "closed") == col("close_reason").is_not(None),
                sa.func.char_length(col("note")).between(1, MAX_NOTE_CHARS),
            ),
            name="goal_revisions_check",
        ),
    )


def upgrade() -> None:
    _goals()
    _revisions()
    _proposal_check(KINDS)


def downgrade() -> None:
    proposals = sa.table("builder_proposals", col("kind"))
    notifications = sa.table("notifications", col("notice_kind"), col("details", postgresql.JSONB))
    op.execute(
        notifications.delete().where(
            notifications.c.notice_kind == NOTICE, notifications.c.details["kind"].astext == GOAL
        )
    )
    op.execute(proposals.delete().where(proposals.c.kind == GOAL))
    _proposal_check(KINDS_0079)
    op.drop_table("goal_revisions")
    op.drop_table("goals")
