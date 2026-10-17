"""The table of the tests' core plugin notes (``tests.hub.core_notes``), a revision the tests append to the hub's chain
of migrations, after its head, as a core plugin of evo-agents puts its tables in that chain (criterion a22 of
hub-agent-plugins).

Revision ID: 9001
Revises: the hub's head
"""

import sqlalchemy as sa
from alembic import op

from evo_agents.hub.migrate import script_location

revision = "9001"
down_revision = max(path.name[:4] for path in (script_location() / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notes_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="notes_entries_project_fkey"),
        sa.PrimaryKeyConstraint("id", name="notes_entries_pkey"),
    )


def downgrade() -> None:
    op.drop_table("notes_entries")
