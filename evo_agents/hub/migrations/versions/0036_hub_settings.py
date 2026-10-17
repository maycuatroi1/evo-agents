"""Hub settings: the versions of the branding of the hub and of which features are on, for the hub and for a project.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-17

``evo_agents.hub.tables_settings`` describes the table; the lists below are copies of it and of
``evo_agents.hub.server.blobs`` as of this revision, never imports, so a later change to them takes a new revision.
Every statement is an Alembic operation on SQLAlchemy Core expressions.

hub_settings holds every version of each layer of a setting a hub admin changes, never changed: the kind (branding,
the hub's alone; features, the hub's own layer or a project's), the project of a project's layer (NULL for the
hub's; branding has none), the version (1 and up within the layer), the values it sets (an object), the version a
rollback copied (below its own), who wrote it and when. A layer's current version is its highest.

The blob store holds the logo and the favicon of the branding outside any project, as blobs of kind branding-image:
blobs.blobs_hub_kind takes it besides the bundles of skills and plugins.

Going back drops the table and the blobs rows of kind branding-image (the objects stay in the bucket, as an older
release leaves blobs it does not know), which is the schema that schema 0035 runs on.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

KINDS = ("branding", "features")  # tables_settings.KINDS
GLOBAL_KINDS_0035 = ("skill-bundle", "plugin-bundle")
GLOBAL_KINDS = (*GLOBAL_KINDS_0035, "branding-image")  # what the hub holds outside any project

col = sa.column


def _hub_kinds(kinds) -> None:
    op.drop_constraint("blobs_hub_kind", "blobs", type_="check")
    op.create_check_constraint(
        "blobs_hub_kind", "blobs", sa.or_(col("project_id").is_not(None), col("kind").in_(kinds))
    )


def upgrade() -> None:
    op.create_table(
        "hub_settings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("rolled_back_to", sa.Integer(), nullable=True),
        sa.Column("changed_by", sa.BigInteger(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT", name="hub_settings_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"], ondelete="RESTRICT", name="hub_settings_changed_by_fkey"),
        sa.PrimaryKeyConstraint("id", name="hub_settings_pkey"),
        sa.UniqueConstraint(
            "kind", "project_id", "version", name="hub_settings_version_key", postgresql_nulls_not_distinct=True
        ),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(KINDS),
                sa.or_(col("kind") == "features", col("project_id").is_(None)),
                col("version") >= 1,
                sa.or_(col("rolled_back_to").is_(None), col("rolled_back_to").between(1, col("version") - 1)),
                sa.func.jsonb_typeof(col("values")) == "object",
            ),
            name="hub_settings_check",
        ),
    )
    _hub_kinds(GLOBAL_KINDS)


def downgrade() -> None:
    blobs = sa.table("blobs", col("project_id"), col("kind"))
    op.execute(blobs.delete().where(blobs.c.project_id.is_(None), blobs.c.kind == "branding-image"))
    _hub_kinds(GLOBAL_KINDS_0035)
    op.drop_table("hub_settings")
