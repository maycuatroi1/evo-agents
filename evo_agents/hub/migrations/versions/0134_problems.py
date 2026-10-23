"""The problems of a project, kept as revisions, the problem a goal answers, and @builder's proposals of one.

Revision ID: 0134
Revises: 0133
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.tables_problems``, ``evo_agents.hub.problems`` and
``evo_agents.hub.tables_settings`` as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions.

problems holds one row per problem of a project (plan problem-oriented-hub, criterion a1): its number (the code
P-<number>, 1 and up, unique in the project) and its newest revision. problem_revisions holds every revision of each
problem, which the hub only inserts: the statement, who is hurt, when it happens and how they cope today (each 1 to
MAX_TEXT_CHARS, not blank), the metric (an object or NULL), the rank (1 to MAX_RANK), the owner, the status (STATUSES)
with its reason (1 to MAX_REASON_CHARS) exactly when it is solved or dropped, the writer's note (1 to MAX_NOTE_CHARS,
or NULL), who wrote it and when. A project's problems go with the project; a member a revision names is not deleted
under it.

goal_revisions gains problem_id (criterion a2): the problem of the goal's project the revision answers, or NULL. Its
foreign key takes no action of its own, so the check waits for the end of the statement: deleting a project deletes
its goals and its problems in one statement. builder_proposals.kind takes ``problem``: @builder proposes a problem or
a change of one, which a writer of the project approves before the hub writes it as the problem's next revision. No
other column of an older table changes.

Going back deletes the proposals of kind problem and their Inbox notices, puts the check of 0091 back on
builder_proposals, drops goal_revisions.problem_id and both tables: the goals of 0133 answer no problem.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None

KINDS_0091 = ("branding", "features", "plugin_config", "agent", "policy", "goal")
PROBLEM = "problem"
KINDS = ("branding", "features", "plugin_config", "agent", "policy", PROBLEM, "goal")  # tables_settings.PROPOSAL_KINDS
STATES = ("pending", "applied", "rejected", "failed", "rolled_back")  # evo_agents.hub.tables_settings.PROPOSAL_STATES
MAX_SUMMARY_CHARS = 500
MAX_ERROR_CHARS = 2000
PROPOSAL_CHECK = "builder_proposals_check"
NOTICE = "builder_proposal"
PROBLEM_STATUSES = ("open", "solved", "dropped")  # evo_agents.hub.problems.STATUSES
OPEN = "open"
MAX_STATEMENT_CHARS = 2000  # evo_agents.hub.problems.MAX_STATEMENT_CHARS
MAX_TEXT_CHARS = 2000  # evo_agents.hub.problems.MAX_TEXT_CHARS
MAX_REASON_CHARS = 1000  # evo_agents.hub.problems.MAX_REASON_CHARS
MAX_NOTE_CHARS = 1000  # evo_agents.hub.problems.MAX_NOTE_CHARS
MAX_RANK = 10_000  # evo_agents.hub.problems.MAX_RANK

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


def _key(table: str, column: str, target: str, ondelete: str | None) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _said(name: str, most: int):
    """``name`` holds 1 to ``most`` characters, and not only blanks."""
    return sa.func.char_length(sa.func.btrim(col(name))).between(1, most)


def _problems() -> None:
    op.create_table(
        "problems",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("problems", "project_id", "projects", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name="problems_pkey"),
        sa.UniqueConstraint("project_id", "number", name="problems_number_key"),
        sa.CheckConstraint(sa.and_(col("number") >= 1, col("revision") >= 1), name="problems_check"),
    )


def _revisions() -> None:
    name, status = "problem_revisions", col("status")
    op.create_table(
        name,
        sa.Column("problem_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("who", sa.Text(), nullable=False),
        sa.Column("when", sa.Text(), nullable=False),
        sa.Column("workaround", sa.Text(), nullable=False),
        sa.Column("metric", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(name, "problem_id", "problems", "CASCADE"),
        _key(name, "owner_id", "users", "RESTRICT"),
        _key(name, "created_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("problem_id", "revision", name="problem_revisions_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("revision") >= 1,
                _said("statement", MAX_STATEMENT_CHARS),
                _said("who", MAX_TEXT_CHARS),
                _said("when", MAX_TEXT_CHARS),
                _said("workaround", MAX_TEXT_CHARS),
                sa.func.jsonb_typeof(col("metric", postgresql.JSONB)) == "object",
                col("rank").between(1, MAX_RANK),
                status.in_(PROBLEM_STATUSES),
                _said("close_reason", MAX_REASON_CHARS),
                (status == OPEN) == col("close_reason").is_(None),
                sa.func.char_length(col("note")).between(1, MAX_NOTE_CHARS),
            ),
            name="problem_revisions_check",
        ),
    )


def upgrade() -> None:
    _problems()
    _revisions()
    op.add_column("goal_revisions", sa.Column("problem_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("goal_revisions_problem_id_fkey", "goal_revisions", "problems", ["problem_id"], ["id"])
    _proposal_check(KINDS)


def downgrade() -> None:
    proposals = sa.table("builder_proposals", col("kind"))
    notifications = sa.table("notifications", col("notice_kind"), col("details", postgresql.JSONB))
    op.execute(
        notifications.delete().where(
            notifications.c.notice_kind == NOTICE, notifications.c.details["kind"].astext == PROBLEM
        )
    )
    op.execute(proposals.delete().where(proposals.c.kind == PROBLEM))
    _proposal_check(KINDS_0091)
    op.drop_constraint("goal_revisions_problem_id_fkey", "goal_revisions", type_="foreignkey")
    op.drop_column("goal_revisions", "problem_id")
    op.drop_table("problem_revisions")
    op.drop_table("problems")
