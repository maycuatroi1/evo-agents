"""Who a global skill is for: skills.audience.

Revision ID: 0105
Revises: 0104
Create Date: 2026-10-22

Plan autonomous-oss-growth, criterion a3 (``evo_agents.hub.server.skills`` describes the rule). A global skill is for
``everyone`` signed in, or for hub ``admins`` alone; a project skill is always ``everyone``, its project's grants
deciding who reads it. Every skill that exists takes ``everyone``, so nobody loses a skill on upgrade.

The names below are copies of ``evo_agents.hub.server.skills.AUDIENCES`` as of this revision, never imports, so a later
change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions. No other
column changes.

Going back drops the column: every member sees the skills that were for hub admins alone again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

EVERYONE = "everyone"
AUDIENCES = (EVERYONE, "admins")

col = sa.column


def upgrade() -> None:
    op.add_column("skills", sa.Column("audience", sa.Text(), server_default=EVERYONE, nullable=False))
    op.create_check_constraint(
        "skills_audience_check",
        "skills",
        sa.and_(col("audience").in_(AUDIENCES), sa.or_(col("scope") == "global", col("audience") == EVERYONE)),
    )


def downgrade() -> None:
    op.drop_constraint("skills_audience_check", "skills", type_="check")
    op.drop_column("skills", "audience")
