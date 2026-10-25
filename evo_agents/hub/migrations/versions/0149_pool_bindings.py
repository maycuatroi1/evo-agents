"""A night shift on a pool, and secrets and accounts bound to a pool.

Revision ID: 0149
Revises: 0148
Create Date: 2026-10-25

A charter names the pool its night runs go to, or the worker as before (plan worker-pools, criterion a4): charters
and schedules gain pool_id, worker_id may now be NULL, and a check holds exactly one of the two. The night shift
places the run of a pool's schedule with the selector pool=<name>, so any ready node of the pool claims it.
secret_bindings gains pool_id: a binding to a pool covers each node of it, those that join later too; a check keeps a
binding to a worker or a pool, never both, and the unique key counts the pool. Rows written before this revision keep
their worker and mean what they meant. Only columns and constraints are added.

Going back deletes the charters, schedules and bindings that name a pool, drops the columns and checks, and restores
worker_id NOT NULL and the old unique key: the schema 0148 runs on, whose hub knows no pool of a charter or binding.
"""

import sqlalchemy as sa
from alembic import op

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None

NIGHT = ("charters", "schedules")
BINDINGS = "secret_bindings"
KEY = "secret_bindings_key"

col = sa.column


def _key(*columns) -> None:
    op.drop_index(KEY, table_name=BINDINGS)
    op.create_index(
        KEY, BINDINGS, ["secret_id", "project_id", *(sa.func.coalesce(col(c), 0) for c in columns)], unique=True
    )


def upgrade() -> None:
    for table in NIGHT:
        op.add_column(table, sa.Column("pool_id", sa.BigInteger()))
        op.create_foreign_key(f"{table}_pool_id_fkey", table, "worker_pools", ["pool_id"], ["id"], ondelete="RESTRICT")
        op.alter_column(table, "worker_id", nullable=True)
        op.create_check_constraint(
            f"{table}_worker_or_pool_check", table, col("worker_id").is_(None) != col("pool_id").is_(None)
        )
    op.add_column(BINDINGS, sa.Column("pool_id", sa.BigInteger()))
    op.create_foreign_key(f"{BINDINGS}_pool_id_fkey", BINDINGS, "worker_pools", ["pool_id"], ["id"], ondelete="CASCADE")
    op.create_check_constraint(
        f"{BINDINGS}_worker_or_pool_check", BINDINGS, sa.or_(col("worker_id").is_(None), col("pool_id").is_(None))
    )
    _key("worker_id", "pool_id")
    op.create_index(f"{BINDINGS}_pool_idx", BINDINGS, ["pool_id"], postgresql_where=col("pool_id").is_not(None))


def _drop_pool_rows(name: str) -> None:
    table = sa.table(name, col("pool_id"))
    op.execute(table.delete().where(table.c.pool_id.is_not(None)))


def downgrade() -> None:
    _drop_pool_rows(BINDINGS)
    op.drop_index(f"{BINDINGS}_pool_idx", table_name=BINDINGS)
    _key("worker_id")
    op.drop_constraint(f"{BINDINGS}_worker_or_pool_check", BINDINGS, type_="check")
    op.drop_constraint(f"{BINDINGS}_pool_id_fkey", BINDINGS, type_="foreignkey")
    op.drop_column(BINDINGS, "pool_id")
    for table in reversed(NIGHT):
        _drop_pool_rows(table)
        op.drop_constraint(f"{table}_worker_or_pool_check", table, type_="check")
        op.drop_constraint(f"{table}_pool_id_fkey", table, type_="foreignkey")
        op.drop_column(table, "pool_id")
        op.alter_column(table, "worker_id", nullable=False)
