"""The cost floor of an agent type's run: project_agent_types.min_run_usd.

Revision ID: 0260
Revises: 0259
Create Date: 2026-11-19

Plan task-run-cost-floor, step 1. The hub handed task runs a cap of what was left of their type's daily_usd, however
small, so a run could start with a cap of a cent and die in seconds. project_agent_types gains min_run_usd, the least
cost cap the hub hands a task run or a question run of the type (``evo_agents.hub.task_runs``): it refuses the run
when less than that is left. NULL, the value of every type enabled before this revision, means the hub's default.
A set floor is above 0. Going back drops the column with its check.
"""

import sqlalchemy as sa
from alembic import op

revision = "0260"
down_revision = "0259"
branch_labels = None
depends_on = None

CHECK = "project_agent_types_min_run_usd_check"


def upgrade() -> None:
    op.add_column("project_agent_types", sa.Column("min_run_usd", sa.Double(), nullable=True))
    floor = sa.column("min_run_usd")
    op.create_check_constraint(CHECK, "project_agent_types", sa.or_(floor.is_(None), floor > 0))


def downgrade() -> None:
    op.drop_constraint(CHECK, "project_agent_types", type_="check")
    op.drop_column("project_agent_types", "min_run_usd")
