"""Reading the documents of a project: the documents a member sees, a document's versions with their reviews, a version
loaded whole as a ``doc.Document`` (``Snapshot``), and the plugin's effective config in the project (``settings``).

A document carries a label (the project's default, or one an admin set when importing it), and every version, item,
review and comment of it is read under that label: through the sink X-Evo-Sink names, else the project's hub sink,
as plans are read (``ProjectAccess.visible``). A document the member cannot see answers the 404 of one that does not
exist.

An archived document (``archived_at``) is out of the list of documents unless asked for, and out of the knowledge
graph; it is still read by its slug, and takes no write until an admin unarchives it (``writable``): its codes stay
held in the project and its slug taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs.doc import Document, Item, SrsError
from evo_agents.core_plugins.srs.template import Template, TemplateError, code_key, template_of
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.plugins import config as plugin_config

PLUGIN = "srs"
SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER


@dataclass(frozen=True)
class DocumentRow:
    """A document with its latest version's number and state."""

    id: int
    slug: str
    title: str
    kind: str
    template: str
    approved_version: int | None
    label: dict
    created_by: str
    created_at: datetime
    archived_at: datetime | None
    latest_version: int | None
    latest_state: str | None


@dataclass(frozen=True)
class ReviewRow:
    reviewer: str
    reviewer_id: int
    role: str | None
    decision: str
    body: str
    round: int
    created_at: datetime


@dataclass(frozen=True)
class VersionRow:
    """A version of a document, with the reviews of every round it went through."""

    id: int
    number: int
    parent_number: int | None
    state: str
    round: int
    author: str
    author_id: int
    drafted_by_run: int | None
    summary: str
    content_sha256: str
    preamble_sha256: str | None
    created_at: datetime
    submitted_at: datetime | None
    decided_at: datetime | None
    reviews: list[ReviewRow] = field(default_factory=list)


@dataclass
class Snapshot:
    """A version loaded whole: its row, its content, and the id of each item by key."""

    version: VersionRow
    doc: Document
    ids: dict[str, int]


@dataclass(frozen=True)
class Settings:
    """The plugin's effective config in a project: the template, the members of each role, the approval policy, and
    whether the author of a version may approve it (``self_approval``, off unless an admin turns it on)."""

    template: Template
    roles: dict = field(default_factory=dict)
    policy: dict = field(default_factory=dict)
    self_approval: bool = False


def sink_of(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


def reader(access: ProjectAccess) -> None:
    """Reading needs a grant on the project (a hub admin without one gets this far only)."""
    if access.role is None:
        raise SrsError(403, f"reading the documents of project {access.name} needs a grant on it")


def missing(project: str, slug: str) -> SrsError:
    return SrsError(404, f"no document {slug} in project {project} that you can see: see `evo-agents hub srs list`")


def _documents_query(project_id: int):
    d, v, u = tables.srs.documents, tables.srs.versions, tables.users
    latest = v.alias("latest")
    newest = select(func.max(v.c.number)).where(v.c.document_id == d.c.id).scalar_subquery()
    return (
        select(
            d.c.id,
            d.c.slug,
            d.c.title,
            d.c.kind,
            d.c.template,
            d.c.approved_version,
            d.c.label,
            u.c.login.label("created_by"),
            d.c.created_at,
            d.c.archived_at,
            latest.c.number.label("latest_version"),
            latest.c.state.label("latest_state"),
        )
        .select_from(
            d.join(u, u.c.id == d.c.created_by).outerjoin(
                latest, and_(latest.c.document_id == d.c.id, latest.c.number == newest)
            )
        )
        .where(d.c.project_id == project_id)
    )


async def approved_documents(conn: AsyncConnection, project_id: int) -> list[DocumentRow]:
    """Every document of the project that has an approved version and is not archived, by slug, whoever reads: the
    knowledge graph reads them all and labels each by its row (``kg``)."""
    d = tables.srs.documents
    query = _documents_query(project_id).where(d.c.approved_version.is_not(None), d.c.archived_at.is_(None))
    return [DocumentRow(**row._mapping) for row in await conn.execute(query.order_by(d.c.slug))]


async def documents(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, *, with_archived: bool = False
) -> list[DocumentRow]:
    """The documents of the project the member sees, by slug; the archived ones too with ``with_archived``."""
    reader(access)
    through = sink_of(access, sink)
    d = tables.srs.documents
    query = _documents_query(access.project_id).order_by(d.c.slug)
    if not with_archived:
        query = query.where(d.c.archived_at.is_(None))
    rows = [DocumentRow(**row._mapping) for row in await conn.execute(query)]
    return [row for row in rows if through is not None and access.visible(row.label, through)]


def writable(access: ProjectAccess, doc: DocumentRow) -> DocumentRow:
    """``doc`` when it takes writes; 409 when it is archived."""
    if doc.archived_at is not None:
        raise SrsError(
            409,
            f"document {doc.slug} of project {access.name} is archived and takes no change: an admin of the project "
            f"opens it again with `evo-agents hub srs unarchive {doc.slug} --project {access.name}`",
        )
    return doc


async def document(
    conn: AsyncConnection,
    access: ProjectAccess,
    slug: str,
    sink: str | None,
    *,
    lock: bool = False,
    even_archived: bool = False,
) -> DocumentRow:
    """Document ``slug`` as the member sees it; 404 when there is none or they cannot see it. ``lock`` takes its row
    lock first, for a write, and refuses an archived document (``writable``) unless ``even_archived``, which archiving
    and unarchiving pass."""
    reader(access)
    d = tables.srs.documents
    if lock:
        held = select(d.c.id).where(d.c.project_id == access.project_id, d.c.slug == slug).with_for_update()
        await conn.execute(held)
    row = (await conn.execute(_documents_query(access.project_id).where(d.c.slug == slug))).first()
    through = sink_of(access, sink)
    if row is None or through is None or not access.visible(row.label, through):
        raise missing(access.name, slug)
    found = DocumentRow(**row._mapping)
    return writable(access, found) if lock and not even_archived else found


def _versions_query(document_id: int):
    v, u = tables.srs.versions, tables.users
    return (
        select(
            v.c.id,
            v.c.number,
            v.c.parent_number,
            v.c.state,
            v.c.round,
            u.c.login.label("author"),
            v.c.author_id,
            v.c.drafted_by_run,
            v.c.summary,
            v.c.content_sha256,
            v.c.preamble_sha256,
            v.c.created_at,
            v.c.submitted_at,
            v.c.decided_at,
        )
        .join_from(v, u, u.c.id == v.c.author_id)
        .where(v.c.document_id == document_id)
    )


async def _reviews(conn: AsyncConnection, version_ids: list[int]) -> dict[int, list[ReviewRow]]:
    r, u = tables.srs.reviews, tables.users
    query = (
        select(
            r.c.version_id,
            u.c.login.label("reviewer"),
            r.c.reviewer_id,
            r.c.role,
            r.c.decision,
            r.c.body,
            r.c.round,
            r.c.created_at,
        )
        .join_from(r, u, u.c.id == r.c.reviewer_id)
        .where(r.c.version_id.in_(version_ids or [0]))
        .order_by(r.c.id)
    )
    found: dict[int, list[ReviewRow]] = {}
    for row in await conn.execute(query):
        values = dict(row._mapping)
        found.setdefault(values.pop("version_id"), []).append(ReviewRow(**values))
    return found


async def versions(conn: AsyncConnection, document_id: int) -> list[VersionRow]:
    """Every version of a document, the newest first, with its reviews."""
    rows = list(await conn.execute(_versions_query(document_id).order_by(tables.srs.versions.c.number.desc())))
    reviews = await _reviews(conn, [row.id for row in rows])
    return [VersionRow(**row._mapping, reviews=reviews.get(row.id, [])) for row in rows]


async def version(conn: AsyncConnection, doc: DocumentRow, number: int | None = None, *, lock: bool = False):
    """Version ``number`` of ``doc`` (its latest when None) with its reviews; 404 when it has none such."""
    v = tables.srs.versions
    wanted = number if number is not None else doc.latest_version
    if wanted is None:
        raise SrsError(404, f"document {doc.slug} has no version yet")
    query = _versions_query(doc.id).where(v.c.number == wanted)
    if lock:
        query = query.with_for_update(of=v)
    row = (await conn.execute(query)).first()
    if row is None:
        raise SrsError(404, f"document {doc.slug} has no version {wanted}")
    reviews = await _reviews(conn, [row.id])
    return VersionRow(**row._mapping, reviews=reviews.get(row.id, []))


async def text(conn: AsyncConnection, sha256: str | None) -> str:
    if sha256 is None:
        return ""
    t = tables.srs.texts
    return (await conn.execute(select(t.c.body).where(t.c.sha256 == sha256))).scalar_one()


def _items_query(version_id: int):
    s = tables.srs
    iv, i, t = s.item_versions, s.items, s.texts
    return (
        select(
            i.c.id,
            i.c.key,
            i.c.kind,
            i.c.code,
            iv.c.parent_item_id,
            iv.c.heading_level,
            iv.c.title,
            iv.c.fields,
            t.c.body,
        )
        .join_from(iv, i, i.c.id == iv.c.item_id)
        .join(t, t.c.sha256 == iv.c.body_sha256)
        .where(iv.c.version_id == version_id)
        .order_by(iv.c.position)
    )


async def _aliases(conn: AsyncConnection, item_ids: list[int]) -> dict[int, list[str]]:
    c = tables.srs.codes
    query = (
        select(c.c.item_id, c.c.code)
        .where(c.c.item_id.in_(item_ids or [0]), c.c.is_primary.is_(False))
        .order_by(c.c.item_id, c.c.created_at, c.c.code_key)
    )
    found: dict[int, list[str]] = {}
    for row in await conn.execute(query):
        found.setdefault(row.item_id, []).append(row.code)
    return found


def _in_order(aliases: list[str], named: str | None) -> list[str]:
    """``aliases`` in the order the item's ID row names them, those it does not name after them."""
    text = code_key(named or "")

    def place(alias: str) -> tuple[int, str]:
        at = text.find(code_key(alias))
        return (at if at >= 0 else len(text) + 1, code_key(alias))

    return sorted(aliases, key=place)


async def _refs(conn: AsyncConnection, version_id: int, keys: dict[int, str]) -> dict[int, list[tuple[str, str]]]:
    r = tables.srs.refs
    query = select(r.c.from_item, r.c.rel, r.c.to_item).where(r.c.version_id == version_id)
    found: dict[int, list[tuple[str, str]]] = {}
    for row in await conn.execute(query.order_by(r.c.from_item, r.c.rel, r.c.to_item)):
        if row.to_item in keys:
            found.setdefault(row.from_item, []).append((row.rel, keys[row.to_item]))
    return found


async def snapshot(conn: AsyncConnection, row: VersionRow) -> Snapshot:
    """Version ``row`` loaded whole."""
    rows = list(await conn.execute(_items_query(row.id)))
    index = {item.id: at for at, item in enumerate(rows)}
    keys = {item.id: item.key for item in rows}
    aliases = await _aliases(conn, list(index))
    refs = await _refs(conn, row.id, keys)
    items = [
        Item(
            kind=item.kind,
            title=item.title,
            body=item.body,
            code=item.code,
            aliases=_in_order(aliases.get(item.id, []), (item.fields or {}).get("id")),
            level=item.heading_level,
            fields=dict(item.fields or {}),
            parent=index.get(item.parent_item_id),
            key=item.key,
            refs=refs.get(item.id, []),
        )
        for item in rows
    ]
    doc = Document(preamble=await text(conn, row.preamble_sha256), items=items)
    return Snapshot(row, doc, {item.key: item.id for item in rows})


async def settings(conn: AsyncConnection, access: ProjectAccess) -> Settings:
    """The plugin's effective config in the project; 409 when its code patterns do not compile."""
    return await project_settings(conn, access.project_id, access.name)


async def project_settings(conn: AsyncConnection, project_id: int, project: str) -> Settings:
    """``settings`` of project ``project_id`` named ``project``, for a caller that is no member: the kg source."""
    row = await enabled_in(conn, PLUGIN, project_id)
    if row is None:
        raise SrsError(404, f"plugin {PLUGIN} is not enabled in project {project}: an admin of the project enables it")
    stacks = await stored_stacks(conn, [row.id], project_id, None)
    merged = plugin_config.merge(row.manifest.get("config"), stacks.get(row.id, []))
    values = {name: item.value for name, item in merged.fields.items() if not item.secret}
    try:
        template = template_of(values.get("template"), values.get("code_patterns"))
    except TemplateError as exc:
        raise SrsError(409, f"the config of plugin {PLUGIN} in project {project}: {exc}") from None
    roles, policy = dict(values.get("roles") or {}), dict(values.get("approval_policy") or {})
    return Settings(template, roles, policy, self_approval=values.get("self_approval") is True)


__all__ = [
    "PLUGIN",
    "SINK_HEADER",
    "DocumentRow",
    "ReviewRow",
    "Settings",
    "Snapshot",
    "VersionRow",
    "approved_documents",
    "document",
    "documents",
    "missing",
    "project_settings",
    "reader",
    "settings",
    "sink_of",
    "snapshot",
    "text",
    "version",
    "versions",
    "writable",
]
