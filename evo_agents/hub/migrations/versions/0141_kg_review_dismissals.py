"""The dismissed items of a project's knowledge graph review queue.

Revision ID: 0141
Revises: 0139
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.tables_kg`` and ``evo_agents.kg.review`` as of this revision, never
imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core
expressions.

kg_review_dismissals holds one row per item of a project's review queue a writer dismissed (plan kg-map-and-review,
criterion a5): the item's fingerprint, which holds across builds (``evo_agents.kg.review``), its rule (RULES), the
reason (1 to MAX_REASON_CHARS once trimmed), who dismissed it, when, and the build they saw it in. Dismissing an item
again replaces its row. A project's dismissals go with it, a member a dismissal names is not deleted under it, and a
build that goes leaves the dismissal without one. No other table changes.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0141"
down_revision = "0139"
branch_labels = None
depends_on = None

TABLE = "kg_review_dismissals"
RULES = (
    "dangling_mention",
    "unmentioned_document",
    "cluster_without_knowledge",
    "possible_duplicate",
    "ambiguous_match",
    "bulk_noise",
)
MAX_REASON_CHARS = 1000

col = sa.column


def _key(column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("dismissed_by", sa.BigInteger(), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("build_id", sa.BigInteger(), nullable=True),
        _key("project_id", "projects", ondelete="CASCADE"),
        _key("dismissed_by", "users", ondelete="RESTRICT"),
        _key("build_id", "kg_builds", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("project_id", "fingerprint", name=f"{TABLE}_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("rule").in_(RULES),
                sa.func.char_length(col("fingerprint")).between(1, 100),
                sa.func.char_length(sa.func.btrim(col("reason"))).between(1, MAX_REASON_CHARS),
            ),
            name=f"{TABLE}_check",
        ),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
