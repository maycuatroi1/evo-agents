"""A pool of each agent, and the placement of each agent.

Revision ID: 0189
Revises: 0188
Create Date: 2026-11-01

worker_pools.agent_id is new (plan agent-dedicated-pools, criterion a1): the agent (``agent_profiles.id``) whose pool
it is, NULL for a pool a member made. At most one pool names an agent (``worker_pools_agent_key``). The pool of an
agent is named ``agent-<handle>``, so the name check takes up to 106 characters for it (a handle has up to 100), and
the member's keep 40; it is owned by the member who created the agent and serves the agent's project. This revision
gives every agent that is not deleted its pool, and renames a member's pool whose name starts with ``agent-`` to
``pool-`` and the rest (cut to 40 characters), since ``agent-`` is the prefix of the agents' pools from now on.

agent_profiles.placement is new: ``legacy`` (a run of the agent is placed as before) or ``pool`` (it goes to a node
of the agent's own pool). The agents that exist stay ``legacy``; the hub makes an agent it creates from now on
``pool``. The column is a bridge, which the step that moves every agent to ``pool`` removes. Only columns and
indexes are added, and the rows above.

Going back drops the pools of agents (their join tokens and project rows go with them, their nodes become workers
without a pool), then the column and the constraint of ``agent_id``, and restores the name check of 40. The members'
pools renamed above keep their new names.
"""

import sqlalchemy as sa
from alembic import op

revision = "0189"
down_revision = "0188"
branch_labels = None
depends_on = None

POOLS, PROFILES = "worker_pools", "agent_profiles"
NAME_CHECK = f"{POOLS}_name_check"
PLACEMENT_CHECK = f"{PROFILES}_placement_check"
col = sa.column
pools = sa.table(POOLS, col("id"), col("owner_id"), col("name", sa.Text()), col("agent_id"))
profiles = sa.table(
    PROFILES, col("id"), col("project_id"), col("handle", sa.Text()), col("created_by"), col("deleted_at")
)
served_by = sa.table("worker_pool_projects", col("pool_id"), col("project_id"))
workers = sa.table("workers", col("pool_id"))


def upgrade() -> None:
    op.add_column(POOLS, sa.Column("agent_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(f"{POOLS}_agent_id_fkey", POOLS, PROFILES, ["agent_id"], ["id"], ondelete="RESTRICT")
    op.create_index(
        f"{POOLS}_agent_key", POOLS, ["agent_id"], unique=True, postgresql_where=col("agent_id").is_not(None)
    )
    op.drop_constraint(NAME_CHECK, POOLS, type_="check")
    length = sa.func.char_length(col("name"))
    op.create_check_constraint(
        NAME_CHECK,
        POOLS,
        sa.or_(
            sa.and_(col("agent_id").is_(None), length.between(1, 40)),
            sa.and_(col("agent_id").is_not(None), length.between(7, 106)),
        ),
    )
    op.add_column(PROFILES, sa.Column("placement", sa.Text(), server_default="legacy", nullable=False))
    op.create_check_constraint(PLACEMENT_CHECK, PROFILES, col("placement").in_(["legacy", "pool"]))
    renamed = sa.func.left("pool-" + sa.func.substr(pools.c.name, 7), 40)
    members = sa.and_(sa.func.lower(pools.c.name).like("agent-%"), pools.c.agent_id.is_(None))
    op.execute(sa.update(pools).where(members).values(name=renamed))
    live = profiles.c.deleted_at.is_(None)
    made = sa.select(profiles.c.created_by, "agent-" + profiles.c.handle, profiles.c.id).where(live)
    op.execute(sa.insert(pools).from_select(["owner_id", "name", "agent_id"], made))
    served = sa.select(pools.c.id, profiles.c.project_id).join_from(pools, profiles, profiles.c.id == pools.c.agent_id)
    op.execute(sa.insert(served_by).from_select(["pool_id", "project_id"], served))


def downgrade() -> None:
    agents_pools = sa.select(pools.c.id).where(pools.c.agent_id.is_not(None))
    op.execute(sa.update(workers).where(workers.c.pool_id.in_(agents_pools)).values(pool_id=None))
    op.execute(sa.delete(pools).where(pools.c.agent_id.is_not(None)))
    op.drop_constraint(PLACEMENT_CHECK, PROFILES, type_="check")
    op.drop_column(PROFILES, "placement")
    op.drop_constraint(NAME_CHECK, POOLS, type_="check")
    op.create_check_constraint(NAME_CHECK, POOLS, sa.func.char_length(col("name")).between(1, 40))
    op.drop_index(f"{POOLS}_agent_key", table_name=POOLS)
    op.drop_constraint(f"{POOLS}_agent_id_fkey", POOLS, type_="foreignkey")
    op.drop_column(POOLS, "agent_id")
