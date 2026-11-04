"""The KG settings of a project and the history of their changes.

Revision ID: 0196
Revises: 0195
Create Date: 2026-11-04

kg_settings is new (plan kg-data-profile-curator, criterion a4): one row a project holding the settings that differ
from the defaults (``evo_agents.kg.settings``: the weight of the ranking by meaning, the BM25 column weights, the
length from which a body is cut into passages and the passage length, the characters embedded, the strategy). A
project without a row is tuned by the defaults, and the table starts empty, so every project ranks as before. The
semantic mode and model stay in kg_semantic.

kg_settings_changes is the history: each change with who made it (a member, or the run of a Curator agent), when, why,
the figures it cites, the values before and after, and for an undo the change it undid.

Only tables are added. Going back drops both.
"""

import sqlalchemy as sa
from alembic import op

revision = "0196"
down_revision = "0195"
branch_labels = None
depends_on = None

SETTINGS = "kg_settings"
CHANGES = "kg_settings_changes"
KINDS = ("set", "undo")  # a copy of evo_agents.hub.tables_kg.SETTINGS_KINDS, never an import


def upgrade() -> None:
    op.create_table(
        SETTINGS,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("overrides", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_run_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{SETTINGS}_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL", name=f"{SETTINGS}_updated_by_fkey"),
        sa.ForeignKeyConstraint(
            ["updated_run_id"], ["runs.id"], ondelete="SET NULL", name=f"{SETTINGS}_updated_run_id_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", name=f"{SETTINGS}_pkey"),
    )
    op.create_table(
        CHANGES,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("figures", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("before", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("after", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("undoes", sa.BigInteger(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{CHANGES}_project_id_fkey"),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"], ondelete="SET NULL", name=f"{CHANGES}_changed_by_fkey"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name=f"{CHANGES}_run_id_fkey"),
        sa.ForeignKeyConstraint(["undoes"], [f"{CHANGES}.id"], ondelete="SET NULL", name=f"{CHANGES}_undoes_fkey"),
        sa.PrimaryKeyConstraint("id", name=f"{CHANGES}_pkey"),
        sa.CheckConstraint(sa.column("kind").in_(KINDS), name=f"{CHANGES}_kind_check"),
    )
    op.create_index(f"{CHANGES}_project_idx", CHANGES, ["project_id", "id"])


def downgrade() -> None:
    op.drop_table(CHANGES)
    op.drop_table(SETTINGS)
