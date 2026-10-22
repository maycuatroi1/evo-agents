"""The reviews of single use cases of a BA document: srs_use_case_reviews.

Revision ID: 0098
Revises: 0097
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.tables_srs`` as of this revision, never imports, so a later change to
it takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

srs_use_case_reviews holds each approval or request for changes a writer gave one use case of a version in review (plan
srs-use-case-review): the version it was given on, the item (the use case's identity across versions), the reviewer, the
role it counts for, the decision and a note. A later review of the same reviewer on the same use case replaces the
earlier one when the reviews are counted, and the earlier one stays: no uniqueness here. Which reviews count for a
later version is decided when they are read (``evo_agents.core_plugins.srs.use_case_reviews``), never stored.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None

TABLE = "srs_use_case_reviews"
REVIEW_ROLES = ("ba", "qa", "dev")
REVIEW_DECISIONS = ("approve", "request_changes")
MAX_REVIEW_CHARS = 10_000

col = sa.column


def _key(column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewer_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("version_id", "srs_versions", ondelete="CASCADE"),
        _key("item_id", "srs_items", ondelete="CASCADE"),
        _key("reviewer_id", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("role").in_(REVIEW_ROLES),
                col("decision").in_(REVIEW_DECISIONS),
                sa.func.char_length(col("body")) <= MAX_REVIEW_CHARS,
            ),
            name=f"{TABLE}_check",
        ),
    )
    op.create_index(f"{TABLE}_version_idx", TABLE, ["version_id", "item_id"])


def downgrade() -> None:
    op.drop_table(TABLE)
