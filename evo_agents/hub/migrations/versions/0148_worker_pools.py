"""Worker pools and their join tokens.

Revision ID: 0148
Revises: 0147
Create Date: 2026-10-25

worker_pools holds each pool a member made (plan worker-pools, criterion a1): its owner, its name (unique among the
owner's pools in any case), the slots (1 to MAX_SLOTS), labels and taints every node takes, the codes of doctor
findings it accepts, the days a node stays offline before the hub revokes it (1 to 90, 7 by default), and when it was
made and last changed. worker_pool_projects lists the projects its nodes serve; a pool's rows go with it, and with the
project. worker_join_tokens holds the join tokens of a pool: the hex SHA-256 of the token alone, who made it, when it
expires, how many machines it takes (1 to MAX_USES) and how many joined with it, never more, and when it was revoked.
workers.pool_id names the pool a node joined, NULL for a worker paired or registered; a pool with nodes is not deleted
under them. The bounds below are copies of ``evo_agents.hub.runs.MAX_SLOTS`` and of
``evo_agents.hub.server.worker_pool_models`` as of this revision, never imports, so a later change takes a new
revision. Only tables and a column are added.

Going back drops the column and the three tables, which is the schema 0147 runs on: its hub has no pools, and the
nodes that joined one stay its workers, without their pool.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None

POOLS, PROJECTS, TOKENS = "worker_pools", "worker_pool_projects", "worker_join_tokens"
MAX_SLOTS = 16  # evo_agents.hub.runs.MAX_SLOTS
MAX_REAP_DAYS = 90
MAX_USES = 100

col = sa.column


def _texts(name: str) -> sa.Column:
    return sa.Column(name, postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False)


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _key(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        POOLS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slots", sa.SmallInteger(), server_default="1", nullable=False),
        _texts("labels"),
        _texts("taints"),
        _texts("accepted_findings"),
        sa.Column("reap_after_days", sa.SmallInteger(), server_default="7", nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        _key(POOLS, "owner_id", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{POOLS}_pkey"),
        sa.CheckConstraint(col("slots").between(1, MAX_SLOTS), name=f"{POOLS}_slots_check"),
        sa.CheckConstraint(col("reap_after_days").between(1, MAX_REAP_DAYS), name=f"{POOLS}_reap_after_days_check"),
        sa.CheckConstraint(sa.func.char_length(col("name")).between(1, 40), name=f"{POOLS}_name_check"),
    )
    op.create_index(f"{POOLS}_name_key", POOLS, ["owner_id", sa.func.lower(col("name"))], unique=True)
    op.create_table(
        PROJECTS,
        sa.Column("pool_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        _key(PROJECTS, "pool_id", POOLS, "CASCADE"),
        _key(PROJECTS, "project_id", "projects", "CASCADE"),
        sa.PrimaryKeyConstraint("pool_id", "project_id", name=f"{PROJECTS}_pkey"),
    )
    op.create_table(
        TOKENS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("pool_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.SmallInteger(), nullable=False),
        sa.Column("uses", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _key(TOKENS, "pool_id", POOLS, "CASCADE"),
        _key(TOKENS, "created_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{TOKENS}_pkey"),
        sa.UniqueConstraint("token_hash", name=f"{TOKENS}_token_hash_key"),
        sa.CheckConstraint(col("max_uses").between(1, MAX_USES), name=f"{TOKENS}_max_uses_check"),
        sa.CheckConstraint(sa.and_(col("uses") >= 0, col("uses") <= col("max_uses")), name=f"{TOKENS}_uses_check"),
        sa.CheckConstraint(col("expires_at") > col("created_at"), name=f"{TOKENS}_expires_check"),
    )
    op.create_index(f"{TOKENS}_pool_idx", TOKENS, ["pool_id"])
    op.add_column("workers", sa.Column("pool_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("workers_pool_id_fkey", "workers", POOLS, ["pool_id"], ["id"], ondelete="RESTRICT")
    op.create_index("workers_pool_idx", "workers", ["pool_id"], postgresql_where=col("pool_id").is_not(None))


def downgrade() -> None:
    op.drop_index("workers_pool_idx", table_name="workers")
    op.drop_constraint("workers_pool_id_fkey", "workers", type_="foreignkey")
    op.drop_column("workers", "pool_id")
    op.drop_index(f"{TOKENS}_pool_idx", table_name=TOKENS)
    op.drop_table(TOKENS)
    op.drop_table(PROJECTS)
    op.drop_index(f"{POOLS}_name_key", table_name=POOLS)
    op.drop_table(POOLS)
