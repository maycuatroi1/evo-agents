"""The tool of the feature docs on the hub's /mcp: ``docs__read``, which reads the documents of the project.

The hub lists it in a session whose project has the feature on, and checks its arguments against the manifest's
schema first. It reads for a person: the agent of a chat run for the person who mentioned the agent (the origin of the
chain, ``chat_runs.ChatRun.dispatcher_id``), the agent of any other run for the member who dispatched it, anyone else
for themselves. A document is read when that person sees it through the session's sink and, when the run's worker is
another member's, when that member does too: what the tool answers reaches the machine the agent runs on. A refusal is
the tool's error text.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.chat_runs import chat_run
from evo_agents.hub.server.docs import store
from evo_agents.hub.server.docs.store import DocsError, DocumentRow
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import MACHINE, Principal


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


@dataclass(frozen=True)
class Reader:
    """The person the tool reads for, their access, the session's sink, and the access of the member whose worker
    runs the agent when another."""

    access: ProjectAccess
    login: str
    sink: str | None
    worker: ProjectAccess | None = None

    def sees(self, doc: DocumentRow) -> bool:
        if self.worker is None:
            return True
        through = store.sink_of(self.worker, self.sink)
        return through is not None and self.worker.visible(doc.label, through)


async def _principal(conn: AsyncConnection, user_id: int, config) -> Principal:
    login = (await conn.execute(select(tables.users.c.login).where(tables.users.c.id == user_id))).scalar_one()
    return Principal(user_id, login, config is not None and config.is_admin(login), None, MACHINE, "")


async def _access(conn: AsyncConnection, person: Principal, project: str) -> ProjectAccess:
    try:
        return await project_access(conn, person, project)
    except HTTPException:
        raise DocsError(403, f"{person.login} holds no grant on project {project} now") from None


async def reader(conn: AsyncConnection, params: dict, context: CoreContext) -> Reader:
    """Who the tool of ``params`` reads for (see the module)."""
    if context.run_id is None:
        login = (params.get("caller") or {}).get("login") or ""
        user_id = (
            await conn.execute(select(tables.users.c.id).where(tables.users.c.login == login))
        ).scalar_one_or_none()
        if user_id is None:
            raise DocsError(403, f"no member {login} on the hub")
        person = await _principal(conn, user_id, context.config)
        return Reader(await _access(conn, person, context.project), person.login, context.sink)
    r = tables.runs
    row = (await conn.execute(select(r.c.kind, r.c.dispatched_by).where(r.c.id == context.run_id))).one()
    person_id = row.dispatched_by
    found = await chat_run(conn, context.run_id) if row.kind == "chat" else None
    if found is not None:
        person_id = found.dispatcher_id
    person = await _principal(conn, person_id, context.config)
    access = await _access(conn, person, context.project)
    worker = None
    if person_id != row.dispatched_by:
        worker = await _access(conn, await _principal(conn, row.dispatched_by, context.config), context.project)
    return Reader(access, person.login, context.sink, worker)


async def _documents(conn: AsyncConnection, who: Reader) -> str:
    rows = [doc for doc in await store.documents(conn, who.access, who.sink) if who.sees(doc)]
    if not rows:
        return f"Project {who.access.name} has no document that {who.login} sees."
    lines = [f"The documents of project {who.access.name}:"]
    for doc in rows:
        lines.append(f"- {doc.slug}: {doc.title}; version {doc.latest_version} by {doc.updated_by}")
    return "\n".join(lines)


async def _document(conn: AsyncConnection, who: Reader, slug: str, number: int | None) -> str:
    doc = await store.document(conn, who.access, slug, who.sink)
    if not who.sees(doc):
        raise store.missing(who.access.name, slug)
    row = await store.version(conn, doc, number)
    when = row.created_at.strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"{doc.slug}: {row.title}", f"Version {row.number} of {doc.latest_version}, by {row.author}, {when}."]
    if row.images:
        lines.append("Images it shows: " + ", ".join(f"{image.name} ({image.media_type})" for image in row.images))
    return "\n".join([*lines, "", row.body or ""])


async def read(params: dict, context: CoreContext) -> dict:
    """``docs__read``: the documents of the project, or one version of one as Markdown."""
    arguments = params.get("arguments") or {}
    try:
        async with context.engine.begin() as conn:
            who = await reader(conn, params, context)
            if not arguments.get("document"):
                return _text(await _documents(conn, who))
            return _text(await _document(conn, who, arguments["document"], arguments.get("version")))
    except DocsError as exc:
        return _text(f"error: {exc.message}", error=True)
