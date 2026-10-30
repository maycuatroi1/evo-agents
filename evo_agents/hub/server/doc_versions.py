"""The versions of a document in a chat or a run (docs/hub.md, section "Versions of a document"): every file of one
name that its messages carry, oldest first, so the web's document panel shows what each version changed even when the
messages of the older ones are not on the page it loaded.

- GET /v1/chats/{id}/doc-versions?name=, to a member of the chat (``chat_access``): the files people attached to its
  messages and the outputs its agents' messages carry, threads included, of the messages whose label the caller
  clears (``chat_access.label_reader``), in the order of the messages, a message's attachments before its outputs;
- GET /v1/projects/{p}/runs/{id}/doc-versions?name=, to the run's readers (``views.readable_run``, by sink): the files
  of the messages its inbox took and the outputs of the run, in the order they came.

A document is named as the web names it (``docKey`` of web/src/components/chats/doc-model.ts): the last part of a
file's name after a slash, without case, so an output kept under a path is a version of the file attached under its
own name. Each version carries the URL of its bytes on the route that serves it to the same reader
(``attachments``, ``output_content``). Anyone else gets the 404 of a chat or run that does not exist. Nothing here
is stored: the versions are read from ``attachments``, ``run_inbox_attachments``, ``run_outputs`` and
``run_output_links``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import outputs, tables
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.chat_access import chat_access, label_reader
from evo_agents.hub.server.chat_members import ChatId
from evo_agents.hub.server.chat_views import HUB_AUTHOR, ChatAuthor, agent_author, person_author, profiles_by_id
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.output_content import Sink
from evo_agents.hub.server.output_views import chat_url, run_url
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import CurrentUser

ATTACHED = "attached"
MAX_QUERY_CHARS = 1024  # an output's name may carry its path below .evo-run/outputs/
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 422)}
DocName = Annotated[
    str,
    Query(
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description="the document's name; only its last part after a slash counts, without case",
    ),
]

chat_router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})


class DocVersion(BaseModel):
    source: Literal[(ATTACHED, *outputs.SOURCES)] = Field(
        description="attached: a file a person attached to a message; sent, tool or outputs: an output of a run"
    )
    id: int = Field(description="the attachment's id, or the output's: the two are counted apart")
    name: str = Field(description="the file's name; an output's may carry its path below .evo-run/outputs/")
    media_type: str
    viewer: Literal[outputs.VIEWERS] = Field(description="what the web opens it with; none offers a download only")
    size: int = Field(description="bytes")
    sha256: str
    caption: str | None = Field(description="an output's caption; null for an attachment")
    created_at: datetime = Field(description="when its message was sent, or when the run sent the output")
    url: str = Field(description="where this hub answers its bytes to the reader of this list")
    message_id: int | None = Field(description="the chat message that carries it; null for none")
    seq: int | None = Field(description="the chat event of that message; null outside a chat")
    thread_root_id: int | None = Field(description="the first message of that message's thread; null for none")
    run_id: int | None = Field(description="the run that wrote it, for an output; null for an attachment")
    author: ChatAuthor | None = Field(
        description="who sent it: the person who attached it, or the agent of the chat message that carries an "
        "output; null for an output in a run's list, which the run's agent wrote"
    )


class DocVersions(BaseModel):
    key: str = Field(description="the name the versions share: the last part after a slash, lower case")
    versions: list[DocVersion] = Field(description="oldest first; those the reader may not read left out")


def doc_key(name: str) -> str:
    """The key of the document ``name`` is a version of, as the web's ``docKey`` has it."""
    name = name.strip()
    return (name[name.rfind("/") + 1 :] or name).lower()


def _version(row, source: str, url: str, **found) -> DocVersion:
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
        **({"message_id": None, "seq": None, "thread_root_id": None, "run_id": None, "author": None} | found),
    )


def _file_columns(table) -> tuple:
    return (table.c.id, table.c.name, table.c.media_type, table.c.size, table.c.sha256)


# A chat


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


async def _chat_rows(conn: AsyncConnection, chat_id: int) -> tuple[list, list]:
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
    return (await conn.execute(attached)).all(), (await conn.execute(carried)).all()


def _chat_author(row, profiles) -> ChatAuthor:
    if row.author_profile_id is not None and row.author_profile_id in profiles:
        return agent_author(profiles[row.author_profile_id])
    if row.author_login is not None:
        return person_author(row.author_login, row.author_github_id)
    return HUB_AUTHOR


async def chat_versions(conn: AsyncConnection, user_id: int, chat_id: int, key: str) -> list[DocVersion]:
    """The versions of document ``key`` in chat ``chat_id`` that person ``user_id`` reads, oldest first."""
    attached, carried = await _chat_rows(conn, chat_id)
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
    return [version for _, version in sorted(found, key=lambda pair: pair[0])]


def _in_chat(row, profiles) -> dict:
    return {
        "message_id": row.message_id,
        "seq": row.seq,
        "thread_root_id": row.thread_root_id,
        "author": _chat_author(row, profiles),
    }


@chat_router.get("/{chat_id}/doc-versions", response_model=DocVersions, responses=REFUSALS)
async def chat_doc_versions(request: Request, chat_id: ChatId, user: CurrentUser, name: DocName) -> DocVersions:
    """Every version of the document ``name`` in the chat, threads included, oldest first: the files people attached
    and the outputs agents sent under that name, of the messages the caller reads."""
    key = doc_key(name)
    async with request.app.state.engine.begin() as conn:
        await chat_access(conn, user, chat_id)
        versions = await chat_versions(conn, user.user_id, chat_id, key)
    return DocVersions(key=key, versions=versions)


# A run


async def run_versions(conn: AsyncConnection, project: str, run_id: int, key: str) -> list[DocVersion]:
    """The versions of document ``key`` in run ``run_id`` of ``project``, oldest first: the files of the messages its
    inbox took and the outputs of the run."""
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
    found: list[tuple[tuple, DocVersion]] = []
    seen: set[int] = set()
    for row in sorted((await conn.execute(attached)).all(), key=lambda row: (row.sent_at, row.id)):
        if doc_key(row.name) != key or row.id in seen:  # a file two inbox rows took is one version
            continue
        seen.add(row.id)
        url = f"/v1/projects/{quote(project, safe='')}/runs/{run_id}/attachments/{row.id}/content"
        author = person_author(row.login, row.github_id)
        version = _version(row, ATTACHED, url, created_at=row.sent_at, message_id=row.chat_message_id, author=author)
        found.append(((row.sent_at, 0, row.id), version))
    for row in (await conn.execute(written)).all():
        if doc_key(row.name) == key:
            version = _version(
                row, row.source, run_url(project, run_id, row.id), created_at=row.created_at, run_id=run_id
            )
            found.append(((row.created_at, 1, row.id), version))
    return [version for _, version in sorted(found, key=lambda pair: pair[0])]


@router.get("/{project}/runs/{run_id}/doc-versions", response_model=DocVersions, responses=REFUSALS)
async def run_doc_versions(
    request: Request, project: ProjectName, run_id: RunId, user: CurrentUser, name: DocName, sink: Sink = None
) -> DocVersions:
    """Every version of the document ``name`` in the run, oldest first: the files of the messages sent to its agent
    and the outputs of the run under that name, for the run's readers."""
    key = doc_key(name)
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        versions = await run_versions(conn, project, run_id, key)
    return DocVersions(key=key, versions=versions)
