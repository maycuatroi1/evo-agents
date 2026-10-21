"""The tables of the core plugin srs: BA documents with versions, coded items, reviews and comments.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-19

The names below are copies of ``evo_agents.hub.tables_srs`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

The plugin is ``evo_agents.core_plugins.srs``; every table carries its prefix. srs_documents holds a project's BA
documents (an SRS of a template, or a free specification) with the label each carries; srs_versions each save of one,
never changed after, in a straight line per document (draft, in_review, approved, superseded); srs_texts the Markdown
bodies once each by SHA-256; srs_items the identity of an item across versions with its kind and code; srs_codes every
code and alias an item held, unique in the project by their normalized form, never given again; srs_item_versions an
item as one version has it; srs_refs links between items of a version; srs_reviews the approvals and requests for
changes of a version per review round; srs_comments threads on a document, a version or an item.

Going back drops the nine tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

DOCUMENT_KINDS = ("srs", "spec")
ITEM_KINDS = (
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
VERSION_STATES = ("draft", "in_review", "approved", "superseded")
REF_RELS = ("uses_screen", "refines", "relates")
REVIEW_ROLES = ("ba", "qa", "dev")
REVIEW_DECISIONS = ("approve", "request_changes")
SLUG = r"^[a-z0-9][a-z0-9-]{0,99}$"
MAX_TITLE_CHARS = 300
MAX_ITEM_TITLE_CHARS = 500
MAX_SUMMARY_CHARS = 500
MAX_CODE_CHARS = 100
MAX_REVIEW_CHARS = 10_000
MAX_COMMENT_CHARS = 20_000
SHA256_CHARS = 64

col = sa.column
TABLES = (  # in the order going back drops them
    "srs_comments",
    "srs_reviews",
    "srs_refs",
    "srs_item_versions",
    "srs_codes",
    "srs_items",
    "srs_versions",
    "srs_texts",
    "srs_documents",
)


def _key(table: str, column: str, target: str, *, ondelete: str, field: str = "id"):
    return sa.ForeignKeyConstraint([column], [f"{target}.{field}"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _length(name: str, low: int, high: int):
    return sa.func.char_length(col(name)).between(low, high)


def _documents() -> None:
    name = "srs_documents"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="srs", nullable=False),
        sa.Column("template", sa.Text(), server_default="srs-basic", nullable=False),
        sa.Column("approved_version", sa.Integer(), nullable=True),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="srs_documents_pkey"),
        sa.UniqueConstraint("project_id", "slug", name="srs_documents_slug_key"),
        sa.CheckConstraint(
            sa.and_(
                col("slug").regexp_match(SLUG),
                _length("title", 1, MAX_TITLE_CHARS),
                col("kind").in_(DOCUMENT_KINDS),
                _length("template", 1, 100),
                col("approved_version") >= 1,
            ),
            name="srs_documents_check",
        ),
    )


def _texts() -> None:
    op.create_table(
        "srs_texts",
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("sha256", name="srs_texts_pkey"),
        sa.CheckConstraint(sa.func.char_length(col("sha256")) == SHA256_CHARS, name="srs_texts_check"),
    )


def _versions() -> None:
    name, state = "srs_versions", col("state")
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("parent_number", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), server_default="draft", nullable=False),
        sa.Column("round", sa.Integer(), server_default="0", nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("drafted_by_run", sa.BigInteger(), nullable=True),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("preamble_sha256", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        _key(name, "document_id", "srs_documents", ondelete="CASCADE"),
        _key(name, "author_id", "users", ondelete="RESTRICT"),
        _key(name, "drafted_by_run", "runs", ondelete="SET NULL"),
        _key(name, "preamble_sha256", "srs_texts", ondelete="RESTRICT", field="sha256"),
        sa.PrimaryKeyConstraint("id", name="srs_versions_pkey"),
        sa.UniqueConstraint("document_id", "number", name="srs_versions_number_key"),
        sa.CheckConstraint(
            sa.and_(
                col("number") >= 1,
                col("parent_number") < col("number"),
                state.in_(VERSION_STATES),
                col("round") >= 0,
                sa.func.char_length(col("summary")) <= MAX_SUMMARY_CHARS,
                sa.func.char_length(col("content_sha256")) == SHA256_CHARS,
                sa.or_(state != "approved", col("decided_at").is_not(None)),
                sa.or_(state != "in_review", col("submitted_at").is_not(None)),
            ),
            name="srs_versions_check",
        ),
    )


def _items() -> None:
    name = "srs_items"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("created_in", sa.Integer(), nullable=False),
        sa.Column("retired_in", sa.Integer(), nullable=True),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "document_id", "srs_documents", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="srs_items_pkey"),
        sa.UniqueConstraint("document_id", "key", name="srs_items_key_key"),
        sa.CheckConstraint(
            sa.and_(
                _length("key", 1, 64),
                col("kind").in_(ITEM_KINDS),
                _length("code", 1, MAX_CODE_CHARS),
                col("created_in") >= 1,
                col("retired_in") > col("created_in"),
            ),
            name="srs_items_check",
        ),
    )


def _codes() -> None:
    name = "srs_codes"
    op.create_table(
        name,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("code_key", sa.Text(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "item_id", "srs_items", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "code_key", name="srs_codes_pkey"),
        sa.CheckConstraint(
            sa.and_(_length("code", 1, MAX_CODE_CHARS), _length("code_key", 1, MAX_CODE_CHARS)), name="srs_codes_check"
        ),
    )
    op.create_index("srs_codes_item_idx", name, ["item_id"])


def _item_versions() -> None:
    name = "srs_item_versions"
    op.create_table(
        name,
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_item_id", sa.BigInteger(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("heading_level", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("body_sha256", sa.Text(), nullable=False),
        _key(name, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(name, "item_id", "srs_items", ondelete="CASCADE"),
        _key(name, "parent_item_id", "srs_items", ondelete="CASCADE"),
        _key(name, "body_sha256", "srs_texts", ondelete="RESTRICT", field="sha256"),
        sa.PrimaryKeyConstraint("version_id", "item_id", name="srs_item_versions_pkey"),
        sa.UniqueConstraint("version_id", "position", name="srs_item_versions_position_key"),
        sa.CheckConstraint(
            sa.and_(
                col("position") >= 0,
                col("heading_level").between(1, 6),
                sa.func.char_length(col("title")) <= MAX_ITEM_TITLE_CHARS,
                col("parent_item_id") != col("item_id"),
            ),
            name="srs_item_versions_check",
        ),
    )
    op.create_index("srs_item_versions_item_idx", name, ["item_id"])


def _refs() -> None:
    name = "srs_refs"
    op.create_table(
        name,
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("from_item", sa.BigInteger(), nullable=False),
        sa.Column("rel", sa.Text(), nullable=False),
        sa.Column("to_item", sa.BigInteger(), nullable=False),
        _key(name, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(name, "from_item", "srs_items", ondelete="CASCADE"),
        _key(name, "to_item", "srs_items", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("version_id", "from_item", "rel", "to_item", name="srs_refs_pkey"),
        sa.CheckConstraint(
            sa.and_(col("rel").in_(REF_RELS), col("from_item") != col("to_item")), name="srs_refs_check"
        ),
    )


def _reviews() -> None:
    name = "srs_reviews"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("reviewer_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key(name, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(name, "reviewer_id", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="srs_reviews_pkey"),
        sa.UniqueConstraint("version_id", "round", "reviewer_id", name="srs_reviews_reviewer_key"),
        sa.CheckConstraint(
            sa.and_(
                col("round") >= 1,
                col("role").in_(REVIEW_ROLES),
                col("decision").in_(REVIEW_DECISIONS),
                sa.func.char_length(col("body")) <= MAX_REVIEW_CHARS,
            ),
            name="srs_reviews_check",
        ),
    )


def _comments() -> None:
    name = "srs_comments"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=True),
        sa.Column("item_id", sa.BigInteger(), nullable=True),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        _key(name, "document_id", "srs_documents", ondelete="CASCADE"),
        _key(name, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(name, "item_id", "srs_items", ondelete="CASCADE"),
        _key(name, "parent_id", "srs_comments", ondelete="CASCADE"),
        _key(name, "author_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "resolved_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="srs_comments_pkey"),
        sa.CheckConstraint(
            sa.and_(
                _length("body", 1, MAX_COMMENT_CHARS),
                col("resolved_by").is_(None) == col("resolved_at").is_(None),
            ),
            name="srs_comments_check",
        ),
    )
    op.create_index("srs_comments_document_idx", name, ["document_id", "id"])


def upgrade() -> None:
    _documents()
    _texts()
    _versions()
    _items()
    _codes()
    _item_versions()
    _refs()
    _reviews()
    _comments()


def downgrade() -> None:
    op.drop_index("srs_comments_document_idx", table_name="srs_comments")
    op.drop_index("srs_item_versions_item_idx", table_name="srs_item_versions")
    op.drop_index("srs_codes_item_idx", table_name="srs_codes")
    for name in TABLES:
        op.drop_table(name)
