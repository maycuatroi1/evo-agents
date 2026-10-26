"""The tables of the hub's feature docs (0163), which ``evo_agents.hub.tables`` adds to its metadata as
``tables.docs``: queries name them there (``tables.docs.documents``). The feature's code is
``evo_agents.hub.server.docs``.

- docs_documents: a document of a project, Markdown with images, by its slug: its title as the latest version has it,
  the number of that version, and the label every version and image of it is read under (the project's by default).
- docs_versions: each save of a document, never changed after: its number (1 and up within the document), its title
  and Markdown body, what it changed (``summary``, may be empty) and who wrote it, when.
- docs_images: the images uploaded for a project's documents, once each by SHA-256: their media type, size and who
  uploaded them first. The row refers to the blob of the bytes (``evo_agents.hub.blob_gc``), so none is deleted.
- docs_version_images: the images a version shows, by the name its Markdown gives each (``shots/board.png``).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

SLUG = r"^[a-z0-9][a-z0-9-]{0,99}$"  # docs_documents.slug
IMAGE_NAME = r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}(/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}){0,4}$"  # docs_version_images.name
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")  # docs_images.media_type
MAX_TITLE_CHARS = 300
MAX_BODY_CHARS = 500_000
MAX_SUMMARY_CHARS = 500
MAX_IMAGE_NAME_CHARS = 200
MAX_IMAGES = 100  # of a version
MiB = 1024 * 1024
MAX_IMAGE_BYTES = 10 * MiB


@dataclass(frozen=True)
class DocsTables:
    """The tables of the feature docs, by what each holds."""

    documents: Table
    versions: Table
    images: Table
    version_images: Table


def _key(table: str, columns: list[str], target: str, fields: list[str], *, ondelete: str) -> ForeignKeyConstraint:
    targets = [f"{target}.{field}" for field in fields]
    return ForeignKeyConstraint(columns, targets, ondelete=ondelete, name=f"{table}_{columns[-1]}_fkey")


def _documents(metadata: MetaData) -> Table:
    name = "docs_documents"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("slug", Text, nullable=False),
        Column("title", Text, nullable=False),
        Column("label", JSONB, nullable=False),
        Column("latest_version", Integer, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _key(name, ["project_id"], "projects", ["id"], ondelete="RESTRICT"),
        _key(name, ["created_by"], "users", ["id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="docs_documents_pkey"),
        UniqueConstraint("project_id", "slug", name="docs_documents_slug_key"),
    )


def _versions(metadata: MetaData) -> Table:
    name = "docs_versions"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("document_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("title", Text, nullable=False),
        Column("body", Text, nullable=False),
        Column("summary", Text, nullable=False, server_default=""),
        Column("author_id", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _key(name, ["document_id"], "docs_documents", ["id"], ondelete="CASCADE"),
        _key(name, ["author_id"], "users", ["id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="docs_versions_pkey"),
        UniqueConstraint("document_id", "number", name="docs_versions_number_key"),
    )


def _images(metadata: MetaData) -> Table:
    name = "docs_images"
    return Table(
        name,
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("sha256", Text, nullable=False),
        Column("media_type", Text, nullable=False),
        Column("size", BigInteger, nullable=False),
        Column("uploaded_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _key(name, ["project_id"], "projects", ["id"], ondelete="RESTRICT"),
        _key(name, ["uploaded_by"], "users", ["id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("project_id", "sha256", name="docs_images_pkey"),
        Index("docs_images_sha256_idx", "sha256"),
    )


def _version_images(metadata: MetaData) -> Table:
    name = "docs_version_images"
    return Table(
        name,
        metadata,
        Column("version_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("sha256", Text, nullable=False),
        _key(name, ["version_id"], "docs_versions", ["id"], ondelete="CASCADE"),
        _key(name, ["project_id", "sha256"], "docs_images", ["project_id", "sha256"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("version_id", "name", name="docs_version_images_pkey"),
        Index("docs_version_images_image_idx", "project_id", "sha256"),
    )


def docs_tables(metadata: MetaData) -> DocsTables:
    """The tables of the feature docs, added to ``metadata``."""
    return DocsTables(_documents(metadata), _versions(metadata), _images(metadata), _version_images(metadata))
