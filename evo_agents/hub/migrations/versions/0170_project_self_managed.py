"""A self-managed project, and the project a plugin was installed for.

Revision ID: 0170
Revises: 0169
Create Date: 2026-10-28

The names below are copies of ``evo_agents.hub.tables`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

projects gains ``self_managed``, which a hub admin turns on, and ``trusted_owners``, the GitHub owners whose releases the
admins of that project install plugins from. plugins gains ``project_id``: a plugin an admin of a self-managed project
installed from a release belongs to that project alone, and no other project enables or sees it; null for a plugin the
hub admins installed, which every project may enable.

Going back drops the three columns: the plugins that were scoped to a project become plugins of the hub, and every
project stops being self-managed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0170"
down_revision = "0169"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("self_managed", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column(
        "projects",
        sa.Column("trusted_owners", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column("plugins", sa.Column("project_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("plugins_project_id_fkey", "plugins", "projects", ["project_id"], ["id"], ondelete="RESTRICT")


def downgrade() -> None:
    op.drop_constraint("plugins_project_id_fkey", "plugins", type_="foreignkey")
    op.drop_column("plugins", "project_id")
    op.drop_column("projects", "trusted_owners")
    op.drop_column("projects", "self_managed")
