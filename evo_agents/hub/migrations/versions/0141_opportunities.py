"""The opportunities of a project, kept as revisions, what they answer, their evidence, and @builder's proposals of one.

Revision ID: 0141
Revises: 0140
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.tables_opportunities``, ``evo_agents.hub.opportunities`` and
``evo_agents.hub.tables_settings`` as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions.

opportunities holds one row per opportunity of a project (plan opportunity-intake, criterion a1): its number (the code
O-<number>, 1 and up, unique in the project) and its newest revision. opportunity_revisions holds every revision of
each opportunity, which the hub only inserts: the statement, who benefits and the leverage (each 1 to MAX_TEXT_CHARS,
not blank), the window (a date and a note of 1 to MAX_TEXT_CHARS, both or either NULL), the metric (an object or
NULL), the rank (1 to MAX_RANK), the owner, the status (STATUSES) with its reason (1 to MAX_REASON_CHARS) exactly when
it is not open, the writer's note (1 to MAX_NOTE_CHARS, or NULL), who wrote it and when. opportunity_answers holds the
problems a revision answers, in order, at most MAX_ANSWERS; the problem may be another project's. The opportunities of a
project go with it, and so do the rows that answer one of its problems; a member a revision names is not deleted under
it.

opportunity_evidence holds one row per line of evidence of an opportunity (criterion a2), numbered within it, as
problem_evidence (0137) does a problem's, with SIGNALS: the eight of a problem and seven more. builder_proposals.kind
takes ``opportunity``: @builder proposes an opportunity or a change of one, with lines of evidence, which a writer of
the project approves. No other column of an older table changes.

Going back deletes the proposals of kind opportunity and their Inbox notices, puts the check of 0134 back on
builder_proposals and drops the four tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None

KINDS_0134 = ("branding", "features", "plugin_config", "agent", "policy", "problem", "goal")
OPPORTUNITY = "opportunity"
KINDS = (*KINDS_0134, OPPORTUNITY)  # tables_settings.PROPOSAL_KINDS
STATES = ("pending", "applied", "rejected", "failed", "rolled_back")  # evo_agents.hub.tables_settings.PROPOSAL_STATES
MAX_SUMMARY_CHARS = 500
MAX_ERROR_CHARS = 2000
PROPOSAL_CHECK = "builder_proposals_check"
NOTICE = "builder_proposal"
STATUSES = ("open", "captured", "missed", "dropped")  # evo_agents.hub.opportunities.STATUSES
OPEN = "open"
MAX_STATEMENT_CHARS = 2000
MAX_TEXT_CHARS = 2000
MAX_REASON_CHARS = 1000
MAX_NOTE_CHARS = 1000
MAX_RANK = 10_000
MAX_ANSWERS = 20
SOURCE_KINDS = ("hub_chat", "messenger", "chat_fpt", "email", "file", "harness", "other")
SIGNALS = (
    "complaint",
    "workaround",
    "repeated_request",
    "blocker",
    "commitment",
    "proposed_solution",
    "contradiction",
    "decision_without_reason",
    "wish",
    "external_demand",
    "window",
    "reusable_asset",
    "pull",
    "intro",
    "idle_capacity",
)  # evo_agents.hub.opportunities.SIGNALS
LABELS = ("public", "internal")
MAX_EXCERPT_CHARS = 300
MAX_URI_CHARS = 2000
MAX_LOCATOR_CHARS = 500
MAX_SPEAKER_CHARS = 200
EVIDENCE = "opportunity_evidence"

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


def _said(name: str, most: int, *, optional: bool = False):
    """``name`` holds 1 to ``most`` characters, and not only blanks; an optional one may be NULL."""
    said = sa.func.char_length(sa.func.btrim(col(name))).between(1, most)
    return sa.or_(col(name).is_(None), said) if optional else said


def _opportunities() -> None:
    op.create_table(
        "opportunities",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("opportunities", "project_id", "projects", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name="opportunities_pkey"),
        sa.UniqueConstraint("project_id", "number", name="opportunities_number_key"),
        sa.CheckConstraint(sa.and_(col("number") >= 1, col("revision") >= 1), name="opportunities_check"),
    )


def _revisions() -> None:
    name, status = "opportunity_revisions", col("status")
    op.create_table(
        name,
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("beneficiary", sa.Text(), nullable=False),
        sa.Column("leverage", sa.Text(), nullable=False),
        sa.Column("window_until", sa.Date(), nullable=True),
        sa.Column("window_note", sa.Text(), nullable=True),
        sa.Column("metric", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(name, "opportunity_id", "opportunities", "CASCADE"),
        _key(name, "owner_id", "users", "RESTRICT"),
        _key(name, "created_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("opportunity_id", "revision", name="opportunity_revisions_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("revision") >= 1,
                _said("statement", MAX_STATEMENT_CHARS),
                _said("beneficiary", MAX_TEXT_CHARS),
                _said("leverage", MAX_TEXT_CHARS),
                _said("window_note", MAX_TEXT_CHARS, optional=True),
                sa.func.jsonb_typeof(col("metric", postgresql.JSONB)) == "object",
                col("rank").between(1, MAX_RANK),
                status.in_(STATUSES),
                _said("close_reason", MAX_REASON_CHARS),
                (status == OPEN) == col("close_reason").is_(None),
                sa.func.char_length(col("note")).between(1, MAX_NOTE_CHARS),
            ),
            name="opportunity_revisions_check",
        ),
    )


def _answers() -> None:
    name = "opportunity_answers"
    op.create_table(
        name,
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("problem_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["opportunity_id", "revision"],
            ["opportunity_revisions.opportunity_id", "opportunity_revisions.revision"],
            ondelete="CASCADE",
            name=f"{name}_revision_fkey",
        ),
        _key(name, "problem_id", "problems", "CASCADE"),
        sa.PrimaryKeyConstraint("opportunity_id", "revision", "position", name=f"{name}_pkey"),
        sa.UniqueConstraint("opportunity_id", "revision", "problem_id", name=f"{name}_problem_key"),
        sa.CheckConstraint(col("position").between(1, MAX_ANSWERS), name=f"{name}_check"),
    )
    op.create_index(f"{name}_problem_idx", name, ["problem_id"])


def _evidence() -> None:
    op.create_table(
        EVIDENCE,
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("speaker", sa.Text(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("confirmed_by", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposal_id", sa.BigInteger(), nullable=True),
        _key(EVIDENCE, "opportunity_id", "opportunities", "CASCADE"),
        _key(EVIDENCE, "added_by", "users", "RESTRICT"),
        _key(EVIDENCE, "confirmed_by", "users", "RESTRICT"),
        _key(EVIDENCE, "proposal_id", "builder_proposals", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("opportunity_id", "number", name=f"{EVIDENCE}_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("number") >= 1,
                col("source_kind").in_(SOURCE_KINDS),
                _said("source_uri", MAX_URI_CHARS),
                _said("locator", MAX_LOCATOR_CHARS, optional=True),
                _said("speaker", MAX_SPEAKER_CHARS, optional=True),
                _said("excerpt", MAX_EXCERPT_CHARS),
                col("signal").in_(SIGNALS),
                col("label").in_(LABELS),
                col("confirmed_by").is_(None) == col("confirmed_at").is_(None),
            ),
            name=f"{EVIDENCE}_check",
        ),
    )
    op.create_index(
        f"{EVIDENCE}_proposal_idx", EVIDENCE, ["proposal_id"], postgresql_where=col("proposal_id").is_not(None)
    )


def upgrade() -> None:
    _opportunities()
    _revisions()
    _answers()
    _evidence()
    _proposal_check(KINDS)


def downgrade() -> None:
    proposals = sa.table("builder_proposals", col("kind"))
    notifications = sa.table("notifications", col("notice_kind"), col("details", postgresql.JSONB))
    op.execute(
        notifications.delete().where(
            notifications.c.notice_kind == NOTICE, notifications.c.details["kind"].astext == OPPORTUNITY
        )
    )
    op.execute(proposals.delete().where(proposals.c.kind == OPPORTUNITY))
    _proposal_check(KINDS_0134)
    op.drop_index(f"{EVIDENCE}_proposal_idx", table_name=EVIDENCE)
    op.drop_table(EVIDENCE)
    op.drop_index("opportunity_answers_problem_idx", table_name="opportunity_answers")
    op.drop_table("opportunity_answers")
    op.drop_table("opportunity_revisions")
    op.drop_table("opportunities")
