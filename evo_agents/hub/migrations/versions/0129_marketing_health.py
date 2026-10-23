"""The health check of a channel account of the core plugin marketing: a read of the platform with the account's
credential, or of its login session, that posts nothing (plan autonomous-oss-growth, step 22).

Revision ID: 0129
Revises: 0128
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.tables_marketing`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

marketing_channels gains health (unknown until the first check, then ok, failing or n/a for a channel no code posts
on), health_reason (what the check read, or why it failed; never a credential) and health_checked_at. Going back drops
the three columns; the results of the checks are lost, and the next check makes them again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None

HEALTH_STATES = ("unknown", "ok", "failing", "n/a")

col = sa.column


def upgrade() -> None:
    op.add_column("marketing_channels", sa.Column("health", sa.Text(), server_default="unknown", nullable=False))
    op.add_column("marketing_channels", sa.Column("health_reason", sa.Text(), nullable=True))
    op.add_column("marketing_channels", sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "marketing_channels_health_check", "marketing_channels", col("health").in_(HEALTH_STATES)
    )


def downgrade() -> None:
    op.drop_constraint("marketing_channels_health_check", "marketing_channels", type_="check")
    op.drop_column("marketing_channels", "health_checked_at")
    op.drop_column("marketing_channels", "health_reason")
    op.drop_column("marketing_channels", "health")
