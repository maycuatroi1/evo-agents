"""Every SQL statement of the feature docs (``evo_agents.hub.server.docs``): the documents of a project, their versions
and the images each shows, the images of a project, what the knowledge graph reads, and the members and runs the tool
``docs__read`` reads for.

A save writes through ``DocumentStore``, a ``VersionedStore`` keyed by slug: it takes the document's row lock first,
so two saves of one document are one after the other; a save made on a version (``base_version``) is refused (409)
unless that is the latest version, so an editor does not save over a version it never read, and with 0 unless the
document does not exist yet, so a new one never lands on another of the same slug; a save whose title, body, format
and images are those of the latest version writes nothing. The checks of who may save what are the service's.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.base.versions import VersionedStore
from evo_agents.hub.server.docs.models import DocsError, DocumentRow, Draft, ImageRef, KgDocument, VersionRow


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
            v.c.format,
        )
        .select_from(
            d.join(creator, creator.c.id == d.c.created_by)
            .join(v, (v.c.document_id == d.c.id) & (v.c.number == d.c.latest_version))
            .join(author, author.c.id == v.c.author_id)
        )
        .where(d.c.project_id == project_id)
    )


async def documents(conn: AsyncConnection, project_id: int) -> list[DocumentRow]:
    """Every document of the project, whatever its label, by title."""
    d = tables.docs.documents
    rows = await conn.execute(_documents_query(project_id).order_by(d.c.title, d.c.slug))
    return [DocumentRow(**row._mapping) for row in rows]


async def find(conn: AsyncConnection, project_id: int, slug: str, *, lock: bool = False) -> DocumentRow | None:
    """Document ``slug`` of the project, whatever its label; ``lock`` takes its row lock first, for a write."""
    d = tables.docs.documents
    if lock:
        held = select(d.c.id).where(d.c.project_id == project_id, d.c.slug == slug).with_for_update()
        await conn.execute(held)
    row = (await conn.execute(_documents_query(project_id).where(d.c.slug == slug))).first()
    return None if row is None else DocumentRow(**row._mapping)


def _versions_query(document_id: int):
    v, u = tables.docs.versions, tables.users
    return (
        select(v.c.id, v.c.number, v.c.title, v.c.summary, u.c.login.label("author"), v.c.created_at, v.c.format)
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


async def version(conn: AsyncConnection, document_id: int, number: int) -> VersionRow | None:
    """Version ``number`` of a document whole, its body and images; None when it has none such."""
    v = tables.docs.versions
    query = _versions_query(document_id).add_columns(v.c.body).where(v.c.number == number)
    row = (await conn.execute(query)).first()
    return None if row is None else VersionRow(**row._mapping, images=await images_of(conn, row.id))


async def kg_documents(conn: AsyncConnection, project_id: int) -> list[KgDocument]:
    """Every document of project ``project_id`` at its latest version, by slug, whatever its label: the build of the
    graph labels each item with it, so a reader sees only what their sink lets through (``evo_agents.hub.server.docs.
    kg``)."""
    d, v, u = tables.docs.documents, tables.docs.versions, tables.users
    query = (
        select(
            d.c.slug,
            v.c.title,
            d.c.label,
            v.c.number.label("version"),
            u.c.login.label("author"),
            v.c.body,
            v.c.created_at.label("updated_at"),
            v.c.format,
        )
        .select_from(
            d.join(v, (v.c.document_id == d.c.id) & (v.c.number == d.c.latest_version)).join(u, u.c.id == v.c.author_id)
        )
        .where(d.c.project_id == project_id)
        .order_by(d.c.slug)
    )
    return [KgDocument(**row._mapping) for row in await conn.execute(query)]


async def shows_image(conn: AsyncConnection, document_id: int, sha256: str) -> tuple[str, int] | None:
    """The media type and size of image ``sha256`` when a version of the document shows it; None otherwise."""
    k, v, i = tables.docs.version_images, tables.docs.versions, tables.docs.images
    query = (
        select(i.c.media_type, i.c.size)
        .join_from(k, v, v.c.id == k.c.version_id)
        .join(i, (i.c.project_id == k.c.project_id) & (i.c.sha256 == k.c.sha256))
        .where(v.c.document_id == document_id, k.c.sha256 == sha256)
        .limit(1)
    )
    row = (await conn.execute(query)).first()
    return None if row is None else (row.media_type, row.size)


async def project_images(conn: AsyncConnection, project_id: int, sha256s: set[str]) -> set[str]:
    """The images of ``sha256s`` the project holds (``docs_images``)."""
    i = tables.docs.images
    query = select(i.c.sha256).where(i.c.project_id == project_id, i.c.sha256.in_(sha256s or [""]))
    return set((await conn.execute(query)).scalars())


async def record_image(conn: AsyncConnection, project_id: int, user_id: int, found: dict) -> bool:
    """The project's row of an image (sha256, media_type, size); True when this call wrote it."""
    i = tables.docs.images
    added = (
        pg_insert(i)
        .values(project_id=project_id, uploaded_by=user_id, **found)
        .on_conflict_do_nothing(index_elements=[i.c.project_id, i.c.sha256])
        .returning(i.c.sha256)
    )
    return (await conn.execute(added)).first() is not None


async def user_id_of(conn: AsyncConnection, login: str) -> int | None:
    """The id of the member ``login``; None when the hub has none."""
    return (await conn.execute(select(tables.users.c.id).where(tables.users.c.login == login))).scalar_one_or_none()


async def run_of(conn: AsyncConnection, run_id: int) -> Any:
    """The kind of run ``run_id`` and the member who dispatched it (``kind``, ``dispatched_by``)."""
    r = tables.runs
    return (await conn.execute(select(r.c.kind, r.c.dispatched_by).where(r.c.id == run_id))).one()


class DocumentStore(VersionedStore[str, DocumentRow]):
    """The documents of one project as versioned records keyed by slug (see the module): ``save`` writes a ``Draft``
    as the next version."""

    noun = "document"

    def __init__(self, project_id: int, project: str):
        self.project_id, self.project = project_id, project

    async def held(self, conn: AsyncConnection, key: str) -> DocumentRow | None:
        return await find(conn, self.project_id, key, lock=True)

    def version_of(self, row: DocumentRow) -> int:
        return row.latest_version

    def missing(self, key: str, base: int) -> Exception:
        return DocsError(409, f"document {key} of project {self.project} does not exist: save it without a base")

    def refusal(self, stale: list) -> Exception:
        return DocsError(
            409,
            f"{'; '.join(map(str, stale))}: someone saved it since you read it; read it again and save on the newer "
            "version",
        )

    async def check(self, conn: AsyncConnection, key: str, row: DocumentRow | None, base: int | None) -> None:
        if row is not None and base == 0:
            raise DocsError(409, f"document {key} exists already in project {self.project}: pick another slug")
        await super().check(conn, key, row, base)

    def unchanged(self, row: DocumentRow, values: Draft) -> bool:
        return values.same_as_latest

    async def insert(
        self, conn: AsyncConnection, key: str, row: DocumentRow | None, version: int, values: Draft
    ) -> None:
        d = tables.docs.documents
        if row is None:
            created = {"project_id": self.project_id, "slug": key, "title": values.content["title"]}
            created |= {"label": values.label, "latest_version": version, "created_by": values.author_id}
            document_id = (await conn.execute(insert(d).values(**created).returning(d.c.id))).scalar_one()
            await self._write_version(conn, document_id, version, values)
            return
        await self._write_version(conn, row.id, version, values)
        changed = {"title": values.content["title"], "latest_version": version, "updated_at": func.now()}
        await conn.execute(update(d).values(**changed).where(d.c.id == row.id))

    async def _write_version(self, conn: AsyncConnection, document_id: int, number: int, values: Draft) -> None:
        """Version ``number`` of document ``document_id``, showing the draft's images ({name: sha256})."""
        v = tables.docs.versions
        written = {"document_id": document_id, "number": number, "author_id": values.author_id, **values.content}
        version_id = (await conn.execute(insert(v).values(written).returning(v.c.id))).scalar_one()
        if values.images:
            rows = [
                {"version_id": version_id, "name": name, "project_id": self.project_id, "sha256": sha256}
                for name, sha256 in values.images.items()
            ]
            await conn.execute(insert(tables.docs.version_images), rows)
