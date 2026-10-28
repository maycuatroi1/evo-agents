"""The labels of the knowledge graph's searches.

Revision ID: 0178
Revises: 0177
Create Date: 2026-10-28

kg_query_labels is new (plan kg-retrieval-quality, criterion a1): one row per kg_search a session or a run made, as its
facet read it (``evo_agents.hub.facets.QueryLabel``): the project, the session or the run (``kind``), its place among
the searches of the facet (``seq``) and where it came, the query, the results with their ranks as printed, the files
of those results the agent read or edited after it (``clicks``), whether a grep, rg or find came right after its chain
of lookups, the files read or edited after that grep, and the sensitivity label of the session or the run, which the
readers check against a sink's clearance. A facet made again replaces the labels of its session or run in the
transaction that writes the facet, whose row lock keeps two writers apart. It starts empty: the facets of FACET_VERSION
3 fill it.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0178"
down_revision = "0177"
branch_labels = None
depends_on = None

TABLE = "kg_query_labels"


def _key(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete="CASCADE", name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("at", sa.Text(), nullable=True),
        sa.Column("searched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("results", postgresql.JSONB(), nullable=False),
        sa.Column("clicks", postgresql.JSONB(), nullable=False),
        sa.Column("grep_after", sa.Boolean(), nullable=False),
        sa.Column("after_grep", postgresql.JSONB(), nullable=False),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("project_id", "projects"),
        _key("run_id", "runs"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.CheckConstraint(sa.column("kind").in_(("session", "run")), name=f"{TABLE}_kind_check"),
        sa.CheckConstraint(
            "(kind = 'session' AND session_id IS NOT NULL AND run_id IS NULL) OR (kind = 'run' AND run_id IS NOT NULL)",
            name=f"{TABLE}_owner_check",
        ),
    )
    op.create_index(f"{TABLE}_session_idx", TABLE, ["project_id", "session_id"])
    op.create_index(f"{TABLE}_run_idx", TABLE, ["run_id"])
    op.create_index(f"{TABLE}_project_idx", TABLE, ["project_id", "searched_at"])


def downgrade() -> None:
    op.drop_table(TABLE)
