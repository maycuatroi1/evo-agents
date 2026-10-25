"""Adopt the entries table an older chain of the hub made, as it is, or create it.

Revision ID: northwind_ledger_0001
Revises:
"""

from alembic import op

from evo_agents.hub.server import core_plugin_api as hub
from northwind_ledger import tables

revision = "northwind_ledger_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    hub.adopt_tables(op.get_bind(), tables.metadata, ["northwind_ledger_entries"])


def downgrade() -> None:
    op.drop_table("northwind_ledger_entries")
