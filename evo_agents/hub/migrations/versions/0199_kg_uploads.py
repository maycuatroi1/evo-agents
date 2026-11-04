"""Archives a writer uploads as sources of a project's graph.

Revision ID: 0199
Revises: 0198
Create Date: 2026-11-04

kg_uploads is new (plan kg-upload-archive, step 1): one row for each archive (.zip, .tar, .tar.gz, .tgz) a writer sends
with ``evo-agents hub kg upload`` (``evo_agents.hub.server.kg_uploads``). The bytes go to the blob store as an S3
multipart upload under ``object_key``; the row holds the project, the source the archive becomes, its label (by names,
as the hub stores a pushed run's), who sent it, its name, size, SHA-256 and part size, the store's multipart id while
parts are coming, and its state: uploading, extracting once every part is in, then extracted or failed. ``report`` is
what reading it found (files read, files skipped by reason) and ``build_id`` the build that follows. One upload of the
same archive (project, sender, SHA-256, source) is in flight at a time (``kg_uploads_resume_key``), so a second call
resumes the first. The table starts empty. No other table changes.

The states below are copies of ``evo_agents.hub.tables_kg.UPLOAD_STATES`` as of this revision, never imports, so a later
change to them takes a new revision.

Going back drops the table. The archives it named stay in the blob store under ``kg-archives/``; the cleanup job aborts
their unfinished multipart uploads.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0199"
down_revision = "0198"
branch_labels = None
depends_on = None

TABLE = "kg_uploads"
STATES = ("uploading", "extracting", "extracted", "failed")

col = sa.column


def _key(column: str, target: str, *, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("part_size", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("multipart_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("report", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("build_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _key("project_id", "projects", ondelete="CASCADE"),
        _key("build_id", "kg_builds", ondelete="SET NULL"),
        _key("created_by", "users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.UniqueConstraint("object_key", name=f"{TABLE}_object_key_key"),
        sa.CheckConstraint(col("state").in_(STATES), name=f"{TABLE}_state_check"),
    )
    op.create_index(f"{TABLE}_project_idx", TABLE, ["project_id", "id"])
    op.create_index(
        f"{TABLE}_resume_key",
        TABLE,
        ["project_id", "created_by", "sha256", "source"],
        unique=True,
        postgresql_where=col("state") == "uploading",
    )


def downgrade() -> None:
    op.drop_table(TABLE)
