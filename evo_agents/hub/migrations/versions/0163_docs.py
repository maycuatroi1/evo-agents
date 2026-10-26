"""The tables of the feature docs: a project's documents, Markdown with images, each save a version.

Revision ID: 0163
Revises: 0162
Create Date: 2026-10-26

The names below are copies of ``evo_agents.hub.tables_docs`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

docs_documents holds a project's documents by slug, with the title and number of the latest version and the label each
carries; docs_versions each save of one, never changed after; docs_images the images uploaded for a project's
documents, once each by SHA-256, with their media type and size; docs_version_images the images a version shows, by
the name its Markdown gives each. The feature is off in every project until a layer of the features turns it on.

Going back drops the four tables. The blobs of the images stay in the blob store, which nothing deletes then: no row
of 0162 refers to them, and the collection of unreferenced blobs reads only what a deletion recorded.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0163"
down_revision = "0162"
branch_labels = None
depends_on = None

SLUG = r"^[a-z0-9][a-z0-9-]{0,99}$"
IMAGE_NAME = r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}(/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}){0,4}$"
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
SHA256 = r"^[0-9a-f]{64}$"
MAX_TITLE_CHARS = 300
MAX_BODY_CHARS = 500_000
MAX_SUMMARY_CHARS = 500
MAX_IMAGE_NAME_CHARS = 200
MAX_IMAGE_BYTES = 10 * 1024 * 1024

col = sa.column
TABLES = ("docs_version_images", "docs_images", "docs_versions", "docs_documents")  # in the order going back drops them


def _key(table: str, columns: list[str], target: str, fields: list[str], *, ondelete: str):
    targets = [f"{target}.{field}" for field in fields]
    return sa.ForeignKeyConstraint(columns, targets, ondelete=ondelete, name=f"{table}_{columns[-1]}_fkey")


def _length(name: str, low: int, high: int):
    return sa.func.char_length(col(name)).between(low, high)


def _documents() -> None:
    name = "docs_documents"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(name, ["project_id"], "projects", ["id"], ondelete="RESTRICT"),
        _key(name, ["created_by"], "users", ["id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="docs_documents_pkey"),
        sa.UniqueConstraint("project_id", "slug", name="docs_documents_slug_key"),
        sa.CheckConstraint(
            sa.and_(
                col("slug").regexp_match(SLUG),
                _length("title", 1, MAX_TITLE_CHARS),
                col("latest_version") >= 1,
            ),
            name="docs_documents_check",
        ),
    )


def _versions() -> None:
    name = "docs_versions"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(name, ["document_id"], "docs_documents", ["id"], ondelete="CASCADE"),
        _key(name, ["author_id"], "users", ["id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="docs_versions_pkey"),
        sa.UniqueConstraint("document_id", "number", name="docs_versions_number_key"),
        sa.CheckConstraint(
            sa.and_(
                col("number") >= 1,
                _length("title", 1, MAX_TITLE_CHARS),
                _length("body", 0, MAX_BODY_CHARS),
                _length("summary", 0, MAX_SUMMARY_CHARS),
            ),
            name="docs_versions_check",
        ),
    )


def _images() -> None:
    name = "docs_images"
    op.create_table(
        name,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(name, ["project_id"], "projects", ["id"], ondelete="RESTRICT"),
        _key(name, ["uploaded_by"], "users", ["id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("project_id", "sha256", name="docs_images_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("sha256").regexp_match(SHA256),
                col("media_type").in_(IMAGE_TYPES),
                col("size").between(1, MAX_IMAGE_BYTES),
            ),
            name="docs_images_check",
        ),
    )
    op.create_index("docs_images_sha256_idx", name, ["sha256"])


def _version_images() -> None:
    name = "docs_version_images"
    op.create_table(
        name,
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        _key(name, ["version_id"], "docs_versions", ["id"], ondelete="CASCADE"),
        _key(name, ["project_id", "sha256"], "docs_images", ["project_id", "sha256"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("version_id", "name", name="docs_version_images_pkey"),
        sa.CheckConstraint(
            sa.and_(_length("name", 1, MAX_IMAGE_NAME_CHARS), col("name").regexp_match(IMAGE_NAME)),
            name="docs_version_images_check",
        ),
    )
    op.create_index("docs_version_images_image_idx", name, ["project_id", "sha256"])


def upgrade() -> None:
    _documents()
    _versions()
    _images()
    _version_images()


def downgrade() -> None:
    for name in TABLES:
        op.drop_table(name)
