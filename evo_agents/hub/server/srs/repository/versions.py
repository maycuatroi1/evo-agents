"""Writing a version of a document: its row, the texts of its items, its new items and codes, the rows of each item
as the version has it and their links, and the revisions of its use cases (``revisions.record``).

``VersionStore`` is the write, a ``VersionedStore`` (``evo_agents.hub.server.base.versions``) on the document's
latest version, which the caller read under the document's row lock (``store.document`` with ``lock``). srs merges a
save made on an older version into the latest (``service.edits.edited``, which answers 409 with both texts of an
item that changed since), so ``check`` refuses no base; a save whose content is the latest's (``unchanged``) writes
nothing. ``service.writes.write_version`` checks the content and answers what the store did.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import ClassVar

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.base.versions import VersionedStore
from evo_agents.hub.server.srs.models.doc import Document, Item
from evo_agents.hub.server.srs.models.template import code_key
from evo_agents.hub.server.srs.repository import revisions
from evo_agents.hub.server.srs.repository.store import Snapshot


@dataclass(frozen=True)
class Writer:
    """Who writes a version, where: the project, the document, the author, whether they are an admin of the project,
    and the run of the agent that drafted it."""

    project_id: int
    document_id: int
    author_id: int
    admin: bool
    run_id: int | None = None


@dataclass(frozen=True)
class HeldCode:
    item_id: int
    document_id: int
    retired_in: int | None


@dataclass(frozen=True)
class NewVersion:
    """The content of the next version, checked, every item keyed: its SHA-256 (``service.writes.content_sha256``),
    the items new in it and the ids of those of the latest it drops."""

    writer: Writer
    doc: Document
    content_sha256: str
    summary: str = ""
    fresh: list[Item] = field(default_factory=list)
    gone: list[int] = field(default_factory=list)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def held_codes(conn: AsyncConnection, project_id: int, keys: list[str]) -> dict[str, HeldCode]:
    """Code key -> the item that holds or held it, for the codes of ``keys`` the project gave."""
    c, i = tables.srs.codes, tables.srs.items
    query = (
        select(c.c.code_key, c.c.item_id, i.c.document_id, i.c.retired_in)
        .join_from(c, i, i.c.id == c.c.item_id)
        .where(c.c.project_id == project_id, c.c.code_key.in_(keys or [""]))
    )
    return {row.code_key: HeldCode(row.item_id, row.document_id, row.retired_in) for row in await conn.execute(query)}


async def code_keys_like(conn: AsyncConnection, project_id: int, prefix: str) -> list[str]:
    """The code keys the project ever gave that start with ``prefix``, dropped items' included."""
    c = tables.srs.codes
    query = select(c.c.code_key).where(c.c.project_id == project_id, c.c.code_key.like(f"{prefix}%"))
    return list((await conn.execute(query)).scalars())


async def _texts(conn: AsyncConnection, bodies: list[str]) -> None:
    rows = {sha256(body): body for body in bodies}
    t = tables.srs.texts
    values = [{"sha256": key, "body": body} for key, body in rows.items()]
    await conn.execute(pg_insert(t).values(values).on_conflict_do_nothing(index_elements=[t.c.sha256]))


async def _new_items(conn: AsyncConnection, writer: Writer, fresh: list[Item], number: int) -> dict[str, int]:
    """Insert the items of ``fresh``, new in version ``number``; key -> id of each."""
    if not fresh:
        return {}
    i = tables.srs.items
    values = [
        {
            "project_id": writer.project_id,
            "document_id": writer.document_id,
            "key": item.key,
            "kind": item.kind,
            "code": item.code,
            "created_in": number,
        }
        for item in fresh
    ]
    rows = await conn.execute(insert(i).values(values).returning(i.c.id, i.c.key))
    return {row.key: row.id for row in rows}


def _key(item: Item) -> str:
    return item.key or ""


async def _codes(conn: AsyncConnection, writer: Writer, doc: Document, ids: dict[str, int]) -> None:
    keys = sorted({code_key(code) for item in doc.items for code in item.codes()})
    held = await held_codes(conn, writer.project_id, keys)
    values, seen = [], set(held)
    for item in doc.items:
        for code in item.codes():
            if code_key(code) not in seen:
                seen.add(code_key(code))
                values.append(
                    {
                        "project_id": writer.project_id,
                        "code_key": code_key(code),
                        "item_id": ids[_key(item)],
                        "code": code,
                        "is_primary": code == item.code,
                    }
                )
    if values:
        await conn.execute(insert(tables.srs.codes).values(values))


async def _item_rows(conn: AsyncConnection, version_id: int, doc: Document, ids: dict[str, int]) -> None:
    s = tables.srs
    rows = [
        {
            "version_id": version_id,
            "item_id": ids[_key(item)],
            "parent_item_id": ids[_key(doc.items[item.parent])] if item.parent is not None else None,
            "position": position,
            "heading_level": item.level,
            "title": item.title,
            "fields": item.fields,
            "body_sha256": sha256(item.body),
        }
        for position, item in enumerate(doc.items)
    ]
    if rows:
        await conn.execute(insert(s.item_versions).values(rows))
    refs = [
        {"version_id": version_id, "from_item": ids[_key(item)], "rel": rel, "to_item": ids[target]}
        for item in doc.items
        for rel, target in dict.fromkeys(item.refs)
        if target in ids
    ]
    if refs:
        await conn.execute(insert(s.refs).values(refs))


class VersionStore(VersionedStore[int, Snapshot]):
    """The versions of one document, by its id, on ``latest``: its latest version, which the caller read under the
    document's row lock (None for a document with no version yet)."""

    noun: ClassVar[str] = "document"

    def __init__(self, latest: Snapshot | None):
        self.latest = latest

    async def held(self, conn: AsyncConnection, key: int) -> Snapshot | None:
        return self.latest

    def version_of(self, row: Snapshot) -> int:
        return row.version.number

    async def check(self, conn: AsyncConnection, key: int, row: Snapshot | None, base: int | None) -> None:
        """Refuse no base: a save made on an older version is merged into the latest before (``edits.edited``)."""

    def unchanged(self, row: Snapshot, values: NewVersion) -> bool:
        return values.content_sha256 == row.version.content_sha256

    async def insert(
        self, conn: AsyncConnection, key: int, row: Snapshot | None, version: int, values: NewVersion
    ) -> None:
        """Write ``values`` as version ``version`` of document ``key``, after ``row`` (None for its first)."""
        writer, doc = values.writer, values.doc
        await _texts(conn, [doc.preamble, *(item.body for item in doc.items)])
        v = tables.srs.versions
        row_values = {
            "document_id": key,
            "number": version,
            "parent_number": row.version.number if row else None,
            "author_id": writer.author_id,
            "drafted_by_run": writer.run_id,
            "summary": values.summary,
            "preamble_sha256": sha256(doc.preamble) if doc.preamble else None,
            "content_sha256": values.content_sha256,
        }
        version_id = (await conn.execute(insert(v).values(row_values).returning(v.c.id))).scalar_one()
        ids = {**(row.ids if row else {}), **await _new_items(conn, writer, values.fresh, version)}
        await _codes(conn, writer, doc, ids)
        if values.gone:
            i = tables.srs.items
            await conn.execute(update(i).where(i.c.id.in_(values.gone)).values(retired_in=version))
        await _item_rows(conn, version_id, doc, ids)
        await revisions.record(conn, key, version_id, writer.author_id, doc, ids)


__all__ = ["HeldCode", "NewVersion", "VersionStore", "Writer", "code_keys_like", "held_codes", "sha256"]
