"""The vectors a project's builds embedded, by model and digest of their text: kg_vector_cache.

Revision ID: 0254
Revises: 0253
Create Date: 2026-11-16

Plan kg-centralized-on-hub, step 3: a hub build keeps the cache of its embeddings in the hub's database instead of the
vectors.sqlite of the project's directory on the worker (``evo_agents.hub.kg_semantic.HubCache``). The table starts
empty: the first build of a project that asks for vectors takes the ones its vectors.sqlite and its last graph hold,
so nothing is embedded again. Going back drops the table; the next build reads vectors.sqlite as before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0254"
down_revision = "0253"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kg_vector_cache",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_vector_cache_project_id_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "model", "digest", name="kg_vector_cache_pkey"),
    )


def downgrade() -> None:
    op.drop_table("kg_vector_cache")
