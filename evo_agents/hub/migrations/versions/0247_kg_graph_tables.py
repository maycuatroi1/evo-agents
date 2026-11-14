"""The knowledge graph on Postgres: corpus items, memo, nodes, edges, text rows, provenance, asked rows, embeddings,
heads, branches.

Revision ID: 0247
Revises: 0246
Create Date: 2026-11-15

Plan kg-centralized-on-hub, step 2 (criterion a3, ``evo_agents.hub.tables_kg.graph_tables``). A build writes a
project's graph to these tables instead of a graph.sqlite blob: kg_corpus_items holds each item of its sources with its
row hash, kg_memo_entries what an extractor made of a row hash, kg_nodes, kg_edges, kg_text_rows, kg_provenance (units,
where-provenance and derivations), kg_asked (what agents asked before they read a node, a store's node_asked) and
kg_embeddings (the rows of a store's node_vec) the graph of each branch (branch_id 0 is main, another a kg_branches id),
and kg_graph_heads the build, content hash, summary, vector model and weight of an asked match each branch is at. kg_branches and kg_branch_ops hold the draft branches of a graph and their operations. The tables
start empty: the next build of a project fills them.

Going back drops them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0247"
down_revision = "0246"
branch_labels = None
depends_on = None

col = sa.column


def _now(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _labels() -> list[sa.Column]:
    return [
        sa.Column("label_lvl", sa.Integer()),
        sa.Column("label_loc", sa.Integer()),
        sa.Column("label_int", sa.Text()),
        sa.Column("label_aud", sa.Text()),
    ]


def _scoped(name: str, key: str, *columns: sa.Column) -> None:
    op.create_table(
        name,
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("branch_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(key, sa.Text(), nullable=False),
        *columns,
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        sa.PrimaryKeyConstraint("project_id", "branch_id", key, name=f"{name}_pkey"),
    )


def _graph() -> None:
    _scoped(
        "kg_corpus_items",
        "item_key",
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("row_hash", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("label", JSONB(), nullable=False),
        sa.Column("audience", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("body", JSONB(), nullable=False),
        _now("updated_at"),
    )
    op.create_table(
        "kg_memo_entries",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("extractor", sa.Text(), nullable=False),
        sa.Column("row_hash", sa.Text(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        _now("created_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_memo_entries_project_id_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "extractor", "row_hash", name="kg_memo_entries_pkey"),
    )
    _scoped(
        "kg_nodes",
        "node_id",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("props", sa.Text(), nullable=False),
        sa.Column("status", sa.Text()),
        sa.Column("conf", sa.Double()),
        *_labels(),
        sa.Column("aliases", sa.Text(), nullable=False, server_default="[]"),
    )
    _scoped(
        "kg_edges",
        "edge_id",
        sa.Column("src", sa.Text(), nullable=False),
        sa.Column("rel", sa.Text(), nullable=False),
        sa.Column("dst", sa.Text(), nullable=False),
        sa.Column("props", sa.Text(), nullable=False),
        sa.Column("status", sa.Text()),
        sa.Column("conf", sa.Double()),
        *_labels(),
    )
    op.create_index("kg_edges_src_idx", "kg_edges", ["project_id", "branch_id", "src"])
    op.create_index("kg_edges_dst_idx", "kg_edges", ["project_id", "branch_id", "dst"])
    _scoped(
        "kg_text_rows",
        "row_key",
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("row", JSONB(), nullable=False),
    )
    _scoped(
        "kg_provenance",
        "row_key",
        sa.Column("elem_id", sa.Text(), nullable=False),
        sa.Column("row", JSONB(), nullable=False),
    )
    _scoped(
        "kg_asked",
        "row_key",
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("row", JSONB(), nullable=False),
    )
    _scoped(
        "kg_embeddings",
        "row_key",
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("aud", sa.Text()),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
    )


def _heads_and_branches() -> None:
    op.create_table(
        "kg_graph_heads",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("branch_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("build_id", sa.BigInteger()),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("top_label", ARRAY(sa.Integer()), nullable=False),
        sa.Column("top_audience", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("summary", JSONB(), nullable=False, server_default="{}"),
        sa.Column("vectors", JSONB()),
        sa.Column("asked_weight", sa.Double()),
        _now("updated_at"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_graph_heads_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["build_id"], ["kg_builds.id"], ondelete="SET NULL", name="kg_graph_heads_build_id_fkey"
        ),
        sa.PrimaryKeyConstraint("project_id", "branch_id", name="kg_graph_heads_pkey"),
    )
    op.create_table(
        "kg_branches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_build_id", sa.BigInteger()),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("created_by", sa.BigInteger()),
        _now("created_at"),
        sa.CheckConstraint(col("status").in_(("open", "merged", "closed")), name="kg_branches_status_check"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="kg_branches_project_id_fkey"
        ),
        sa.ForeignKeyConstraint(
            ["base_build_id"], ["kg_builds.id"], ondelete="SET NULL", name="kg_branches_base_build_id_fkey"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL", name="kg_branches_created_by_fkey"),
        sa.PrimaryKeyConstraint("id", name="kg_branches_pkey"),
        sa.UniqueConstraint("project_id", "name", name="kg_branches_project_id_name_key"),
    )
    op.create_table(
        "kg_branch_ops",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("branch_id", sa.BigInteger(), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("by_user", sa.BigInteger()),
        _now("at"),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["kg_branches.id"], ondelete="CASCADE", name="kg_branch_ops_branch_id_fkey"
        ),
        sa.ForeignKeyConstraint(["by_user"], ["users.id"], ondelete="SET NULL", name="kg_branch_ops_by_user_fkey"),
        sa.PrimaryKeyConstraint("id", name="kg_branch_ops_pkey"),
    )
    op.create_index("kg_branch_ops_branch_idx", "kg_branch_ops", ["branch_id", "id"])


def upgrade() -> None:
    _graph()
    _heads_and_branches()


def downgrade() -> None:
    for name in (
        "kg_branch_ops",
        "kg_branches",
        "kg_graph_heads",
        "kg_embeddings",
        "kg_asked",
        "kg_provenance",
        "kg_text_rows",
        "kg_edges",
        "kg_nodes",
        "kg_memo_entries",
        "kg_corpus_items",
    ):
        op.drop_table(name)
