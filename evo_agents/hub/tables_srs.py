"""The tables of the core plugin srs (0046), which ``evo_agents.hub.tables`` adds to its metadata as ``tables.srs``:
queries name them there (``tables.srs.documents``). The plugin's code is ``evo_agents.core_plugins.srs``; the hub owns
the tables because its one chain of migrations creates them (``docs/plugins.md``, Core plugins).

- srs_documents: a BA document of a project, an SRS of a template or a free specification (``kind``), with its slug,
  title, the version approved last, and the label every row of the document carries (the project's by default).
- srs_versions: each save of a document, never changed after: its number (1 and up within the document), the number
  it was built on, its state (draft, in_review, approved, superseded), the review round, its author, the run that
  drafted it when an agent did, its summary, the text before the first heading and the SHA-256 of its content.
- srs_texts: the Markdown bodies, kept once by SHA-256, so a version that leaves an item as it was adds no text.
- srs_items: an item's identity across versions: its key (the hub's, fixed), its kind, its code (null for a plain
  section), the version it appeared in and the one it was dropped in.
- srs_codes: every code and alias an item ever held, by the code's normalized form, unique in the project, so no two
  items share one and a dropped item's codes are never given again.
- srs_item_versions: an item as one version has it: parent, position, heading level, title, fields and body.
- srs_refs: links between the items of a version (uses_screen, refines, relates).
- srs_reviews: an approval or a request for changes of a version in a review round, with the role it counts for.
- srs_comments: threads of comments on a document, a version or an item.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

DOCUMENT_KINDS = ("srs", "spec")  # srs_documents.kind
ITEM_KINDS = (  # srs_items.kind
    "section",
    "use_case",
    "business_rule",
    "screen",
    "nfr",
    "cbr",
    "glossary",
    "approval_page",
    "revision_history",
)
VERSION_STATES = ("draft", "in_review", "approved", "superseded")  # srs_versions.state
REF_RELS = ("uses_screen", "refines", "relates")  # srs_refs.rel
REVIEW_ROLES = ("ba", "qa", "dev")  # srs_reviews.role
REVIEW_DECISIONS = ("approve", "request_changes")  # srs_reviews.decision
SLUG = r"^[a-z0-9][a-z0-9-]{0,99}$"  # srs_documents.slug
MAX_TITLE_CHARS = 300  # of a document
MAX_ITEM_TITLE_CHARS = 500
MAX_SUMMARY_CHARS = 500  # of a version
MAX_CODE_CHARS = 100
MAX_REVIEW_CHARS = 10_000
MAX_COMMENT_CHARS = 20_000


def code_key(code: str) -> str:
    """The form two codes are compared in, as srs_codes.code_key holds it: whitespace runs as one space, case folded.
    The plugin tasks finds an item by code in this form too."""
    return " ".join(code.split()).casefold()


@dataclass(frozen=True)
class SrsTables:
    """The tables of the core plugin srs, by what each holds."""

    documents: Table
    texts: Table
    versions: Table
    items: Table
    codes: Table
    item_versions: Table
    refs: Table
    reviews: Table
    comments: Table


def _key(table: str, column: str, target: str, *, ondelete: str, field: str = "id") -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.{field}"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _documents(metadata: MetaData) -> Table:
    name = "srs_documents"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("slug", Text, nullable=False),
        Column("title", Text, nullable=False),
        Column("kind", Text, nullable=False, server_default="srs"),
        Column("template", Text, nullable=False, server_default="srs-basic"),
        Column("approved_version", Integer),
        Column("label", JSONB, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("archived_at", DateTime(timezone=True)),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="srs_documents_pkey"),
        UniqueConstraint("project_id", "slug", name="srs_documents_slug_key"),
    )


def _texts(metadata: MetaData) -> Table:
    return Table(
        "srs_texts",
        metadata,
        Column("sha256", Text, nullable=False),
        Column("body", Text, nullable=False),
        PrimaryKeyConstraint("sha256", name="srs_texts_pkey"),
    )


def _versions(metadata: MetaData) -> Table:
    name = "srs_versions"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("document_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("parent_number", Integer),
        Column("state", Text, nullable=False, server_default="draft"),
        Column("round", Integer, nullable=False, server_default="0"),
        Column("author_id", BigInteger, nullable=False),
        Column("drafted_by_run", BigInteger),
        Column("summary", Text, nullable=False, server_default=""),
        Column("preamble_sha256", Text),
        Column("content_sha256", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("submitted_at", DateTime(timezone=True)),
        Column("decided_at", DateTime(timezone=True)),
        _key(name, "document_id", "srs_documents", ondelete="CASCADE"),
        _key(name, "author_id", "users", ondelete="RESTRICT"),
        _key(name, "drafted_by_run", "runs", ondelete="SET NULL"),
        _key(name, "preamble_sha256", "srs_texts", ondelete="RESTRICT", field="sha256"),
        PrimaryKeyConstraint("id", name="srs_versions_pkey"),
        UniqueConstraint("document_id", "number", name="srs_versions_number_key"),
    )


def _items(metadata: MetaData) -> Table:
    name = "srs_items"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("document_id", BigInteger, nullable=False),
        Column("key", Text, nullable=False),
        Column("kind", Text, nullable=False),
        Column("code", Text),
        Column("created_in", Integer, nullable=False),
        Column("retired_in", Integer),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "document_id", "srs_documents", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="srs_items_pkey"),
        UniqueConstraint("document_id", "key", name="srs_items_key_key"),
    )


def _codes(metadata: MetaData) -> Table:
    name = "srs_codes"
    return Table(
        name,
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("code_key", Text, nullable=False),
        Column("item_id", BigInteger, nullable=False),
        Column("code", Text, nullable=False),
        Column("is_primary", Boolean, nullable=False, server_default=false()),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "item_id", "srs_items", ondelete="CASCADE"),
        PrimaryKeyConstraint("project_id", "code_key", name="srs_codes_pkey"),
        Index("srs_codes_item_idx", "item_id"),
    )


def _item_versions(metadata: MetaData) -> Table:
    name = "srs_item_versions"
    return Table(
        name,
        metadata,
        Column("version_id", BigInteger, nullable=False),
        Column("item_id", BigInteger, nullable=False),
        Column("parent_item_id", BigInteger),
        Column("position", Integer, nullable=False),
        Column("heading_level", Integer),
        Column("title", Text, nullable=False),
        Column("fields", JSONB, nullable=False, server_default="{}"),
        Column("body_sha256", Text, nullable=False),
        _key(name, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(name, "item_id", "srs_items", ondelete="CASCADE"),
        _key(name, "parent_item_id", "srs_items", ondelete="CASCADE"),
        _key(name, "body_sha256", "srs_texts", ondelete="RESTRICT", field="sha256"),
        PrimaryKeyConstraint("version_id", "item_id", name="srs_item_versions_pkey"),
        UniqueConstraint("version_id", "position", name="srs_item_versions_position_key"),
        Index("srs_item_versions_item_idx", "item_id"),
    )


def _refs(metadata: MetaData) -> Table:
    name = "srs_refs"
    return Table(
        name,
        metadata,
        Column("version_id", BigInteger, nullable=False),
        Column("from_item", BigInteger, nullable=False),
        Column("rel", Text, nullable=False),
        Column("to_item", BigInteger, nullable=False),
        _key(name, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(name, "from_item", "srs_items", ondelete="CASCADE"),
        _key(name, "to_item", "srs_items", ondelete="CASCADE"),
        PrimaryKeyConstraint("version_id", "from_item", "rel", "to_item", name="srs_refs_pkey"),
    )


def _reviews(metadata: MetaData) -> Table:
    name = "srs_reviews"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("version_id", BigInteger, nullable=False),
        Column("round", Integer, nullable=False),
        Column("reviewer_id", BigInteger, nullable=False),
        Column("role", Text),
        Column("decision", Text, nullable=False),
        Column("body", Text, nullable=False, server_default=""),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        _key(name, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(name, "reviewer_id", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="srs_reviews_pkey"),
        UniqueConstraint("version_id", "round", "reviewer_id", name="srs_reviews_reviewer_key"),
    )


def _comments(metadata: MetaData) -> Table:
    name = "srs_comments"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("document_id", BigInteger, nullable=False),
        Column("version_id", BigInteger),
        Column("item_id", BigInteger),
        Column("parent_id", BigInteger),
        Column("author_id", BigInteger, nullable=False),
        Column("run_id", BigInteger),
        Column("body", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("resolved_by", BigInteger),
        Column("resolved_at", DateTime(timezone=True)),
        _key(name, "document_id", "srs_documents", ondelete="CASCADE"),
        _key(name, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(name, "item_id", "srs_items", ondelete="CASCADE"),
        _key(name, "parent_id", "srs_comments", ondelete="CASCADE"),
        _key(name, "author_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "resolved_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="srs_comments_pkey"),
        Index("srs_comments_document_idx", "document_id", "id"),
    )


def srs_tables(metadata: MetaData) -> SrsTables:
    """The tables of the core plugin srs, added to ``metadata`` (which holds projects, users and runs already)."""
    return SrsTables(
        documents=_documents(metadata),
        texts=_texts(metadata),
        versions=_versions(metadata),
        items=_items(metadata),
        codes=_codes(metadata),
        item_versions=_item_versions(metadata),
        refs=_refs(metadata),
        reviews=_reviews(metadata),
        comments=_comments(metadata),
    )
