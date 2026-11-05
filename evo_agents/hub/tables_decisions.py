"""The tables of decisions and notifications (0010, 0014), which ``evo_agents.hub.tables`` adds to its metadata and
exposes as its own attributes (``tables.decisions``, ``tables.run_inbox``, ``tables.notifications``,
``tables.notification_channels``, ``tables.notification_deliveries``): queries name them there.

decisions holds each question a run asked a member and its answer; run_inbox the messages members sent a run, an
answer among them, until the run took them. notifications holds what the hub tells a member, notification_channels
the channels a member linked, and notification_deliveries each notification sent on a channel, to a push device or
to the web, with its attempts.
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
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB


def _stamp(name: str) -> Column:
    """A timestamptz column that defaults to now() and is never NULL."""
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    """A timestamptz column that is NULL until it happens."""
    return Column(name, DateTime(timezone=True))


def _id() -> Column:
    """``id bigint GENERATED ALWAYS AS IDENTITY``."""
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def decision_tables(metadata: MetaData) -> tuple[Table, Table]:
    """decisions and run_inbox (0010, 0014), added to ``metadata``."""
    decisions = Table(
        "decisions",
        metadata,
        _id(),
        Column("run_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("plan_id", Text),  # NULL for a decision an author run asked before it put a plan (schema 0029)
        Column("step_key", Text),
        Column("category", Text, nullable=False),
        Column("question", Text, nullable=False),
        Column("context", Text),
        Column("options", JSONB, nullable=False),
        Column("state", Text, nullable=False, server_default="open"),
        Column("answer_option", Text),
        Column("answer_text", Text),
        Column("answered_by", BigInteger),
        _stamp("asked_at"),
        _when("answered_at"),
        _when("delivered_at"),
        Column("asked_of", BigInteger),  # who answers it, as the run's principal routes it; NULL before 0115: its owner
        _when("wake_at"),  # a wait (category wait, 0140): when the hub answers it at the latest
        Column("wake_on", JSONB),  # and the event of hub_events it wakes on sooner: kind, contains, after (0140)
        _when("held_at"),  # when the person asked last chose an option that leaves it open (0140)
        Column("recommended_reason", Text),  # why the agent recommends its option (0155)
        Column("reversible", Boolean),  # whether the act it asks about can be undone (0155)
        Column("undo", Text),  # and how (0155)
        Column(
            "evidence", JSONB(none_as_null=True)
        ),  # the refs it rests on, an array of texts; None is SQL NULL (0155)
        # what a decision of category credential asks for: env_var, secret_name, hub_account; never a value (0167)
        Column("credential_request", JSONB(none_as_null=True)),
        # a merge approval: the head and the scope of the request its answer holds for (0184, ``approval_heads``)
        Column("approval", JSONB(none_as_null=True)),
        ForeignKeyConstraint(["answered_by"], ["users.id"], ondelete="RESTRICT", name="decisions_answered_by_fkey"),
        ForeignKeyConstraint(["asked_of"], ["users.id"], ondelete="RESTRICT", name="decisions_asked_of_fkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="decisions_project_id_fkey"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="decisions_run_id_fkey"),
        PrimaryKeyConstraint("id", name="decisions_pkey"),
        Index("decisions_run_idx", "run_id", "id"),
    )
    Index("decisions_project_idx", decisions.c.project_id, decisions.c.id.desc())
    Index("decisions_open_idx", decisions.c.run_id, postgresql_where=decisions.c.state == "open")

    run_inbox = Table(
        "run_inbox",
        metadata,
        _id(),
        Column("run_id", BigInteger, nullable=False),
        Column("sent_by", BigInteger, nullable=False),
        Column("body", Text, nullable=False),
        _stamp("created_at"),
        _when("delivered_at"),
        Column("decision_id", BigInteger),
        ForeignKeyConstraint(["decision_id"], ["decisions.id"], ondelete="CASCADE", name="run_inbox_decision_id_fkey"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_inbox_run_id_fkey"),
        ForeignKeyConstraint(["sent_by"], ["users.id"], ondelete="RESTRICT", name="run_inbox_sent_by_fkey"),
        PrimaryKeyConstraint("id", name="run_inbox_pkey"),
    )
    Index(
        "run_inbox_pending_idx", run_inbox.c.run_id, run_inbox.c.id, postgresql_where=run_inbox.c.delivered_at.is_(None)
    )
    return decisions, run_inbox


def notification_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """notifications, notification_channels and notification_deliveries (0010, 0014), added to ``metadata``."""
    notifications = Table(
        "notifications",
        metadata,
        _id(),
        Column("user_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("notice_kind", Text),
        Column("project_id", BigInteger),
        Column("run_id", BigInteger),
        Column("decision_id", BigInteger),
        Column("title", Text, nullable=False),
        Column("body", Text),
        Column("details", JSONB),
        Column("link", Text),
        _stamp("created_at"),
        _when("read_at"),
        Column("proposal_id", BigInteger),
        ForeignKeyConstraint(
            ["decision_id"], ["decisions.id"], ondelete="CASCADE", name="notifications_decision_id_fkey"
        ),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="notifications_project_id_fkey"),
        ForeignKeyConstraint(
            ["proposal_id"], ["proposals.id"], ondelete="CASCADE", name="notifications_proposal_id_fkey"
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="notifications_run_id_fkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="notifications_user_id_fkey"),
        PrimaryKeyConstraint("id", name="notifications_pkey"),
    )
    Index("notifications_user_idx", notifications.c.user_id, notifications.c.id.desc())
    Index("notifications_unread_idx", notifications.c.user_id, postgresql_where=notifications.c.read_at.is_(None))
    Index(
        "notifications_decision_key",
        notifications.c.decision_id,
        notifications.c.user_id,
        unique=True,
        postgresql_where=notifications.c.decision_id.is_not(None),
    )
    Index(
        "notifications_proposal_key",
        notifications.c.proposal_id,
        notifications.c.user_id,
        unique=True,
        postgresql_where=notifications.c.proposal_id.is_not(None),
    )

    notification_channels = Table(
        "notification_channels",
        metadata,
        _id(),
        Column("user_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("config", JSONB, nullable=False, server_default="{}"),
        Column("enabled", Boolean, nullable=False, server_default=true()),
        _stamp("created_at"),
        Column("token_id", BigInteger),  # the web session that linked the channel, which it lives no longer than (0016)
        ForeignKeyConstraint(
            ["token_id"], ["tokens.id"], ondelete="CASCADE", name="notification_channels_token_id_fkey"
        ),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="notification_channels_user_id_fkey"),
        PrimaryKeyConstraint("id", name="notification_channels_pkey"),
        UniqueConstraint("user_id", "kind", name="notification_channels_user_id_kind_key"),
        Index("notification_channels_token_idx", "token_id"),
    )

    notification_deliveries = Table(
        "notification_deliveries",
        metadata,
        _id(),
        Column("notification_id", BigInteger, nullable=False),
        Column("channel_id", BigInteger),
        Column("state", Text, nullable=False, server_default="pending"),
        Column("attempts", SmallInteger, nullable=False, server_default="0"),
        _stamp("next_at"),
        Column("last_error", Text),
        _stamp("created_at"),
        _when("delivered_at"),
        Column("external_id", Text),
        Column("device_id", BigInteger),  # a delivery to a push device, never with a channel (0156)
        ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channels.id"],
            ondelete="CASCADE",
            name="notification_deliveries_channel_id_fkey",
        ),
        ForeignKeyConstraint(
            ["device_id"], ["push_devices.id"], ondelete="CASCADE", name="notification_deliveries_device_id_fkey"
        ),
        ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            ondelete="CASCADE",
            name="notification_deliveries_notification_id_fkey",
        ),
        PrimaryKeyConstraint("id", name="notification_deliveries_pkey"),
        Index("notification_deliveries_channel_key", "notification_id", "channel_id", unique=True),
        Index("notification_deliveries_device_key", "device_id", "notification_id", unique=True),
    )
    Index(  # the web's delivery: no channel and no device (0156)
        "notification_deliveries_web_key",
        notification_deliveries.c.notification_id,
        unique=True,
        postgresql_where=notification_deliveries.c.channel_id.is_(None) & notification_deliveries.c.device_id.is_(None),
    )
    Index(
        "notification_deliveries_due_idx",
        notification_deliveries.c.next_at,
        postgresql_where=notification_deliveries.c.state == "pending",
    )
    Index(  # a reply to a message a channel sent finds its notification (0015)
        "notification_deliveries_external_idx",
        notification_deliveries.c.channel_id,
        notification_deliveries.c.external_id,
        postgresql_where=notification_deliveries.c.external_id.is_not(None),
    )
    return notifications, notification_channels, notification_deliveries
