"""When the event bus last carried a plugin delivery: plugin_deliveries.published_at.

Revision ID: 0230
Revises: 0229
Create Date: 2026-11-09

With the event bus on (EVO_HUB_BROKER_URL, ``evo_agents.hub.bus``), the delivery process publishes the id of each
delivery that falls due on the broker and notes when, so it publishes a delivery once each time it falls due, and
again when nobody claimed it within a while, a message the broker lost (``plugin_events.publishable``). NULL: never
carried, as every delivery of a hub without the bus. The column allows NULL and has no default, so the code before it
runs on the schema after it.

Going back drops the column: the bus publishes every due delivery again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0230"
down_revision = "0229"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plugin_deliveries", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("plugin_deliveries", "published_at")
