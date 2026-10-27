"""An event plugins may take: one use case of a version in review, approved.

Revision ID: 0171
Revises: 0170
Create Date: 2026-10-27

The names below are copies of ``evo_agents.hub.plugin_events`` and ``evo_agents.hub.hub_events`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

plugin_deliveries.kind takes srs.use_case.approved (plan srs-backlog-loop, criterion a1). hub_events takes the kind
already: its check holds a pattern of dotted names, not a list (0077). No column of a table changes.

Going back deletes the deliveries of the event and its rows of hub_events, which the release before never writes, and
puts the check of 0165 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0171"
down_revision = "0170"
branch_labels = None
depends_on = None

DELIVERY_KINDS_0165 = (  # plugin_deliveries.kind as 0165 set it
    "run.state",
    "chat.message",
    "plan.step",
    "notification",
    "task.result",
    "canvas.claim",
    "canvas.baseline",
    "signal.posted",
    "srs.version.approved",
    "tasks.status",
    "agent.answer",
    "chat.bridge",
)
NEW_EVENT = "srs.use_case.approved"  # evo_agents.hub.hub_events.SRS_USE_CASE_APPROVED
DELIVERY_KINDS = (*DELIVERY_KINDS_0165, NEW_EVENT)  # evo_agents.hub.plugin_events.KINDS
DELIVERY_STATES = ("pending", "delivered", "failed")
NAME_PATTERN = "^[a-z][a-z0-9_-]{0,62}$"
MAX_ERROR_CHARS = 2000

col = sa.column


def _deliveries(kinds) -> None:
    op.drop_constraint("plugin_deliveries_check", "plugin_deliveries", type_="check")
    op.create_check_constraint(
        "plugin_deliveries_check",
        "plugin_deliveries",
        sa.and_(
            col("kind").in_(kinds),
            col("contribution").regexp_match(NAME_PATTERN),
            col("state").in_(DELIVERY_STATES),
            col("attempts") >= 0,
            sa.func.char_length(col("last_error")) <= MAX_ERROR_CHARS,
            (col("state") == "delivered") == col("delivered_at").is_not(None),
        ),
    )


def upgrade() -> None:
    _deliveries(DELIVERY_KINDS)


def downgrade() -> None:
    for name in ("plugin_deliveries", "hub_events"):
        rows = sa.table(name, col("kind"))
        op.execute(rows.delete().where(rows.c.kind == NEW_EVENT))
    _deliveries(DELIVERY_KINDS_0165)
