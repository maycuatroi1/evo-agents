"""The permissions tasks:write and docs:write: an isolated plugin writes tasks and documents through the plugin API.

Revision ID: 0224
Revises: 0223
Create Date: 2026-11-08

The names below are copies of ``evo_agents.plugins.manifest.PERMISSIONS`` as of this revision, never imports, so a later
change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

project_plugins.permissions takes ``tasks:write``, with which a plugin creates tasks of the project under keys of its
own and changes and comments on those it created (``evo_agents.hub.server.plugin_tasks_api``), and ``docs:write``,
with which it saves documents whose slug starts with its id (``evo_agents.hub.server.plugin_docs_api``). No row
changes: an admin grants them by enabling the plugin again.

Going back takes both out of every grant and restores the check of 0169.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0224"
down_revision = "0223"
branch_labels = None
depends_on = None

CHECK = "project_plugins_permissions_check"
NEW_PERMISSIONS = ("tasks:write", "docs:write")
PERMISSIONS_0169 = (  # project_plugins.permissions as 0169 set it
    "runs:read",
    "runs:dispatch",
    "chats:read",
    "chats:write",
    "plans:read",
    "memories:read",
    "notifications:send",
    "canvas:read",
    "signals:read",
    "srs:read",
    "tasks:read",
    "records:use",
)
PERMISSIONS = (*PERMISSIONS_0169, *NEW_PERMISSIONS)  # evo_agents.plugins.manifest.PERMISSIONS
col = sa.column


def _permissions(names) -> None:
    op.drop_constraint(CHECK, "project_plugins", type_="check")
    granted = col("permissions").op("<@")(postgresql.array(names, type_=sa.Text()))
    op.create_check_constraint(CHECK, "project_plugins", granted)


def upgrade() -> None:
    _permissions(PERMISSIONS)


def downgrade() -> None:
    granted = sa.table("project_plugins", col("permissions"))
    kept = granted.c.permissions
    for permission in NEW_PERMISSIONS:
        kept = sa.func.array_remove(kept, permission)
    op.execute(granted.update().values(permissions=kept))
    _permissions(PERMISSIONS_0169)
