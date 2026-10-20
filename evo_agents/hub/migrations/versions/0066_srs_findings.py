"""The conflicts @ba finds in a BA document, and the task run of @ba's review of each round of a version.

Revision ID: 0066
Revises: 0065
Create Date: 2026-10-21

srs_findings holds each conflict the agent of a run of @ba wrote with the tool srs__finding
(``evo_agents.core_plugins.srs.findings``): the document, the version it was found on, the item it is about (null for
the whole document), its kind and level, the sources that show it (a JSON array of at least one), a summary, and its
status. A finding still open has no resolution, settler or moment of settling; a resolved or waived one has a
resolution and the moment, and who settled it when a member did (null when it was settled from the graph). Every row
carries the label of its document. srs_versions.review_run_id names the task run of @ba's review of the version's
current round, which approval waits for while it is queued or running.

The names below are copies of ``evo_agents.hub.tables_srs`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Going back drops srs_findings and its rows and the column review_run_id: schema 0065 keeps no finding and approval
waits for no run.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None

TABLE, VERSIONS = "srs_findings", "srs_versions"
FINDING_KINDS = ("duplicate", "contradiction", "kg_conflict", "overlap")
FINDING_LEVELS = ("info", "advisory", "decide")
FINDING_STATUSES = ("open", "resolved", "waived")
MAX_SOURCES = 20
MAX_FINDING_SUMMARY_CHARS = 2_000
MAX_RESOLUTION_CHARS = 10_000
MAX_CHAT_LINK_CHARS = 500

col = sa.column


def _key(table: str, column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _array(name: str, low: int):
    """``name`` is a JSON array of ``low`` to MAX_SOURCES elements."""
    found = col(name)
    return sa.and_(
        sa.func.jsonb_typeof(found) == "array",
        sa.func.jsonb_array_length(found).between(low, MAX_SOURCES),
    )


def _settled():
    """An open finding carries nothing of a settling; a resolved or waived one its resolution and moment."""
    open_ = col("status") == "open"
    unsettled = sa.and_(col("resolution").is_(None), col("settled_by").is_(None), col("settled_at").is_(None))
    settled = sa.and_(col("resolution").is_not(None), col("settled_at").is_not(None))
    return sa.or_(sa.and_(open_, unsettled), sa.and_(col("status") != "open", settled))


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("resolution_sources", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("settled_by", sa.BigInteger(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chat_link", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(TABLE, "document_id", "srs_documents", ondelete="CASCADE"),
        _key(TABLE, "version_id", VERSIONS, ondelete="CASCADE"),
        _key(TABLE, "item_id", "srs_items", ondelete="CASCADE"),
        _key(TABLE, "settled_by", "users", ondelete="RESTRICT"),
        _key(TABLE, "run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="srs_findings_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(FINDING_KINDS),
                col("level").in_(FINDING_LEVELS),
                col("status").in_(FINDING_STATUSES),
                _array("sources", 1),
                _array("resolution_sources", 0),
                sa.func.char_length(col("summary")).between(1, MAX_FINDING_SUMMARY_CHARS),
                sa.func.char_length(col("resolution")).between(1, MAX_RESOLUTION_CHARS),
                sa.func.char_length(col("chat_link")).between(1, MAX_CHAT_LINK_CHARS),
                sa.func.jsonb_typeof(col("label")) == "object",
                _settled(),
            ),
            name="srs_findings_check",
        ),
    )
    op.create_index("srs_findings_document_idx", TABLE, ["document_id", "id"])
    op.add_column(VERSIONS, sa.Column("review_run_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "srs_versions_review_run_id_fkey", VERSIONS, "runs", ["review_run_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("srs_versions_review_run_idx", VERSIONS, ["review_run_id"])


def downgrade() -> None:
    op.drop_index("srs_versions_review_run_idx", table_name=VERSIONS)
    op.drop_constraint("srs_versions_review_run_id_fkey", VERSIONS, type_="foreignkey")
    op.drop_column(VERSIONS, "review_run_id")
    op.drop_index("srs_findings_document_idx", table_name=TABLE)
    op.drop_table(TABLE)
