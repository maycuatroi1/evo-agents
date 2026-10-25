"""The devices members receive push notifications on, their deliveries, and the members' notification preferences.

Revision ID: 0156
Revises: 0155
Create Date: 2026-10-25

The names below are copies of ``evo_agents.hub.tables_push`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

push_devices holds one row a device a member receives push notifications on (plan hub-mobile-phase-0, criterion a1):
the member, the token that added it (it goes with that token), its kind (KINDS), its address (a Web Push
subscription's endpoint, or an APNs or FCM device token, 1 to MAX_ADDRESS_CHARS characters), what its sender needs
besides (``params``, a JSON object), a label (1 to MAX_LABEL_CHARS characters, or none), when it was added and when a
push last reached it. An address of a kind is one device. A member's devices go with the member.

notification_deliveries.device_id names the device a delivery goes to: such a delivery has no channel, the device's
deliveries go with it, and a notification has at most one delivery a device. The web's delivery, the one with no
channel, is now the one with neither a channel nor a device: notification_deliveries_web_key is made again with that
predicate.

notification_preferences holds one row a member who chose something there: ``hide_details``, push notifications with
only the kind of thing, the project and the link (criterion a4). A member without a row has the defaults.

Going back deletes the deliveries to devices, drops device_id, puts the web's key of 0010 back and drops both tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None

DEVICES, PREFERENCES, DELIVERIES = "push_devices", "notification_preferences", "notification_deliveries"
KINDS = ("webpush", "apns", "fcm")  # evo_agents.hub.tables_push.KINDS
MAX_ADDRESS_CHARS = 2048  # evo_agents.hub.tables_push.MAX_ADDRESS_CHARS
MAX_LABEL_CHARS = 100  # evo_agents.hub.tables_push.MAX_LABEL_CHARS
WEB_KEY = "notification_deliveries_web_key"
DEVICE_KEY = "notification_deliveries_device_key"
DEVICE_FKEY = "notification_deliveries_device_id_fkey"
ONE_TARGET = "notification_deliveries_one_target_check"

col = sa.column


def _key(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="CASCADE", name=f"{table}_{column}_fkey")


def _web_key(where) -> None:
    op.create_index(WEB_KEY, DELIVERIES, ["notification_id"], unique=True, postgresql_where=where)


def upgrade() -> None:
    op.create_table(
        DEVICES,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        _key(DEVICES, "user_id", "users"),
        _key(DEVICES, "token_id", "tokens"),
        sa.PrimaryKeyConstraint("id", name=f"{DEVICES}_pkey"),
        sa.UniqueConstraint("kind", "address", name=f"{DEVICES}_kind_address_key"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(KINDS),
                sa.func.char_length(col("address")).between(1, MAX_ADDRESS_CHARS),
                sa.or_(col("label").is_(None), sa.func.char_length(col("label")).between(1, MAX_LABEL_CHARS)),
                sa.func.jsonb_typeof(col("params")) == "object",
            ),
            name=f"{DEVICES}_check",
        ),
    )
    op.create_index(f"{DEVICES}_user_idx", DEVICES, ["user_id"])
    op.create_index(f"{DEVICES}_token_idx", DEVICES, ["token_id"])
    op.create_table(
        PREFERENCES,
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("hide_details", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(PREFERENCES, "user_id", "users"),
        sa.PrimaryKeyConstraint("user_id", name=f"{PREFERENCES}_pkey"),
    )
    op.add_column(DELIVERIES, sa.Column("device_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(DEVICE_FKEY, DELIVERIES, DEVICES, ["device_id"], ["id"], ondelete="CASCADE")
    op.create_check_constraint(ONE_TARGET, DELIVERIES, sa.or_(col("channel_id").is_(None), col("device_id").is_(None)))
    op.create_index(DEVICE_KEY, DELIVERIES, ["device_id", "notification_id"], unique=True)
    op.drop_index(WEB_KEY, table_name=DELIVERIES)
    _web_key(sa.and_(col("channel_id").is_(None), col("device_id").is_(None)))


def downgrade() -> None:
    deliveries = sa.table(DELIVERIES, col("device_id"))
    op.execute(deliveries.delete().where(deliveries.c.device_id.is_not(None)))
    op.drop_index(WEB_KEY, table_name=DELIVERIES)
    _web_key(col("channel_id").is_(None))
    op.drop_index(DEVICE_KEY, table_name=DELIVERIES)
    op.drop_constraint(ONE_TARGET, DELIVERIES, type_="check")
    op.drop_constraint(DEVICE_FKEY, DELIVERIES, type_="foreignkey")
    op.drop_column(DELIVERIES, "device_id")
    op.drop_table(PREFERENCES)
    op.drop_table(DEVICES)
