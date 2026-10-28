"""Whether a project's knowledge graph is searched by meaning too.

Revision ID: 0181
Revises: 0180
Create Date: 2026-10-28

kg_semantic is new (plan kg-retrieval-quality, criterion a12): one row a project, whose mode says what its builds and
kg_search do with the vectors of a multilingual embedding model (``evo_agents.kg.semantic``): ``off``, none; ``index``,
the builds write them, kg_search still ranks by words, so ``kg eval`` compares both on one build; ``on``, kg_search
fuses the ranking by words with the ranking by meaning. A project without a row is off, and the table starts empty, so
every project searches as before. The KG optimize loop sets ``on`` only when the holdout says it helps. The model,
who set the row last and when.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0181"
down_revision = "0180"
branch_labels = None
depends_on = None

TABLE = "kg_semantic"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{TABLE}_project_id_fkey"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL", name=f"{TABLE}_updated_by_fkey"),
        sa.PrimaryKeyConstraint("project_id", name=f"{TABLE}_pkey"),
        sa.CheckConstraint("mode IN ('off', 'index', 'on')", name=f"{TABLE}_mode_check"),
        sa.CheckConstraint("char_length(model) BETWEEN 1 AND 200", name=f"{TABLE}_model_check"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
