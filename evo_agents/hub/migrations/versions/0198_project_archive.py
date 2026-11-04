"""An archived project.

Revision ID: 0198
Revises: 0197
Create Date: 2026-11-04

The names below are copies of ``evo_agents.hub.tables`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

projects gains ``archived_at`` and ``archived_by``, which an admin of the project or a hub admin sets
(``evo_agents.hub.server.project_archive``): an archived project takes no new run, kg push or build, and no night
shift, Loop tick or plugin job runs for it, while everything it holds reads as before. Both are null together
(``projects_archived_check``); no existing project is archived.

Going back drops the two columns and the check: every archived project is live again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0198"
down_revision = "0197"
branch_labels = None
depends_on = None

CHECK = "projects_archived_check"
FOREIGN_KEY = "projects_archived_by_fkey"


def upgrade() -> None:
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("archived_by", sa.BigInteger(), nullable=True))
    op.create_foreign_key(FOREIGN_KEY, "projects", "users", ["archived_by"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint(
        CHECK, "projects", sa.column("archived_at").is_(None) == sa.column("archived_by").is_(None)
    )


def downgrade() -> None:
    op.drop_constraint(CHECK, "projects", type_="check")
    op.drop_constraint(FOREIGN_KEY, "projects", type_="foreignkey")
    op.drop_column("projects", "archived_by")
    op.drop_column("projects", "archived_at")
