"""Reading and writing the documents of a project (``evo_agents.hub.server.docs``).

A document carries a label (the project's default, or one an admin gave it when creating it), and every version and
image of it is read under that label: through the sink X-Evo-Sink names, else the project's hub sink, as plans are
read (``ProjectAccess.visible``). A document the member cannot see answers the 404 of one that does not exist.

``save`` writes the next version of a document, creating it at version 1 when the project has none of that slug. It
takes the document's row lock first, so two saves of one document are one after the other. A save whose title, body
and images are those of the latest version writes nothing; with ``base_version`` it is refused (409) unless that is
the latest version, so an editor does not save over a version it never read, and with 0 unless the document does not
exist yet, so a new one never lands on another of the same slug. Every image a version names must be an
image of the project (``docs_images``), uploaded before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.projects import ProjectAccess

SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER


class DocsError(Exception):
    """Why the hub refuses a request about a document, with the HTTP status a route answers; nothing was written."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class DocumentRow:
    """A document with its latest version's number, author and time."""

    id: int
    slug: str
    title: str
    label: dict
    latest_version: int
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


@dataclass(frozen=True)
class ImageRef:
    """An image a version shows: the name its Markdown gives it, and the image."""

    name: str
    sha256: str
    media_type: str
    size: int


@dataclass(frozen=True)
class VersionRow:
    """A version of a document; ``body`` and ``images`` only when it was read whole (``version``)."""

    id: int
    number: int
    title: str
    summary: str
    author: str
    created_at: datetime
    body: str | None = None
    images: list[ImageRef] = field(default_factory=list)


@dataclass(frozen=True)
class Saved:
    document: DocumentRow
    version: int
    created: bool
    changed: bool


def sink_of(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


def reader(access: ProjectAccess) -> None:
    """Reading needs a grant on the project (a hub admin without one gets this far only)."""
    if access.role is None:
        raise DocsError(403, f"reading the documents of project {access.name} needs a grant on it")


def writer(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "writer"):
        raise DocsError(403, f"{doing} in project {access.name} needs the writer role on it")


def missing(project: str, slug: str) -> DocsError:
    return DocsError(404, f"no document {slug} in project {project} that you can see: see `evo-agents hub docs list`")


def _documents_query(project_id: int):
    d, v, u = tables.docs.documents, tables.docs.versions, tables.users
    creator, author = u.alias("creator"), u.alias("author")
    return (
        select(
            d.c.id,
            d.c.slug,
            d.c.title,
            d.c.label,
            d.c.latest_version,
            creator.c.login.label("created_by"),
            d.c.created_at,
            author.c.login.label("updated_by"),
            d.c.updated_at,
        )
        .select_from(
            d.join(creator, creator.c.id == d.c.created_by)
            .join(v, (v.c.document_id == d.c.id) & (v.c.number == d.c.latest_version))
            .join(author, author.c.id == v.c.author_id)
        )
        .where(d.c.project_id == project_id)
    )


def _visible(access: ProjectAccess, sink: str | None, label) -> bool:
    through = sink_of(access, sink)
    return through is not None and access.visible(label, through)


async def documents(conn: AsyncConnection, access: ProjectAccess, sink: str | None) -> list[DocumentRow]:
    """The documents of the project the member sees, by title."""
    reader(access)
    d = tables.docs.documents
    rows = await conn.execute(_documents_query(access.project_id).order_by(d.c.title, d.c.slug))
    return [DocumentRow(**row._mapping) for row in rows if _visible(access, sink, row.label)]


async def document(
    conn: AsyncConnection, access: ProjectAccess, slug: str, sink: str | None, *, lock: bool = False
) -> DocumentRow:
    """Document ``slug`` as the member sees it; 404 when there is none or they cannot see it. ``lock`` takes its row
    lock first, for a write."""
    found = await _find(conn, access, slug, lock=lock)
    if found is None or not _visible(access, sink, found.label):
        raise missing(access.name, slug)
    return found


async def _find(conn: AsyncConnection, access: ProjectAccess, slug: str, *, lock: bool = False) -> DocumentRow | None:
    reader(access)
    d = tables.docs.documents
    if lock:
        held = select(d.c.id).where(d.c.project_id == access.project_id, d.c.slug == slug).with_for_update()
        await conn.execute(held)
    row = (await conn.execute(_documents_query(access.project_id).where(d.c.slug == slug))).first()
    return None if row is None else DocumentRow(**row._mapping)


def _versions_query(document_id: int):
    v, u = tables.docs.versions, tables.users
    return (
        select(v.c.id, v.c.number, v.c.title, v.c.summary, u.c.login.label("author"), v.c.created_at)
        .join_from(v, u, u.c.id == v.c.author_id)
        .where(v.c.document_id == document_id)
    )


async def versions(conn: AsyncConnection, document_id: int) -> list[VersionRow]:
    """Every version of a document, the newest first, without their bodies."""
    query = _versions_query(document_id).order_by(tables.docs.versions.c.number.desc())
    return [VersionRow(**row._mapping) for row in await conn.execute(query)]


async def images_of(conn: AsyncConnection, version_id: int) -> list[ImageRef]:
    """The images version ``version_id`` shows, by name."""
    k, i = tables.docs.version_images, tables.docs.images
    query = (
        select(k.c.name, k.c.sha256, i.c.media_type, i.c.size)
        .join_from(k, i, (i.c.project_id == k.c.project_id) & (i.c.sha256 == k.c.sha256))
        .where(k.c.version_id == version_id)
        .order_by(k.c.name)
    )
    return [ImageRef(**row._mapping) for row in await conn.execute(query)]


async def version(conn: AsyncConnection, doc: DocumentRow, number: int | None = None) -> VersionRow:
    """Version ``number`` of ``doc`` (its latest when None) whole: its body and images; 404 when it has none such."""
    v = tables.docs.versions
    wanted = number if number is not None else doc.latest_version
    row = (await conn.execute(_versions_query(doc.id).add_columns(v.c.body).where(v.c.number == wanted))).first()
    if row is None:
        raise DocsError(404, f"document {doc.slug} has no version {wanted}: it has 1 to {doc.latest_version}")
    return VersionRow(**row._mapping, images=await images_of(conn, row.id))


async def shows_image(conn: AsyncConnection, doc: DocumentRow, sha256: str) -> tuple[str, int] | None:
    """The media type and size of image ``sha256`` when a version of ``doc`` shows it; None otherwise."""
    k, v, i = tables.docs.version_images, tables.docs.versions, tables.docs.images
    query = (
        select(i.c.media_type, i.c.size)
        .join_from(k, v, v.c.id == k.c.version_id)
        .join(i, (i.c.project_id == k.c.project_id) & (i.c.sha256 == k.c.sha256))
        .where(v.c.document_id == doc.id, k.c.sha256 == sha256)
        .limit(1)
    )
    row = (await conn.execute(query)).first()
    return None if row is None else (row.media_type, row.size)


# Saving


async def _project_images(conn: AsyncConnection, access: ProjectAccess, wanted: dict[str, str]) -> None:
    """422 naming every image of ``wanted`` ({name: sha256}) that is no image of the project."""
    i = tables.docs.images
    query = select(i.c.sha256).where(i.c.project_id == access.project_id, i.c.sha256.in_(set(wanted.values()) or [""]))
    held = set((await conn.execute(query)).scalars())
    lacking = sorted(name for name, sha256 in wanted.items() if sha256 not in held)
    if lacking:
        raise DocsError(
            422,
            f"{len(lacking)} image(s) are no image of project {access.name}, such as {lacking[0]}: upload each first "
            f"(POST /v1/x/docs/{access.name}/images); nothing was saved",
        )


async def _write_version(
    conn: AsyncConnection, access: ProjectAccess, document_id: int, number: int, author_id: int, content: dict, images
) -> None:
    """Version ``number`` of document ``document_id``, showing ``images`` ({name: sha256})."""
    v = tables.docs.versions
    values = {"document_id": document_id, "number": number, "author_id": author_id, **content}
    version_id = (await conn.execute(insert(v).values(values).returning(v.c.id))).scalar_one()
    if images:
        rows = [
            {"version_id": version_id, "name": name, "project_id": access.project_id, "sha256": sha256}
            for name, sha256 in images.items()
        ]
        await conn.execute(insert(tables.docs.version_images), rows)


def _unchanged(latest: VersionRow, content: dict, images: dict[str, str]) -> bool:
    shown = {image.name: image.sha256 for image in latest.images}
    return (latest.title, latest.body) == (content["title"], content["body"]) and shown == images


async def _create(
    conn: AsyncConnection, access: ProjectAccess, user_id: int, slug: str, content: dict, images: dict, label
) -> int:
    if label is not None and not has_role(access.role, "admin"):
        raise DocsError(403, f"only an admin of project {access.name} sets the label of a document")
    stored = access.push_label(label)
    through = sink_of(access, None)
    if through is None or not access.visible(stored, through):
        raise DocsError(
            422,
            f"a new document of project {access.name} carries label {stored.get('level')}, which your grant does not "
            "read: an admin of the project creates it with a label you read",
        )
    d = tables.docs.documents
    values = {"project_id": access.project_id, "slug": slug, "title": content["title"], "label": stored}
    document_id = (
        await conn.execute(insert(d).values(**values, latest_version=1, created_by=user_id).returning(d.c.id))
    ).scalar_one()
    await _write_version(conn, access, document_id, 1, user_id, content, images)
    return document_id


async def save(
    conn: AsyncConnection,
    access: ProjectAccess,
    user_id: int,
    slug: str,
    content: dict,
    images: dict[str, str],
    *,
    base_version: int | None = None,
    label: dict | None = None,
    sink: str | None = None,
) -> Saved:
    """Save ``content`` ({title, body, summary}) showing ``images`` ({name: sha256}) as the next version of document
    ``slug``, created when the project has none of that slug (see the module)."""
    writer(access, "saving a document")
    await _project_images(conn, access, images)
    found = await _find(conn, access, slug, lock=True)
    if found is None:
        if base_version:
            raise DocsError(409, f"document {slug} of project {access.name} does not exist: save it without a base")
        await _create(conn, access, user_id, slug, content, images, label)
        return Saved(await document(conn, access, slug, sink), 1, True, True)
    doc = await document(conn, access, slug, sink)
    if label is not None:
        raise DocsError(422, f"document {slug} exists: its label was given when it was created")
    if base_version == 0:
        raise DocsError(409, f"document {slug} exists already in project {access.name}: pick another slug")
    if base_version is not None and base_version != doc.latest_version:
        raise DocsError(
            409,
            f"document {slug} is at version {doc.latest_version}, not {base_version}: someone saved it since you read "
            "it; read it again and save on the newer version",
        )
    if _unchanged(await version(conn, doc), content, images):
        return Saved(doc, doc.latest_version, False, False)
    number = doc.latest_version + 1
    await _write_version(conn, access, doc.id, number, user_id, content, images)
    d = tables.docs.documents
    changed = {"title": content["title"], "latest_version": number, "updated_at": func.now()}
    await conn.execute(update(d).values(**changed).where(d.c.id == doc.id))
    return Saved(await document(conn, access, slug, sink), number, False, True)
