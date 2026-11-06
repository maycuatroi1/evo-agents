"""The named graphs of a project: kg_named_graphs.

Revision ID: 0216
Revises: 0215
Create Date: 2026-11-07

A project holds several named knowledge graphs (plan multi-agent-systems, step 1): each a name and a set of the
project's sources (repos, uploads, connectors), one source in as many as it fits. The kg tools read one by narrowing
the project's one shared build to its sources at query time (``evo_agents.kg.graph_scope``), so no other table, key,
store or artifact of the graph changes. ``default``, every source, is implicit and has no row. This revision only adds
the table; a project starts with no named graph.

Going back drops it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0216"
down_revision = "0215"
branch_labels = None
depends_on = None

NAME = "kg_named_graphs"
NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}$"


def upgrade() -> None:
    op.create_table(
        NAME,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sources", JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("updated_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{NAME}_project_id_fkey"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL", name=f"{NAME}_created_by_fkey"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL", name=f"{NAME}_updated_by_fkey"),
        sa.PrimaryKeyConstraint("id", name=f"{NAME}_pkey"),
        sa.UniqueConstraint("project_id", "name", name=f"{NAME}_project_id_name_key"),
        sa.CheckConstraint(f"name ~ '{NAME_PATTERN}' AND name <> 'default'", name=f"{NAME}_name_check"),
        sa.CheckConstraint("char_length(description) <= 500", name=f"{NAME}_description_check"),
        sa.CheckConstraint("jsonb_typeof(sources) = 'array'", name=f"{NAME}_sources_check"),
    )


def downgrade() -> None:
    op.drop_table(NAME)
