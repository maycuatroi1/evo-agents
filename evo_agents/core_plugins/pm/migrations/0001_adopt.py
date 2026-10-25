"""Adopt the table of pm the hub's older chain made (0086), as it is, or create it.

Revision ID: pm_0001
Revises:
"""

from alembic import op

from evo_agents.core_plugins.pm import tables
from evo_agents.hub.server import core_plugin_api as hub

revision = "pm_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    hub.adopt_tables(op.get_bind(), tables.metadata, list(tables.NAMES))


def downgrade() -> None:
    for name in reversed(tables.NAMES):
        op.drop_table(name)
