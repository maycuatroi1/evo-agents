"""Plan traces: the plan a system was given to run, on the trace its dispatch opened.

Revision ID: 0236
Revises: 0235
Create Date: 2026-11-11

system_traces takes plan_id and plan_revision: the plan, and its revision then, that POST
/v1/projects/{p}/plans/{plan_id}/system-dispatch handed the system's entry agent in the trace's first message
(``evo_agents.hub.server.plan_systems``). NULL: a trace a typed message opened, which runs no plan. While a trace of a
plan has an active run, a plan run or a step run of the plan is refused (``runs.service.views.plan_activity``), so a
partial index finds the traces of a plan. Both columns allow NULL and have no default, and no row is filled in, so the
code before it runs on the schema after it. No foreign key names the plan: a trace outlives its plan, as a run does.

Going back drops the index and the two columns.
"""

import sqlalchemy as sa
from alembic import op

revision = "0236"
down_revision = "0235"
branch_labels = None
depends_on = None

INDEX = "system_traces_plan_idx"


def upgrade() -> None:
    op.add_column("system_traces", sa.Column("plan_id", sa.Text(), nullable=True))
    op.add_column("system_traces", sa.Column("plan_revision", sa.Integer(), nullable=True))
    op.create_index(
        INDEX, "system_traces", ["project_id", "plan_id"], postgresql_where=sa.column("plan_id").is_not(None)
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="system_traces")
    op.drop_column("system_traces", "plan_revision")
    op.drop_column("system_traces", "plan_id")
