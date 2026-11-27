"""The files the messages of a chat carry and those of a run, read from ``attachments``, ``run_inbox_attachments``,
``run_outputs`` and ``run_output_links``, each with what its version shows of the message or the run."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


def _file_columns(table) -> tuple:
    return (table.c.id, table.c.name, table.c.media_type, table.c.size, table.c.sha256)


def _chat_message_columns() -> tuple:
    cm, u = tables.chat_messages, tables.users
    return (
        cm.c.id.label("message_id"),
        cm.c.seq,
        cm.c.thread_root_id,
        cm.c.label,
        cm.c.created_at.label("sent_at"),
        cm.c.author_profile_id,
        u.c.login.label("author_login"),
        u.c.github_id.label("author_github_id"),
    )


class VersionStore:
    async def chat_rows(self, conn: AsyncConnection, chat_id: int) -> tuple[list, list]:
        """(attachments, outputs): every file the messages of chat ``chat_id`` carry, each with its message."""
        a, cm, u = tables.attachments, tables.chat_messages, tables.users
        o, k = tables.run_outputs, tables.run_output_links
        attached = (
            select(*_file_columns(a), *_chat_message_columns())
            .select_from(a.join(cm, cm.c.id == a.c.chat_message_id).outerjoin(u, u.c.id == cm.c.author_user_id))
            .where(cm.c.chat_id == chat_id)
        )
        carried = (
            select(*_file_columns(o), o.c.caption, o.c.run_id, o.c.created_at, *_chat_message_columns())
            .select_from(
                k.join(o, o.c.id == k.c.output_id)
                .join(cm, cm.c.id == k.c.chat_message_id)
                .outerjoin(u, u.c.id == cm.c.author_user_id)
            )
            .where(cm.c.chat_id == chat_id)
            .add_columns(o.c.source)
        )
        return list((await conn.execute(attached)).all()), list((await conn.execute(carried)).all())

    async def run_rows(self, conn: AsyncConnection, run_id: int) -> tuple[list, list]:
        """(attachments, outputs): the files of the messages the inbox of run ``run_id`` took, each with when it was
        sent and by whom, and the outputs of the run."""
        a, k, i, u = tables.attachments, tables.run_inbox_attachments, tables.run_inbox, tables.users
        o = tables.run_outputs
        attached = (
            select(*_file_columns(a), i.c.created_at.label("sent_at"), u.c.login, u.c.github_id, a.c.chat_message_id)
            .select_from(
                k.join(a, a.c.id == k.c.attachment_id).join(i, i.c.id == k.c.inbox_id).join(u, u.c.id == i.c.sent_by)
            )
            .where(i.c.run_id == run_id)
        )
        written = select(*_file_columns(o), o.c.caption, o.c.source, o.c.created_at).where(o.c.run_id == run_id)
        return list((await conn.execute(attached)).all()), list((await conn.execute(written)).all())


STORE = VersionStore()
