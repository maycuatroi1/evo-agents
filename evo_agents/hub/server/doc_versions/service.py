"""The versions of a document in a chat (``chat_versions``, ``in_chat``) and in a run (``run_versions``, ``in_run``):
the files the repository reads (``VersionStore``) kept by their key and what the reader may read, each with the URL of
its bytes and its author, oldest first."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import outputs
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.views import HUB_AUTHOR, ChatAuthor, agent_author, person_author
from evo_agents.hub.server.chats.repository.views import profiles_by_id
from evo_agents.hub.server.chats.service.access import chat_access, label_reader
from evo_agents.hub.server.doc_versions.models import ATTACHED, DocVersion, DocVersions, doc_key
from evo_agents.hub.server.doc_versions.repository import STORE
from evo_agents.hub.server.outputs.models import chat_url, run_url
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import Principal


def _version(row, source: str, url: str, **found) -> DocVersion:
    shown: dict[str, Any] = {"message_id": None, "seq": None, "thread_root_id": None, "run_id": None, "author": None}
    return DocVersion(
        source=source,
        id=row.id,
        name=row.name,
        media_type=row.media_type,
        viewer=outputs.viewer(row.media_type),
        size=row.size,
        sha256=row.sha256,
        caption=getattr(row, "caption", None),
        url=url,
        **(shown | found),
    )


def _oldest_first(found: list[tuple[tuple, DocVersion]]) -> list[DocVersion]:
    return [version for _, version in sorted(found, key=lambda pair: pair[0])]


# A chat


def _chat_author(row, profiles) -> ChatAuthor:
    if row.author_profile_id is not None and row.author_profile_id in profiles:
        return agent_author(profiles[row.author_profile_id])
    if row.author_login is not None:
        return person_author(row.author_login, row.author_github_id)
    return HUB_AUTHOR


def _in_chat(row, profiles) -> dict:
    return {
        "message_id": row.message_id,
        "seq": row.seq,
        "thread_root_id": row.thread_root_id,
        "author": _chat_author(row, profiles),
    }


async def chat_versions(conn: AsyncConnection, user_id: int, chat_id: int, key: str) -> list[DocVersion]:
    """The versions of document ``key`` in chat ``chat_id`` that person ``user_id`` reads, oldest first."""
    attached, carried = await STORE.chat_rows(conn, chat_id)
    attached = [row for row in attached if doc_key(row.name) == key]
    carried = [row for row in carried if doc_key(row.name) == key]
    reader = await label_reader(conn, user_id, [row.label for row in (*attached, *carried)])
    attached = [row for row in attached if reader.sees(row.label)]
    carried = [row for row in carried if reader.sees(row.label)]
    profiles = await profiles_by_id(conn, {row.author_profile_id for row in (*attached, *carried)} - {None})
    found: list[tuple[tuple, DocVersion]] = []
    for row in attached:
        url = f"/v1/chats/{chat_id}/attachments/{row.id}/content"
        version = _version(row, ATTACHED, url, created_at=row.sent_at, **_in_chat(row, profiles))
        found.append(((row.seq, 0, row.id), version))
    seen: set[int] = set()
    for row in sorted(carried, key=lambda row: (row.seq, row.id)):
        if row.id in seen:  # one output carried by two messages is one version, at the first
            continue
        seen.add(row.id)
        url = chat_url(chat_id, row.id)
        version = _version(
            row, row.source, url, created_at=row.created_at, run_id=row.run_id, **_in_chat(row, profiles)
        )
        found.append(((row.seq, 1, row.id), version))
    return _oldest_first(found)


async def in_chat(engine: AsyncEngine, user: Principal, chat_id: int, name: str) -> DocVersions:
    """The versions of the document ``name`` in chat ``chat_id``, to ``user``, a member of it."""
    key = doc_key(name)
    async with UnitOfWork(engine) as conn:
        await chat_access(conn, user, chat_id)
        versions = await chat_versions(conn, user.user_id, chat_id, key)
    return DocVersions(key=key, versions=versions)


# A run


async def run_versions(conn: AsyncConnection, project: str, run_id: int, key: str) -> list[DocVersion]:
    """The versions of document ``key`` in run ``run_id`` of ``project``, oldest first: the files of the messages its
    inbox took and the outputs of the run."""
    attached, written = await STORE.run_rows(conn, run_id)
    found: list[tuple[tuple, DocVersion]] = []
    seen: set[int] = set()
    for row in sorted(attached, key=lambda row: (row.sent_at, row.id)):
        if doc_key(row.name) != key or row.id in seen:  # a file two inbox rows took is one version
            continue
        seen.add(row.id)
        url = f"/v1/projects/{quote(project, safe='')}/runs/{run_id}/attachments/{row.id}/content"
        author = person_author(row.login, row.github_id)
        version = _version(row, ATTACHED, url, created_at=row.sent_at, message_id=row.chat_message_id, author=author)
        found.append(((row.sent_at, 0, row.id), version))
    for row in written:
        if doc_key(row.name) == key:
            version = _version(
                row, row.source, run_url(project, run_id, row.id), created_at=row.created_at, run_id=run_id
            )
            found.append(((row.created_at, 1, row.id), version))
    return _oldest_first(found)


async def in_run(
    engine: AsyncEngine, user: Principal, project: str, run_id: int, name: str, sink: str | None
) -> DocVersions:
    """The versions of the document ``name`` in run ``run_id`` of ``project``, to ``user``, a reader of the run through
    ``sink``."""
    key = doc_key(name)
    async with UnitOfWork(engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        versions = await run_versions(conn, project, run_id, key)
    return DocVersions(key=key, versions=versions)
