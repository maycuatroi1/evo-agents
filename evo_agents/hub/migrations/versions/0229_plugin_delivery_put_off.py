"""The tries of a plugin delivery the plugin asked to put off: plugin_deliveries.put_off.

Revision ID: 0229
Revises: 0228
Create Date: 2026-11-09

A core plugin whose exception carries ``retry_after`` asks for its delivery to be tried again that many seconds later
(``evo_agents.hub.plugin_events``). Such a try still counts in ``attempts``, and in ``put_off`` too: the tries that
failed otherwise (``attempts - put_off``) wait the backoff and fail the delivery at MAX_ATTEMPTS, the tries put off at a
limit of their own, wide enough for a group chat busy for some minutes. A delivery puts off no more tries than it had
(plugin_deliveries_put_off_check).

Going back drops the column: every try counts as a failed one again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0229"
down_revision = "0228"
branch_labels = None
depends_on = None


col = sa.column


def upgrade() -> None:
    op.add_column("plugin_deliveries", sa.Column("put_off", sa.SmallInteger(), server_default="0", nullable=False))
    rule = sa.and_(col("put_off") >= 0, col("put_off") <= col("attempts"))
    op.create_check_constraint("plugin_deliveries_put_off_check", "plugin_deliveries", rule)


def downgrade() -> None:
    op.drop_column("plugin_deliveries", "put_off")  # its check goes with it
