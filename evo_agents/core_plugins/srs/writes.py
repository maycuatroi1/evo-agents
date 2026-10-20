"""Keeping a version: matching the items of new content to those a document holds, giving codes to screens that have
none, the checks only the tables tell, and writing the version with its items, codes, texts and links.

- ``match_keys`` gives each item of new content the key of the item it continues: by code or alias first, a screen
  without a code by its title, an item without a code by its kind and its path of headings. The rest are new.
- ``give_screen_codes`` gives every screen without a code ``SCR-<n>``, n one more than the largest the project ever
  gave, dropped screens included.
- ``store_problems`` finds what refuses the save: a code another item of the project holds or held, a code an item
  had changed, a coded item dropped by a member who is not an admin of the project.
- ``unheld_problems`` says why no item of the latest version holds a code an admin asked to retire.
- ``write_version`` writes the next version of a document, under its row lock, as one straight line: the version
  built on the latest one. A dropped item keeps its codes, so no item gets them again.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import CODED_KINDS, checks
from evo_agents.core_plugins.srs.doc import ERROR, Document, Item, Problem, SrsError
from evo_agents.core_plugins.srs.store import Snapshot
from evo_agents.core_plugins.srs.template import SCREEN_PREFIX, Template, code_key, screen_number
from evo_agents.hub import tables

KEY_BYTES = 6  # an item's key: 12 hex characters, unique in its document


@dataclass(frozen=True)
class Writer:
    """Who writes a version, where: the project, the document, the author, whether they are an admin of the project,
    and the run of the agent that drafted it."""

    project_id: int
    document_id: int
    author_id: int
    admin: bool
    run_id: int | None = None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def new_key() -> str:
    return secrets.token_hex(KEY_BYTES)


# Matching new content to what the document holds


def _path(doc: Document, index: int) -> tuple:
    item = doc.items[index]
    return (item.kind, *(doc.items[at].title for at in reversed(doc.ancestors(index))), item.title)


def _by_code(base: Document) -> dict[str, Item]:
    return {code_key(code): item for item in base.items for code in item.codes()}


def match_keys(doc: Document, base: Document | None) -> None:
    """Give each item of ``doc`` the key of the item of ``base`` it continues, in place; a screen matched by its title
    takes that screen's code."""
    if base is None:
        return
    coded, used = _by_code(base), set()
    paths: dict[tuple, list[str]] = {}
    screens: dict[str, list[Item]] = {}
    for index, item in enumerate(base.items):
        if item.kind == "screen":
            screens.setdefault(item.title.casefold(), []).append(item)
        elif not item.code:
            paths.setdefault(_path(base, index), []).append(item.key)
    for index, item in enumerate(doc.items):
        held = next((coded[code_key(c)] for c in item.codes() if code_key(c) in coded), None)
        if held is not None and held.kind == item.kind and held.key not in used:
            item.key = held.key
        elif item.kind == "screen" and not item.code:
            found = next((s for s in screens.get(item.title.casefold(), []) if s.key not in used), None)
            if found is not None:
                item.key, item.code = found.key, found.code
        elif not item.code and item.kind not in CODED_KINDS:
            item.key = next((key for key in paths.get(_path(doc, index), []) if key not in used), None)
        if item.key:
            used.add(item.key)


async def give_screen_codes(conn: AsyncConnection, project_id: int, doc: Document) -> None:
    """Give each screen of ``doc`` without a code the next ``SCR-<n>`` of the project, in place."""
    if not any(item.kind == "screen" and not item.code for item in doc.items):
        return
    c = tables.srs.codes
    held = await conn.execute(
        select(c.c.code_key).where(c.c.project_id == project_id, c.c.code_key.like(f"{SCREEN_PREFIX.lower()}%"))
    )
    numbers = [screen_number(row.code_key) for row in held]
    numbers += [screen_number(code) for item in doc.items for code in item.codes()]
    last = max([number for number in numbers if number is not None], default=0)
    for item in doc.items:
        if item.kind == "screen" and not item.code:
            last += 1
            item.code = f"{SCREEN_PREFIX}{last}"


# The checks the tables tell


@dataclass(frozen=True)
class HeldCode:
    item_id: int
    document_id: int
    retired_in: int | None


async def held_codes(conn: AsyncConnection, project_id: int, keys: list[str]) -> dict[str, HeldCode]:
    """Code key -> the item that holds or held it, for the codes of ``keys`` the project gave."""
    c, i = tables.srs.codes, tables.srs.items
    query = (
        select(c.c.code_key, c.c.item_id, i.c.document_id, i.c.retired_in)
        .join_from(c, i, i.c.id == c.c.item_id)
        .where(c.c.project_id == project_id, c.c.code_key.in_(keys or [""]))
    )
    return {row.code_key: HeldCode(row.item_id, row.document_id, row.retired_in) for row in await conn.execute(query)}


def _taken(code: str, held: HeldCode, writer: Writer) -> str:
    if held.retired_in is not None:
        return f"{code!r} was the code of an item dropped in version {held.retired_in}; a code is never given again"
    if held.document_id != writer.document_id:
        return f"{code!r} is the code of an item of another document of the project; codes are unique in the project"
    return f"{code!r} is the code of another item of this document"


def _code_problems(doc: Document, base: Snapshot | None, held: dict[str, HeldCode], writer: Writer) -> list[Problem]:
    found = []
    ids = base.ids if base else {}
    stored = {item.key: item for item in base.doc.items} if base else {}
    for item in doc.items:
        own = ids.get(item.key) if item.key else None
        for code in item.codes():
            found_held = held.get(code_key(code))
            if found_held is not None and found_held.item_id != own:
                message = _taken(code, found_held, writer)
                found.append(Problem(ERROR, "code_taken", message, code=code, item=item.key, line=item.line))
        before = stored.get(item.key)
        if before is not None and before.code and code_key(before.code) != code_key(item.code or ""):
            message = f"the code of {before.code} cannot change in a version; an admin changes a code"
            found.append(Problem(ERROR, "code_changed", message, code=before.code, item=item.key, line=item.line))
    return found


def dropped(doc: Document, base: Snapshot | None) -> list[Item]:
    """The items of ``base`` that ``doc`` no longer has."""
    if base is None:
        return []
    kept = {item.key for item in doc.items if item.key}
    return [item for item in base.doc.items if item.key not in kept]


async def store_problems(conn: AsyncConnection, writer: Writer, doc: Document, base: Snapshot | None) -> list[Problem]:
    """What the tables tell refuses saving ``doc`` as the next version of ``base``."""
    keys = sorted({code_key(code) for item in doc.items for code in item.codes()})
    found = _code_problems(doc, base, await held_codes(conn, writer.project_id, keys), writer)
    if not writer.admin:
        for item in dropped(doc, base):
            if item.code:
                message = f"{item.code} would be dropped; only an admin of the project drops a coded item"
                found.append(Problem(ERROR, "coded_item_dropped", message, code=item.code, item=item.key))
    return found


def _unheld(code: str, held: HeldCode | None, writer: Writer, where: str) -> str:
    if held is not None and held.document_id != writer.document_id:
        return f"{code!r} is the code of an item of another document of the project, not of {where}"
    if held is not None and held.retired_in is not None:
        return f"{code!r} was the code of an item dropped in version {held.retired_in}: it is retired already"
    return f"no item of {where} holds the code {code!r}"


async def unheld_problems(conn: AsyncConnection, writer: Writer, where: str, codes: list[str]) -> list[Problem]:
    """One error per code of ``codes``, none of which an item of the latest version (``where``, such as "version 3
    of orders") holds, saying why: a code of another document, one dropped before, or one nobody holds."""
    held = await held_codes(conn, writer.project_id, sorted({code_key(code) for code in codes}))
    return [
        Problem(ERROR, "code_unknown", _unheld(code, held.get(code_key(code)), writer, where), code=code)
        for code in codes
    ]


# Writing


def content_sha256(doc: Document) -> str:
    """The SHA-256 of a version's content in canonical JSON: its preamble and each item as it stands."""
    keyed = [
        {
            "key": item.key,
            "kind": item.kind,
            "code": item.code,
            "parent": doc.items[item.parent].key if item.parent is not None else None,
            "level": item.level,
            "title": item.title,
            "fields": item.fields,
            "body": sha256(item.body),
            "refs": sorted(item.refs),
        }
        for item in doc.items
    ]
    canonical = json.dumps({"preamble": doc.preamble, "items": keyed}, sort_keys=True, ensure_ascii=False)
    return sha256(canonical)


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
                        "item_id": ids[item.key],
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
            "item_id": ids[item.key],
            "parent_item_id": ids[doc.items[item.parent].key] if item.parent is not None else None,
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
        {"version_id": version_id, "from_item": ids[item.key], "rel": rel, "to_item": ids[target]}
        for item in doc.items
        for rel, target in dict.fromkeys(item.refs)
        if target in ids
    ]
    if refs:
        await conn.execute(insert(s.refs).values(refs))


async def write_version(
    conn: AsyncConnection, writer: Writer, doc: Document, base: Snapshot | None, template: Template, summary: str = ""
) -> int:
    """Write ``doc`` as the version after ``base`` (the document's latest; None for its first), under the document's
    row lock the caller holds; the new version's number. SrsError 422 with the problems that refuse it (codes that do
    not match ``template``, codes twice, and what the tables tell), 409 when it changes nothing."""
    found = checks.code_problems(doc, template) + await store_problems(conn, writer, doc, base)
    refused = [problem for problem in found if problem.refuses_save()]
    if refused:
        raise SrsError(422, f"the version is not saved: {refused[0].message}", refused)
    fresh = [item for item in doc.items if not item.key]
    for item in fresh:  # keys first: the content's SHA-256 names every item by its key
        item.key = new_key()
    content = content_sha256(doc)
    if base is not None and content == base.version.content_sha256:
        raise SrsError(409, f"nothing changed since version {base.version.number}: no version is saved")
    number = base.version.number + 1 if base else 1
    await _texts(conn, [doc.preamble, *(item.body for item in doc.items)])
    v = tables.srs.versions
    values = {
        "document_id": writer.document_id,
        "number": number,
        "parent_number": base.version.number if base else None,
        "author_id": writer.author_id,
        "drafted_by_run": writer.run_id,
        "summary": summary,
        "preamble_sha256": sha256(doc.preamble) if doc.preamble else None,
        "content_sha256": content,
    }
    version_id = (await conn.execute(insert(v).values(values).returning(v.c.id))).scalar_one()
    ids = {**(base.ids if base else {}), **await _new_items(conn, writer, fresh, number)}
    await _codes(conn, writer, doc, ids)
    gone = [base.ids[item.key] for item in dropped(doc, base)] if base else []
    if gone:
        i = tables.srs.items
        await conn.execute(update(i).where(i.c.id.in_(gone)).values(retired_in=number))
    await _item_rows(conn, version_id, doc, ids)
    return number


__all__ = [
    "Writer",
    "content_sha256",
    "dropped",
    "give_screen_codes",
    "held_codes",
    "match_keys",
    "new_key",
    "sha256",
    "store_problems",
    "unheld_problems",
    "write_version",
]
