"""Two events plugins may take: an approved version of an SRS document and a task's status set or changed.

Revision ID: 0085
Revises: 0084
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.plugin_events``, ``evo_agents.hub.hub_events`` and
``evo_agents.plugins.manifest`` as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions.

plugin_deliveries.kind takes srs.version.approved and tasks.status (plan loop-engine, criterion a3); project_plugins
.permissions takes srs:read and tasks:read. hub_events takes both kinds already: its check holds a pattern of dotted
names, not a list (0077). No column of a table changes.

Going back deletes the deliveries of the two events and their rows of hub_events, which the release before never
writes, takes the two permissions out of every grant, and puts the checks of 0077 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

DELIVERY_KINDS_0077 = (  # plugin_deliveries.kind as 0077 set it
    "run.state",
    "chat.message",
    "plan.step",
    "notification",
    "task.result",
    "canvas.claim",
    "canvas.baseline",
    "signal.posted",
)
NEW_EVENTS = ("srs.version.approved", "tasks.status")  # evo_agents.hub.hub_events.SRS_APPROVED, TASK_STATUS
DELIVERY_KINDS = (*DELIVERY_KINDS_0077, *NEW_EVENTS)  # evo_agents.hub.plugin_events.KINDS
DELIVERY_STATES = ("pending", "delivered", "failed")
NAME_PATTERN = "^[a-z][a-z0-9_-]{0,62}$"
MAX_ERROR_CHARS = 2000
PERMISSIONS_0077 = (  # project_plugins.permissions as 0077 set it
    "runs:read",
    "runs:dispatch",
    "chats:read",
    "chats:write",
    "plans:read",
    "memories:read",
    "notifications:send",
    "canvas:read",
    "signals:read",
)
NEW_PERMISSIONS = ("srs:read", "tasks:read")
PERMISSIONS = (*PERMISSIONS_0077, *NEW_PERMISSIONS)  # evo_agents.plugins.manifest.PERMISSIONS

col = sa.column


def _replace(table: str, name: str, rule) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, rule)


def _checks(delivery_kinds, permissions) -> None:
    """The checks that name the kinds of deliveries and the permissions."""
    _replace(
        "plugin_deliveries",
        "plugin_deliveries_check",
        sa.and_(
            col("kind").in_(delivery_kinds),
            col("contribution").regexp_match(NAME_PATTERN),
            col("state").in_(DELIVERY_STATES),
            col("attempts") >= 0,
            sa.func.char_length(col("last_error")) <= MAX_ERROR_CHARS,
            (col("state") == "delivered") == col("delivered_at").is_not(None),
        ),
    )
    granted = col("permissions").op("<@")(postgresql.array(permissions, type_=sa.Text()))
    _replace("project_plugins", "project_plugins_permissions_check", granted)


def upgrade() -> None:
    _checks(DELIVERY_KINDS, PERMISSIONS)


def downgrade() -> None:
    for name in ("plugin_deliveries", "hub_events"):
        rows = sa.table(name, col("kind"))
        op.execute(rows.delete().where(rows.c.kind.in_(NEW_EVENTS)))
    granted = sa.table("project_plugins", col("permissions"))
    kept = granted.c.permissions
    for permission in NEW_PERMISSIONS:
        kept = sa.func.array_remove(kept, permission)
    op.execute(granted.update().values(permissions=kept))
    _checks(DELIVERY_KINDS_0077, PERMISSIONS_0077)
