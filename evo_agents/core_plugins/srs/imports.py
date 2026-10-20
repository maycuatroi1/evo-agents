"""Importing Markdown: a file whose headings follow the template becomes a new document, or the next version of one
(``document``), after a preview of what it holds. An archived document takes no import, a dry run included.

The preview lists every item with its kind, code and title, how many of each kind, what is wrong (``checks`` and what
the tables tell), and for a document that exists the items the import adds, changes and drops. A dry run stops there;
so does an import with a problem that refuses saving (422 with the problems). A new document takes the slug given,
else one made of its title, and the label given (an admin's choice) or the project's.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import checks, markdown, store
from evo_agents.core_plugins.srs.diff import text_of
from evo_agents.core_plugins.srs.doc import Document, Problem, SrsError
from evo_agents.core_plugins.srs.writes import (
    Writer,
    dropped,
    give_screen_codes,
    match_keys,
    store_problems,
    write_version,
)
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.projects import ProjectAccess

MAX_MARKDOWN_BYTES = 4 * 1024 * 1024
SLUG_CHARS = 100


@dataclass(frozen=True)
class ImportRequest:
    """What to import: the Markdown, the document it continues (or the slug and title of a new one), its kind, the
    summary of the version, the label of a new document, and whether to stop at the preview."""

    markdown: str
    document: str | None = None
    slug: str | None = None
    title: str | None = None
    kind: str = "srs"
    summary: str = ""
    label: dict | None = None
    dry_run: bool = False


@dataclass
class Preview:
    """What an import holds and would do."""

    items: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


@dataclass
class Imported:
    """The outcome of an import: the document's slug, whether it was created, the version saved, and the preview."""

    document: str
    created: bool
    version: int | None
    preview: Preview


def slug_of(title: str) -> str:
    """A slug made of ``title``: lowercase letters and digits, runs of anything else as one hyphen."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:SLUG_CHARS].strip("-")
    return slug or "document"


def _name(item) -> str:
    return item.code or item.title


def preview_of(doc: Document, base: store.Snapshot | None, problems: list[Problem]) -> Preview:
    items = [
        {
            "kind": item.kind,
            "code": item.code,
            "title": item.title,
            "level": item.level,
            "parent": item.parent,
            "line": item.line,
        }
        for item in doc.items
    ]
    found = Preview(items, dict(Counter(item.kind for item in doc.items)), problems)
    if base is not None:
        held = {item.key: item for item in base.doc.items}
        for item in doc.items:
            if not item.key:
                found.added.append(_name(item))
            elif text_of(held[item.key]) != text_of(item) or held[item.key].fields != item.fields:
                found.changed.append(_name(item))
        found.removed = [_name(item) for item in dropped(doc, base)]
    return found


async def _slug_free(conn: AsyncConnection, access: ProjectAccess, slug: str) -> None:
    d = tables.srs.documents
    taken = await conn.execute(select(d.c.id).where(d.c.project_id == access.project_id, d.c.slug == slug))
    if taken.first() is not None:
        raise SrsError(
            409, f"project {access.name} has a document {slug}: import with --doc {slug} for its next version"
        )


async def _create(conn: AsyncConnection, access: ProjectAccess, user_id: int, request: ImportRequest, doc) -> int:
    title = (request.title or markdown.title_of(doc) or request.slug or "Untitled").strip()[:300]
    d = tables.srs.documents
    values = {
        "project_id": access.project_id,
        "slug": request.slug,
        "title": title,
        "kind": request.kind,
        "label": access.push_label(request.label),
        "created_by": user_id,
    }
    return (await conn.execute(insert(d).values(values).returning(d.c.id))).scalar_one()


def _writer_check(access: ProjectAccess, request: ImportRequest) -> None:
    if not has_role(access.role, "writer"):
        raise SrsError(403, f"importing a document into project {access.name} needs the writer role on it")
    if request.label is not None and not has_role(access.role, "admin"):
        raise SrsError(403, f"only an admin of project {access.name} sets the label of a document")
    if len(request.markdown.encode()) > MAX_MARKDOWN_BYTES:
        raise SrsError(413, f"the Markdown is over {MAX_MARKDOWN_BYTES // (1024 * 1024)} MiB")


async def import_markdown(
    conn: AsyncConnection, access: ProjectAccess, user_id: int, request: ImportRequest, sink: str | None
) -> Imported:
    """Import ``request`` into the project as ``user_id``; the preview, and the version saved unless a dry run."""
    _writer_check(access, request)
    settings = await store.settings(conn, access)
    doc = markdown.parse(request.markdown, settings.template)
    base, held = None, None
    if request.document:
        held = await store.document(conn, access, request.document, sink, lock=not request.dry_run)
        store.writable(access, held)  # a dry run too: its preview promises no import an archived document refuses
        base = await store.snapshot(conn, await store.version(conn, held)) if held.latest_version else None
    else:
        request = replace(request, slug=request.slug or slug_of(request.title or markdown.title_of(doc) or ""))
        await _slug_free(conn, access, request.slug)
    match_keys(doc, base.doc if base else None)
    await give_screen_codes(conn, access.project_id, doc)
    writer = Writer(access.project_id, held.id if held else 0, user_id, has_role(access.role, "admin"))
    problems = checks.problems(doc, settings.template) + await store_problems(conn, writer, doc, base)
    preview = preview_of(doc, base, problems)
    slug = held.slug if held else request.slug
    refused = [problem for problem in problems if problem.refuses_save()]
    if request.dry_run:
        return Imported(slug, False, None, preview)
    if refused:
        raise SrsError(422, f"the import is not saved: {refused[0].message}", refused)
    document_id = held.id if held else await _create(conn, access, user_id, request, doc)
    writer = Writer(access.project_id, document_id, user_id, writer.admin)
    number = await write_version(conn, writer, doc, base, settings.template, request.summary)
    return Imported(slug, held is None, number, preview)


__all__ = ["MAX_MARKDOWN_BYTES", "ImportRequest", "Imported", "Preview", "import_markdown", "preview_of", "slug_of"]
