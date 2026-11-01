"""Account pools: named sets of accounts that an admin attaches to agents.

Revision ID: 0191
Revises: 0190
Create Date: 2026-11-02

account_pools and account_pool_members are new (plan agent-dedicated-pools, criterion a4). An account pool belongs to
a project and has a name (1 to 40 characters, unique in the project in any case). Its members are secrets of kind env
or provider, whoever owns them: a member's own account that an admin added, or an account of an agent. A secret is in
a pool once. agent_profiles.account_pool_id names the one account pool of an agent, NULL for none; many agents may
name the same pool, and the foreign key restricts deleting a pool in use. A secret row that is removed leaves its pools
(the foreign key cascades); one deleted softly stays listed and is never used (``deleted_at``).

Only tables, one column and indexes are added. Going back drops the column and the two tables.
"""

import sqlalchemy as sa
from alembic import op

revision = "0191"
down_revision = "0190"
branch_labels = None
depends_on = None

POOLS, MEMBERS, PROFILES = "account_pools", "account_pool_members", "agent_profiles"
col = sa.column


def upgrade() -> None:
    op.create_table(
        POOLS,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=f"{POOLS}_project_id_fkey", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=f"{POOLS}_created_by_fkey", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{POOLS}_pkey"),
        sa.CheckConstraint(sa.func.char_length(col("name")).between(1, 40), name=f"{POOLS}_name_check"),
    )
    op.create_index(f"{POOLS}_name_key", POOLS, ["project_id", sa.func.lower(col("name"))], unique=True)
    op.create_table(
        MEMBERS,
        sa.Column("pool_id", sa.BigInteger(), nullable=False),
        sa.Column("secret_id", sa.BigInteger(), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["pool_id"], [f"{POOLS}.id"], name=f"{MEMBERS}_pool_id_fkey", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["secret_id"], ["secrets.id"], name=f"{MEMBERS}_secret_id_fkey", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"], name=f"{MEMBERS}_added_by_fkey", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("pool_id", "secret_id", name=f"{MEMBERS}_pkey"),
    )
    op.create_index(f"{MEMBERS}_secret_idx", MEMBERS, ["secret_id"])
    op.add_column(PROFILES, sa.Column("account_pool_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        f"{PROFILES}_account_pool_id_fkey", PROFILES, POOLS, ["account_pool_id"], ["id"], ondelete="RESTRICT"
    )


def downgrade() -> None:
    op.drop_constraint(f"{PROFILES}_account_pool_id_fkey", PROFILES, type_="foreignkey")
    op.drop_column(PROFILES, "account_pool_id")
    op.drop_index(f"{MEMBERS}_secret_idx", table_name=MEMBERS)
    op.drop_table(MEMBERS)
    op.drop_index(f"{POOLS}_name_key", table_name=POOLS)
    op.drop_table(POOLS)
