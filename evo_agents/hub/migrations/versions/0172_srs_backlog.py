"""A goal may aim at an SRS document, and the hub keeps a ledger of what is delivered of one.

Revision ID: 0172
Revises: 0171
Create Date: 2026-10-27

goal_revisions gains ``srs_document_id`` (the SRS document the goal aims at, NULL for none; NO ACTION, as its problem)
and ``srs_heading`` (the code or title of a heading of that document the goal is limited to, never without a
document). A goal written before has neither. loop_items and the contract loop/1 are not touched.

srs_delivery is new (plan srs-backlog-loop, criterion a2): one row per use case of a document the hub knows a state of
(delivered, undelivered, unknown, delivering), its source (kg, admin, deliver) and its evidence. It starts empty.

Going back drops the table and the two columns.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0172"
down_revision = "0171"
branch_labels = None
depends_on = None

REVISIONS = "goal_revisions"
DOCUMENT = "srs_document_id"
HEADING = "srs_heading"
DOCUMENT_KEY = "goal_revisions_srs_document_id_fkey"
HEADING_CHECK = "goal_revisions_srs_heading_check"
LEDGER = "srs_delivery"
STATES = ("delivered", "undelivered", "unknown", "delivering")
SOURCES = ("kg", "admin", "deliver")

col = sa.column


def _key(column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{LEDGER}_{column}_fkey")


def upgrade() -> None:
    op.add_column(REVISIONS, sa.Column(DOCUMENT, sa.BigInteger(), nullable=True))
    op.add_column(REVISIONS, sa.Column(HEADING, sa.Text(), nullable=True))
    op.create_foreign_key(DOCUMENT_KEY, REVISIONS, "srs_documents", [DOCUMENT], ["id"])
    op.create_check_constraint(HEADING_CHECK, REVISIONS, sa.or_(col(HEADING).is_(None), col(DOCUMENT).is_not(None)))
    op.create_table(
        LEDGER,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("code_key", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("goal_id", sa.BigInteger()),
        sa.Column("item_id", sa.BigInteger()),
        sa.Column("updated_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("project_id", "projects", "CASCADE"),
        _key("document_id", "srs_documents", "CASCADE"),
        _key("goal_id", "goals", "SET NULL"),
        _key("item_id", "loop_items", "SET NULL"),
        _key("updated_by", "users", "SET NULL"),
        sa.CheckConstraint(col("state").in_(STATES), name=f"{LEDGER}_state_check"),
        sa.CheckConstraint(col("source").in_(SOURCES), name=f"{LEDGER}_source_check"),
        sa.PrimaryKeyConstraint("document_id", "code_key", name="srs_delivery_pkey"),
    )
    op.create_index("srs_delivery_project_idx", LEDGER, ["project_id", "state"])


def downgrade() -> None:
    op.drop_index("srs_delivery_project_idx", table_name=LEDGER)
    op.drop_table(LEDGER)
    op.drop_constraint(HEADING_CHECK, REVISIONS, type_="check")
    op.drop_constraint(DOCUMENT_KEY, REVISIONS, type_="foreignkey")
    op.drop_column(REVISIONS, HEADING)
    op.drop_column(REVISIONS, DOCUMENT)
