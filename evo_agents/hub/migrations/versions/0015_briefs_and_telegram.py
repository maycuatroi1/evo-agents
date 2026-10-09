"""The Curator's morning brief, and the Telegram channel: its one-time link codes and the messages it sent.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-08

``evo_agents.hub.server.brief`` and ``evo_agents.hub.server.telegram`` describe the design; the lists and bounds below
are copies of their constants as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the
tables.

notifications.notice_kind takes a fifth value, 'curator_brief': the morning brief of a project's Curator, which the hub
sends its schedule's owner at the charter's brief_at.

curator_briefs holds one brief per project and local day of the charter's time zone: the night it reports on, the
member it went to, what it said (a JSON object), and the notification that carried it.

telegram_links holds the one-time codes a member links a Telegram chat with: the SHA-256 of the code (never the code),
the member, when it was made, when it expires (10 minutes later) and when it was used.

notification_deliveries.external_id is the id a channel's service gave the message it sent, such as Telegram's
message_id, so a reply to that message finds its notification.

Going back deletes the briefs' notifications, drops the two tables and the column, and puts the check of 0014 back,
which is the schema that schema 0014 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

NOTICE_KINDS_0014 = ("push_default_branch", "merge_default_branch", "plan_finished", "run_failed")
NOTICE_KINDS = (*NOTICE_KINDS_0014, "curator_brief")  # evo_agents.hub.runs.NOTICE_KINDS
CODE_HASH_CHARS = 64  # a SHA-256 in hex
LINK_SECONDS = 600  # evo_agents.hub.telegram.LINK_SECONDS
MAX_EXTERNAL_ID_CHARS = 200
MAX_BRIEF_BYTES = 64 * 1024  # evo_agents.hub.server.brief.MAX_BRIEF_BYTES

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _notice_kinds(kinds) -> None:
    op.create_check_constraint("notifications_notice_kind_check", "notifications", col("notice_kind").in_(kinds))


def upgrade() -> None:
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    _notice_kinds(NOTICE_KINDS)

    op.create_table(
        "curator_briefs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("night", sa.Date(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("notification_id", sa.BigInteger(), nullable=True),
        _stamp("created_at"),
        sa.CheckConstraint(
            sa.and_(
                sa.func.jsonb_typeof(col("body")) == "object",
                sa.func.octet_length(sa.cast(col("body"), sa.Text())) <= MAX_BRIEF_BYTES,
            ),
            name="curator_briefs_body_check",
        ),
        sa.CheckConstraint(col("night") <= col("day"), name="curator_briefs_night_check"),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], name="curator_briefs_notification_id_fkey"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="curator_briefs_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="curator_briefs_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="curator_briefs_pkey"),
        sa.UniqueConstraint("project_id", "day", name="curator_briefs_project_id_day_key"),
    )

    op.create_table(
        "telegram_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        _stamp("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            sa.and_(sa.func.char_length(col("code_hash")) == CODE_HASH_CHARS, col("code_hash").op("~")("^[0-9a-f]+$")),
            name="telegram_links_code_hash_check",
        ),
        sa.CheckConstraint(
            sa.extract("epoch", col("expires_at") - col("created_at")).between(1, LINK_SECONDS),
            name="telegram_links_expires_at_check",
        ),
        sa.CheckConstraint(col("used_at") >= col("created_at"), name="telegram_links_used_at_check"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="telegram_links_user_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="telegram_links_pkey"),
        sa.UniqueConstraint("code_hash", name="telegram_links_code_hash_key"),
    )
    op.create_index("telegram_links_user_idx", "telegram_links", ["user_id"])

    op.add_column("notification_deliveries", sa.Column("external_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "notification_deliveries_external_id_check",
        "notification_deliveries",
        sa.func.char_length(col("external_id")).between(1, MAX_EXTERNAL_ID_CHARS),
    )
    op.create_index(
        "notification_deliveries_external_idx",
        "notification_deliveries",
        ["channel_id", "external_id"],
        postgresql_where=col("external_id").is_not(None),
    )


def downgrade() -> None:
    notifications = sa.table("notifications", col("notice_kind"))
    op.drop_index("notification_deliveries_external_idx", table_name="notification_deliveries")
    op.drop_constraint("notification_deliveries_external_id_check", "notification_deliveries", type_="check")
    op.drop_column("notification_deliveries", "external_id")
    op.drop_index("telegram_links_user_idx", table_name="telegram_links")
    op.drop_table("telegram_links")
    op.drop_table("curator_briefs")
    op.drop_constraint("notifications_notice_kind_check", "notifications", type_="check")
    op.execute(notifications.delete().where(notifications.c.notice_kind == "curator_brief"))
    _notice_kinds(NOTICE_KINDS_0014)
