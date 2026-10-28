"""The retrieval question sets of the knowledge graph.

Revision ID: 0179
Revises: 0178
Create Date: 2026-10-28

kg_eval_sets is new (plan kg-retrieval-quality, criterion a2): each version of a project's question set for
``evo-agents kg eval`` (``evo_agents.kg.evaluate``), put with ``evo-agents hub kg eval-set push`` and read by the
machines (``pull``) and by the hub's evaluation of its graph (``evo_agents.hub.server.kg_health``): the set's name, the
questions as JSON objects, the digest of what they ask and answer, who put the version and when. A version is never changed; a set
put again with other questions is the next version. It starts empty.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0179"
down_revision = "0178"
branch_labels = None
depends_on = None

TABLE = "kg_eval_sets"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{TABLE}_project_id_fkey"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL", name=f"{TABLE}_created_by_fkey"),
        sa.PrimaryKeyConstraint("project_id", "version", name=f"{TABLE}_pkey"),
        sa.CheckConstraint("version >= 1", name=f"{TABLE}_version_check"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
