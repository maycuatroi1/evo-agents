"""The tables of the files people attach to messages (0109, ``evo_agents.hub.attachments``), which
``evo_agents.hub.tables`` adds to its metadata and exposes as its own attributes (``tables.attachments``,
``tables.run_inbox_attachments``): queries name them there.

attachments holds each file a person uploaded: its uploader, its target (one run or one chat), name, media type,
size and SHA-256 (its bytes are a blob of kind attachment, which the row itself refers to, ``blob_gc``), when it came,
and the message that holds it once one does: a run_inbox row of its run (``inbox_id``) or a message of its chat
(``chat_message_id``), set once. run_inbox_attachments ties a run_inbox row to the files its message carries, so the
worker's inbox hands them out: the files of an owner's message, and those of a chat message a chat run takes in its
inbox (one chat message may reach several runs).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    func,
)


def _fk(table: str, column: str, target: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [target], ondelete="CASCADE", name=f"{table}_{column}_fkey")


def attachment_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(attachments, run_inbox_attachments), added to ``metadata``."""
    files = Table(
        "attachments",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("uploaded_by", BigInteger, nullable=False),
        Column("run_id", BigInteger),
        Column("chat_id", BigInteger),
        Column("name", Text, nullable=False),
        Column("media_type", Text, nullable=False),
        Column("size", BigInteger, nullable=False),
        Column("sha256", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("inbox_id", BigInteger),
        Column("chat_message_id", BigInteger),
        _fk("attachments", "uploaded_by", "users.id"),
        _fk("attachments", "run_id", "runs.id"),
        _fk("attachments", "chat_id", "chats.id"),
        _fk("attachments", "inbox_id", "run_inbox.id"),
        _fk("attachments", "chat_message_id", "chat_messages.id"),
        PrimaryKeyConstraint("id", name="attachments_pkey"),
    )
    Index("attachments_sha256_idx", files.c.sha256)
    Index("attachments_created_at_idx", files.c.created_at)
    Index("attachments_run_id_idx", files.c.run_id, postgresql_where=files.c.run_id.is_not(None))
    Index("attachments_inbox_id_idx", files.c.inbox_id, postgresql_where=files.c.inbox_id.is_not(None))
    Index(
        "attachments_chat_message_id_idx",
        files.c.chat_message_id,
        postgresql_where=files.c.chat_message_id.is_not(None),
    )
    links = Table(
        "run_inbox_attachments",
        metadata,
        Column("inbox_id", BigInteger, nullable=False),
        Column("attachment_id", BigInteger, nullable=False),
        _fk("run_inbox_attachments", "inbox_id", "run_inbox.id"),
        _fk("run_inbox_attachments", "attachment_id", "attachments.id"),
        PrimaryKeyConstraint("inbox_id", "attachment_id", name="run_inbox_attachments_pkey"),
    )
    Index("run_inbox_attachments_attachment_id_idx", links.c.attachment_id)
    return files, links
