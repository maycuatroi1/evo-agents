"""The files people attach to messages to a run's agent and in group chats, and messages of files with no text.

Revision ID: 0109
Revises: 0108
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.attachments`` and ``evo_agents.hub.tables_attachments`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

attachments holds each file a person uploaded: its uploader, its target (exactly one of a run and a chat), name,
media type, size (at most MAX_FILE_BYTES), SHA-256 and when it came; its bytes are a blob of kind attachment. The
message that holds it is set once: a run_inbox row of its run (inbox_id) or a message of its chat (chat_message_id).
run_inbox_attachments ties a run_inbox row to the files its message carries, a chat message's in each inbox it
reaches. run_inbox.body and chat_messages.body may be empty: a message of files alone has no text, which the hub
checks before it writes one.

Going back gives each empty body a placeholder, restores the checks of 0009 and 0031, and drops the two tables; the
blobs of kind attachment lose the rows that referred to them and wait for the collection of unreferenced blobs.
"""

import sqlalchemy as sa
from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None

MAX_NAME_CHARS = 255  # evo_agents.hub.attachments.MAX_NAME_CHARS
MAX_MEDIA_TYPE_CHARS = 255  # two parts of at most 127 and a slash (evo_agents.hub.outputs.MEDIA_TYPE)
MAX_FILE_BYTES = 50 * 1024 * 1024  # evo_agents.hub.attachments.MAX_FILE_BYTES
MAX_INBOX_BYTES = 8 * 1024  # evo_agents.hub.runs.MAX_MESSAGE_BYTES, as 0009 bounds run_inbox.body
MAX_CHAT_BYTES = 16 * 1024  # evo_agents.hub.chats.MAX_MESSAGE_BYTES, as 0031 bounds chat_messages.body
SHA256 = "^[0-9a-f]{64}$"
PLACEHOLDER = "(files)"  # the body of a message of files alone, going back
BODIES = (
    ("run_inbox", "run_inbox_body_check", MAX_INBOX_BYTES),
    ("chat_messages", "chat_messages_body_check", MAX_CHAT_BYTES),
)

col = sa.column


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target], ondelete="CASCADE", name=f"{table}_{column}_fkey")


def _bodies(least: int) -> None:
    for table, name, most in BODIES:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, sa.func.octet_length(col("body")).between(least, most))


def _partial(name: str, column: str) -> None:
    op.create_index(name, "attachments", [column], postgresql_where=col(column).is_not(None))


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("uploaded_by", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("inbox_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_message_id", sa.BigInteger(), nullable=True),
        _fk("attachments", "uploaded_by", "users.id"),
        _fk("attachments", "run_id", "runs.id"),
        _fk("attachments", "chat_id", "chats.id"),
        _fk("attachments", "inbox_id", "run_inbox.id"),
        _fk("attachments", "chat_message_id", "chat_messages.id"),
        sa.PrimaryKeyConstraint("id", name="attachments_pkey"),
        sa.CheckConstraint(
            sa.and_(
                sa.func.num_nonnulls(col("run_id"), col("chat_id")) == 1,
                sa.or_(col("inbox_id").is_(None), col("run_id").is_not(None)),
                sa.or_(col("chat_message_id").is_(None), col("chat_id").is_not(None)),
                sa.func.char_length(col("name")).between(1, MAX_NAME_CHARS),
                sa.func.char_length(col("media_type")).between(3, MAX_MEDIA_TYPE_CHARS),
                col("size").between(0, MAX_FILE_BYTES),
                col("sha256").regexp_match(SHA256),
            ),
            name="attachments_check",
        ),
    )
    op.create_index("attachments_sha256_idx", "attachments", ["sha256"])
    op.create_index("attachments_created_at_idx", "attachments", ["created_at"])
    _partial("attachments_run_id_idx", "run_id")
    _partial("attachments_inbox_id_idx", "inbox_id")
    _partial("attachments_chat_message_id_idx", "chat_message_id")
    op.create_table(
        "run_inbox_attachments",
        sa.Column("inbox_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        _fk("run_inbox_attachments", "inbox_id", "run_inbox.id"),
        _fk("run_inbox_attachments", "attachment_id", "attachments.id"),
        sa.PrimaryKeyConstraint("inbox_id", "attachment_id", name="run_inbox_attachments_pkey"),
    )
    op.create_index("run_inbox_attachments_attachment_id_idx", "run_inbox_attachments", ["attachment_id"])
    _bodies(0)


def downgrade() -> None:
    for table, _, _ in BODIES:
        bodies = sa.table(table, col("body"))
        op.execute(sa.update(bodies).where(bodies.c.body == "").values(body=PLACEHOLDER))
    _bodies(1)
    op.drop_table("run_inbox_attachments")
    op.drop_table("attachments")
