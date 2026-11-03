"""A pool for each agent that has none.

Revision ID: 0195
Revises: 0194
Create Date: 2026-11-03

0189 gave every agent of that day its pool, ``agent-<handle>``, and the hub gives one to each agent it creates since,
but the profile the first enable of an agent type creates (``agent_types``) got none until this revision's hub: a
question or task run of the type was refused ("has no pool of its own") and no admin could add the pool. This revision
gives every agent that is not deleted and has no pool its pool, as 0189 did: owned by the member who created the
agent, serving the agent's project. Only rows are added.

Going back leaves the pools: an agent with a pool is what the hub of 0194 expects too.
"""

import sqlalchemy as sa
from alembic import op

revision = "0195"
down_revision = "0194"
branch_labels = None
depends_on = None

col = sa.column
pools = sa.table("worker_pools", col("id"), col("owner_id"), col("name", sa.Text()), col("agent_id"))
profiles = sa.table(
    "agent_profiles", col("id"), col("project_id"), col("handle", sa.Text()), col("created_by"), col("deleted_at")
)
served_by = sa.table("worker_pool_projects", col("pool_id"), col("project_id"))


def upgrade() -> None:
    pooled = sa.select(pools.c.agent_id).where(pools.c.agent_id.is_not(None))
    lacking = sa.and_(profiles.c.deleted_at.is_(None), profiles.c.id.not_in(pooled))
    made = sa.select(profiles.c.created_by, "agent-" + profiles.c.handle, profiles.c.id).where(lacking)
    added = op.get_bind().execute(
        sa.insert(pools).from_select(["owner_id", "name", "agent_id"], made).returning(pools.c.id, pools.c.agent_id)
    )
    for pool_id, agent_id in added.all():
        project = sa.select(profiles.c.project_id).where(profiles.c.id == agent_id).scalar_subquery()
        op.execute(sa.insert(served_by).values(pool_id=pool_id, project_id=project))


def downgrade() -> None:
    pass
