"""A decision's label, and the edits of the messages channels sent of a decision: decisions.label and four columns of
notification_deliveries.

Revision ID: 0265
Revises: 0264
Create Date: 2026-11-23

decisions gains label, the level of the project's ladder the agent gave the decision when it asked (``worker ask
--label``): NULL for a decision asked without one, and for every decision asked before this revision, which then reads
as the project's default label. It is 1 to 64 characters.

notification_deliveries gains:

- edit_of, the delivery whose message a delivery edits: once a decision is answered, cancelled or expired, each channel
  that sent its message gets one more delivery of the same notification that edits it (``notifications.queue_edits``),
  so the unique index of a notification and a channel holds for the first deliveries alone, and a delivery is edited
  once (``notification_deliveries_edit_key``). Deleting the first delivery deletes its edit;
- withheld, why a channel sent a message without the notification's content: label (the decision's label keeps it off
  the channel), default_label (a decision without a label, its project's default label and hub sink keeping it off)
  or project (a proposal or a notice its project keeps off); NULL for a message with its content;
- settled_at, when the hub last needed nothing more of a delivery: set once the message of a decision that is no
  longer open went out, or its edit was queued. Every row before this revision is settled now, but a delivery of a
  decision still open, so going live queues no edit of an old message;
- and the index of the deliveries a sweep reads: delivered on a channel with the id of the message, not yet settled.

Going back deletes the edits, puts back the unique index of a notification and a channel over every row, and drops the
columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0265"
down_revision = "0264"
branch_labels = None
depends_on = None

MAX_LABEL_CHARS = 64  # evo_agents.hub.server.decision_models.MAX_LABEL_CHARS, a copy
WITHHELD = ("label", "default_label", "project")  # evo_agents.hub.server.member_channels.WITHHELD, a copy

col = sa.column


def upgrade() -> None:
    op.add_column("decisions", sa.Column("label", sa.Text(), nullable=True))
    op.create_check_constraint(
        "decisions_label_check", "decisions", sa.func.char_length(col("label")).between(1, MAX_LABEL_CHARS)
    )
    op.add_column("notification_deliveries", sa.Column("edit_of", sa.BigInteger(), nullable=True))
    op.add_column("notification_deliveries", sa.Column("withheld", sa.Text(), nullable=True))
    op.add_column("notification_deliveries", sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "notification_deliveries_edit_of_fkey",
        "notification_deliveries",
        "notification_deliveries",
        ["edit_of"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "notification_deliveries_withheld_check", "notification_deliveries", col("withheld").in_(WITHHELD)
    )
    op.create_index("notification_deliveries_edit_key", "notification_deliveries", ["edit_of"], unique=True)
    op.drop_index("notification_deliveries_channel_key", table_name="notification_deliveries")
    op.create_index(
        "notification_deliveries_channel_key",
        "notification_deliveries",
        ["notification_id", "channel_id"],
        unique=True,
        postgresql_where=col("edit_of").is_(None),
    )
    unsettled = (
        col("settled_at").is_(None)
        & col("edit_of").is_(None)
        & col("channel_id").is_not(None)
        & col("external_id").is_not(None)
        & (col("state") == "delivered")
    )
    op.create_index(
        "notification_deliveries_unsettled_idx",
        "notification_deliveries",
        ["notification_id"],
        postgresql_where=unsettled,
    )
    deliveries = sa.table(
        "notification_deliveries",
        col("notification_id", sa.BigInteger()),
        col("settled_at", sa.DateTime(timezone=True)),
        col("edit_of", sa.BigInteger()),
    )
    notifications = sa.table("notifications", col("id", sa.BigInteger()), col("decision_id", sa.BigInteger()))
    decisions = sa.table("decisions", col("id", sa.BigInteger()), col("state", sa.Text()))
    open_decision = (
        sa.select(sa.literal(1))
        .select_from(notifications.join(decisions, decisions.c.id == notifications.c.decision_id))
        .where(notifications.c.id == deliveries.c.notification_id, decisions.c.state == "open")
        .exists()
    )
    op.execute(sa.update(deliveries).where(~open_decision).values(settled_at=sa.func.now()))


def downgrade() -> None:
    deliveries = sa.table("notification_deliveries", col("edit_of", sa.BigInteger()))
    op.execute(sa.delete(deliveries).where(deliveries.c.edit_of.is_not(None)))
    op.drop_index("notification_deliveries_unsettled_idx", table_name="notification_deliveries")
    op.drop_index("notification_deliveries_channel_key", table_name="notification_deliveries")
    op.create_index(
        "notification_deliveries_channel_key", "notification_deliveries", ["notification_id", "channel_id"], unique=True
    )
    op.drop_index("notification_deliveries_edit_key", table_name="notification_deliveries")
    op.drop_column("notification_deliveries", "settled_at")
    op.drop_column("notification_deliveries", "withheld")
    op.drop_column("notification_deliveries", "edit_of")
    op.drop_column("decisions", "label")
