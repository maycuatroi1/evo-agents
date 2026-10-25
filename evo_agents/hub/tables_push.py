"""The tables of push (0148), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own attributes
(``tables.push_devices``, ``tables.notification_preferences``): queries name them there.

push_devices holds each device a member receives push notifications on: its kind (KINDS: a browser's Web Push
subscription, an iPhone's APNs token, an Android phone's FCM token), its address (the subscription's endpoint, or the
device token) and what its sender needs besides (``params``: the subscription's keys p256dh and auth; an APNs token's
topic and environment), the member's label for it, when it was added and when a push last reached it. A device is
bound to the token that added it (``token_id``): it lives no longer than that sign-in, as a Telegram channel lives no
longer than the web session that linked it. An address speaks for one member: adding it again moves it. Each delivery
of a notification to a device is a row of notification_deliveries with its ``device_id``
(``evo_agents.hub.server.push_store``), which goes with the device.

notification_preferences holds what a member chose for their notifications beyond the notices they turned off
(notification_mutes): ``hide_details``, push notifications that carry only the kind of thing, the project and the
link, as one of a restricted project does. A member without a row has the defaults.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

WEBPUSH, APNS, FCM = "webpush", "apns", "fcm"
KINDS = (WEBPUSH, APNS, FCM)  # push_devices.kind (0148)
APNS_ENVIRONMENTS = ("sandbox", "production")  # the APNs servers a token belongs to
MAX_ADDRESS_CHARS = 2048  # push_devices.address: an endpoint or a device token
MAX_LABEL_CHARS = 100  # push_devices.label


def push_tables(metadata: MetaData) -> tuple[Table, Table]:
    """push_devices and notification_preferences (0148), added to ``metadata``."""
    devices = Table(
        "push_devices",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("user_id", BigInteger, nullable=False),
        Column("token_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("address", Text, nullable=False),
        Column("params", JSONB, nullable=False, server_default="{}"),
        Column("label", Text),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("last_sent_at", DateTime(timezone=True)),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="push_devices_user_id_fkey"),
        ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="CASCADE", name="push_devices_token_id_fkey"),
        PrimaryKeyConstraint("id", name="push_devices_pkey"),
        UniqueConstraint("kind", "address", name="push_devices_kind_address_key"),
        Index("push_devices_user_idx", "user_id"),
        Index("push_devices_token_idx", "token_id"),
    )
    preferences = Table(
        "notification_preferences",
        metadata,
        Column("user_id", BigInteger, nullable=False),
        Column("hide_details", Boolean, nullable=False, server_default=false()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="notification_preferences_user_id_fkey"
        ),
        PrimaryKeyConstraint("user_id", name="notification_preferences_pkey"),
    )
    return devices, preferences
