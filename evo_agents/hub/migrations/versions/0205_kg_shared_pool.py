"""The shared KG pool of the hub.

Revision ID: 0205
Revises: 0204
Create Date: 2026-11-05

worker_pools.kg_shared marks the one pool of a member that a hub admin named the shared KG pool (plan
kg-zip-to-answers, step 1, ``evo_agents.hub.server.kg_shared_pool``): its ready nodes take the KG review runs of a
project fed by uploads alone, whose default agent's pool has no ready node, when the project's creator owns the pool.
At most one pool is marked (worker_pools_kg_shared_key), and never an agent's (worker_pools_kg_shared_check).

Going back drops the column: those runs wait for a node of the default agent's pool again.
"""

import sqlalchemy as sa
from alembic import op

revision = "0205"
down_revision = "0204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_pools", sa.Column("kg_shared", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint(
        "worker_pools_kg_shared_check",
        "worker_pools",
        sa.or_(~sa.column("kg_shared"), sa.column("agent_id").is_(None)),
    )
    op.create_index(
        "worker_pools_kg_shared_key",
        "worker_pools",
        ["kg_shared"],
        unique=True,
        postgresql_where=sa.column("kg_shared"),
    )


def downgrade() -> None:
    op.drop_index("worker_pools_kg_shared_key", table_name="worker_pools")
    op.drop_constraint("worker_pools_kg_shared_check", "worker_pools", type_="check")
    op.drop_column("worker_pools", "kg_shared")
