"""The notes of an entry.

Revision ID: northwind_ledger_0002
Revises: northwind_ledger_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "northwind_ledger_0002"
down_revision = "northwind_ledger_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "northwind_ledger_notes",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "entry_id",
            sa.BigInteger,
            sa.ForeignKey("northwind_ledger_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_northwind_ledger_notes_entry_id", "northwind_ledger_notes", ["entry_id"])


def downgrade() -> None:
    op.drop_table("northwind_ledger_notes")
