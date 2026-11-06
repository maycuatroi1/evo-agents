"""A chat run that another agent's run asked for: runs.spawned_by_run_id and runs.spawned_wait_id.

Revision ID: 0214
Revises: 0212
Create Date: 2026-11-06

The agent of a chat run asks an agent of another project with `evo-agents worker agent ask` (plan chat-first-home,
step 2; ``evo_agents.hub.server.agent_asks``). The hub queues the run of the agent asked and names the asking run in
``spawned_by_run_id``, and the wait the asking run parks on in ``spawned_wait_id``: when the run asked ends, the hub
answers that wait with its answer, or with why it failed, and the asking run goes on. Both are NULL for any other run.
A run deleted, or a decision, leaves NULL behind.

Going back drops both columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0214"
down_revision = "0212"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("spawned_by_run_id", sa.BigInteger()))
    op.add_column("runs", sa.Column("spawned_wait_id", sa.BigInteger()))
    op.create_foreign_key(
        "runs_spawned_by_run_id_fkey", "runs", "runs", ["spawned_by_run_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "runs_spawned_wait_id_fkey", "runs", "decisions", ["spawned_wait_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("runs_spawned_wait_id_fkey", "runs", type_="foreignkey")
    op.drop_constraint("runs_spawned_by_run_id_fkey", "runs", type_="foreignkey")
    op.drop_column("runs", "spawned_wait_id")
    op.drop_column("runs", "spawned_by_run_id")
