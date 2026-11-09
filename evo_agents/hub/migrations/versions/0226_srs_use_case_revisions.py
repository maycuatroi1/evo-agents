"""The revisions of each use case of a BA document and its approved baseline: srs_use_case_revisions and
srs_use_case_releases.

Revision ID: 0226
Revises: 0225
Create Date: 2026-11-09

The names below are copies of ``evo_agents.hub.tables_srs`` as of this revision, never imports, so a later change to
it takes a new revision. The reading of a use case (``fingerprint``) is a copy of
``evo_agents.hub.server.srs.use_case_reviews.fingerprints`` as of this revision, hashed with SHA-256.

srs_use_case_revisions holds each revision of a use case, r1 and up: one more each time a saved version changes how the
use case reads (its title, code, fields and body, and the business rules under it with theirs; not its place), with the
version it first appears in and that version's author. A version that drops a use case gives it one last revision with
no fingerprint. A writer may send a revision to review on its own: when, by whom, its round and @ba's review of that
round. Filled here from every version saved so far, in the order of their numbers.

srs_use_case_releases holds each approved revision of a use case: the newest of each use case is the document's approved
baseline, kept apart from the chain of versions. Filled here from the version each document approved last: every use
case of it, at its revision there, as released by whoever approved that version (as its event srs.version.approved
says) when it was approved; the tasks of those use cases were made then, so ``tasks`` is set.

Going back drops both tables.
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0226"
down_revision = "0225"
branch_labels = None
depends_on = None

REVISIONS, RELEASES = "srs_use_case_revisions", "srs_use_case_releases"
USE_CASE, BUSINESS_RULE = "use_case", "business_rule"
RELEASE_CHANGES = ("added", "changed", "removed")

col = sa.column


def _key(table: str, column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _own(row: dict) -> list:
    return [row["title"], row["code"], row["body"], row["fields"]]


def fingerprints(rows: list[dict]) -> dict[int, str]:
    """item id -> the SHA-256 of how each use case of one version reads, ``rows`` being its items in order (item_id,
    key, kind, code, parent_item_id, title, fields, body): its own title, code, body and fields, and the business rules
    whose nearest use case above is it, each with its key and theirs, sorted."""
    by_id = {row["item_id"]: row for row in rows}
    rules: dict[str, list] = {}
    for row in rows:
        if row["kind"] != BUSINESS_RULE:
            continue
        at = row["parent_item_id"]
        while at is not None and at in by_id:
            if by_id[at]["kind"] == USE_CASE:
                rules.setdefault(by_id[at]["key"], []).append([row["key"], _own(row)])
                break
            at = by_id[at]["parent_item_id"]
    found = {}
    for row in rows:
        if row["kind"] == USE_CASE and row["key"]:
            text = json.dumps([_own(row), sorted(rules.get(row["key"], []))], sort_keys=True, default=str)
            found[row["item_id"]] = hashlib.sha256(text.encode()).hexdigest()
    return found


def _items(conn, version_id: int) -> list[dict]:
    query = sa.text(
        "SELECT iv.item_id, i.key, i.kind, i.code, iv.parent_item_id, iv.title, iv.fields, t.body "
        "FROM srs_item_versions iv JOIN srs_items i ON i.id = iv.item_id "
        "JOIN srs_texts t ON t.sha256 = iv.body_sha256 WHERE iv.version_id = :version ORDER BY iv.position"
    )
    return [dict(row._mapping) for row in conn.execute(query, {"version": version_id})]


def backfill_revisions(conn) -> int:
    """Write the revisions of every use case of every document from its versions, in order; how many were written."""
    versions = conn.execute(
        sa.text("SELECT id, document_id, number, author_id, created_at FROM srs_versions ORDER BY document_id, number")
    )
    written = 0
    latest: dict[int, tuple[int, str | None]] = {}  # item id -> (rev, fingerprint) of its newest revision
    document = None
    for version in list(versions):
        if version.document_id != document:
            document, latest = version.document_id, {}
        now = fingerprints(_items(conn, version.id))
        rows = []
        for item_id, print_ in now.items():
            rev, before = latest.get(item_id, (0, None))
            if rev == 0 or before != print_:
                rows.append((item_id, rev + 1, print_))
        for item_id, (rev, before) in latest.items():
            if before is not None and item_id not in now:  # the version dropped it
                rows.append((item_id, rev + 1, None))
        for item_id, rev, print_ in rows:
            latest[item_id] = (rev, print_)
            conn.execute(
                sa.text(
                    f"INSERT INTO {REVISIONS} (document_id, item_id, rev, version_id, fingerprint, author_id, "
                    "created_at) VALUES (:document, :item, :rev, :version, :print, :author, :at)"
                ),
                {
                    "document": version.document_id,
                    "item": item_id,
                    "rev": rev,
                    "version": version.id,
                    "print": print_,
                    "author": version.author_id,
                    "at": version.created_at,
                },
            )
            written += 1
    return written


def backfill_releases(conn) -> int:
    """Release every use case of the version each document approved last, at its revision there; how many."""
    approved = conn.execute(
        sa.text(
            "SELECT d.id AS document_id, v.id AS version_id, v.number, COALESCE(v.decided_at, v.created_at) AS at "
            "FROM srs_documents d JOIN srs_versions v ON v.document_id = d.id AND v.number = d.approved_version"
        )
    )
    written = 0
    for row in list(approved):
        approver = conn.execute(
            sa.text(
                "SELECT u.id FROM hub_events e JOIN users u ON u.id = (e.payload->>'approver_id')::bigint "
                "WHERE e.kind = 'srs.version.approved' AND e.payload->>'document_id' = :document "
                "AND e.payload->>'version' = :number AND e.payload->>'approver_id' ~ '^[0-9]+$' "
                "ORDER BY e.id DESC LIMIT 1"
            ),
            {"document": str(row.document_id), "number": str(row.number)},
        ).scalar()
        cases = conn.execute(
            sa.text(
                f"SELECT DISTINCT ON (r.item_id) r.item_id, r.rev, r.fingerprint FROM {REVISIONS} r "
                "JOIN srs_versions v ON v.id = r.version_id "
                "WHERE r.document_id = :document AND v.number <= :number ORDER BY r.item_id, r.rev DESC"
            ),
            {"document": row.document_id, "number": row.number},
        )
        for case in list(cases):
            if case.fingerprint is None:  # dropped by then
                continue
            conn.execute(
                sa.text(
                    f"INSERT INTO {RELEASES} (document_id, item_id, rev, version_id, change, released_by, "
                    "released_at, tasks) VALUES (:document, :item, :rev, :version, 'added', :by, :at, '{}'::jsonb)"
                ),
                {
                    "document": row.document_id,
                    "item": case.item_id,
                    "rev": case.rev,
                    "version": row.version_id,
                    "by": approver,
                    "at": row.at,
                },
            )
            written += 1
    return written


def upgrade() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_by", sa.BigInteger(), nullable=True),
        sa.Column("round", sa.Integer(), server_default="0", nullable=False),
        sa.Column("review_run_id", sa.BigInteger(), nullable=True),
        _key(REVISIONS, "document_id", "srs_documents", ondelete="CASCADE"),
        _key(REVISIONS, "item_id", "srs_items", ondelete="CASCADE"),
        _key(REVISIONS, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(REVISIONS, "author_id", "users", ondelete="RESTRICT"),
        _key(REVISIONS, "submitted_by", "users", ondelete="RESTRICT"),
        _key(REVISIONS, "review_run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{REVISIONS}_pkey"),
        sa.UniqueConstraint("item_id", "rev", name=f"{REVISIONS}_rev_key"),
        sa.CheckConstraint(sa.and_(col("rev") >= 1, col("round") >= 0), name=f"{REVISIONS}_check"),
    )
    op.create_index(f"{REVISIONS}_document_idx", REVISIONS, ["document_id", "item_id", "rev"])
    op.create_index(f"{REVISIONS}_review_run_idx", REVISIONS, ["review_run_id"])
    op.create_table(
        RELEASES,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("change", sa.Text(), nullable=False),
        sa.Column("released_by", sa.BigInteger(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("tasks", postgresql.JSONB(), nullable=True),
        _key(RELEASES, "document_id", "srs_documents", ondelete="CASCADE"),
        _key(RELEASES, "item_id", "srs_items", ondelete="CASCADE"),
        _key(RELEASES, "version_id", "srs_versions", ondelete="CASCADE"),
        _key(RELEASES, "released_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{RELEASES}_pkey"),
        sa.UniqueConstraint("item_id", "rev", name=f"{RELEASES}_rev_key"),
        sa.CheckConstraint(col("change").in_(RELEASE_CHANGES), name=f"{RELEASES}_check"),
    )
    op.create_index(f"{RELEASES}_document_idx", RELEASES, ["document_id", "item_id", "id"])
    conn = op.get_bind()
    backfill_revisions(conn)
    backfill_releases(conn)


def downgrade() -> None:
    op.drop_table(RELEASES)
    op.drop_table(REVISIONS)
