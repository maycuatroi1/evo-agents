"""The evidence of a problem: lines of a source that show it hurts.

Revision ID: 0137
Revises: 0136
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.tables_problems`` and ``evo_agents.hub.problems`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

problem_evidence holds one row per line of evidence of a problem (plan problem-intake, criterion a1), numbered within
the problem (the code E-<number>, 1 and up): the kind of its source (SOURCE_KINDS) and its address (1 to MAX_URI_CHARS),
where in the source (1 to MAX_LOCATOR_CHARS, or NULL), who spoke (1 to MAX_SPEAKER_CHARS, or NULL), an excerpt of 1 to
MAX_EXCERPT_CHARS characters, the signal it shows (SIGNALS), its label (LABELS: public or internal; the hub refuses
personal), who added it and when, and who confirmed it and when, both or neither. proposal_id names the proposal of
@builder that made the line, or NULL; the key goes to builder_proposals with ON DELETE SET NULL. A problem's lines go
with it, and a member a line names is not deleted under it. No other table changes.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None

TABLE = "problem_evidence"
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
)
LABELS = ("public", "internal")
MAX_EXCERPT_CHARS = 300
MAX_URI_CHARS = 2000
MAX_LOCATOR_CHARS = 500
MAX_SPEAKER_CHARS = 200

col = sa.column


def _key(column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def _said(name: str, most: int, *, optional: bool = False):
    """``name`` holds 1 to ``most`` characters, and not only blanks; an optional one may be NULL."""
    said = sa.func.char_length(sa.func.btrim(col(name))).between(1, most)
    return sa.or_(col(name).is_(None), said) if optional else said


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("problem_id", sa.BigInteger(), nullable=False),
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
        _key("problem_id", "problems", "CASCADE"),
        _key("added_by", "users", "RESTRICT"),
        _key("confirmed_by", "users", "RESTRICT"),
        _key("proposal_id", "builder_proposals", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("problem_id", "number", name=f"{TABLE}_pkey"),
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
            name=f"{TABLE}_check",
        ),
    )
    op.create_index(f"{TABLE}_proposal_idx", TABLE, ["proposal_id"], postgresql_where=col("proposal_id").is_not(None))


def downgrade() -> None:
    op.drop_index(f"{TABLE}_proposal_idx", table_name=TABLE)
    op.drop_table(TABLE)
