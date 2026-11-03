"""Run reach: the optional groups a member's runs that run as them may reach.

Revision ID: 0193
Revises: 0192
Create Date: 2026-11-03

users.run_reach is new: the groups of ``run_access_rules`` a run's token (``evr_...``) of the member skips, chosen
from agents (a project's agent profiles and agent types), workers (workers, their pools and join tokens) and curator
(the Curator's writes); a check keeps any other name out, and the hub writes each once, in that order. It is empty for
everyone, which keeps a run's token refused there as before; the member sets it with PUT /v1/autonomy/run-reach, which
no run's token reaches.

Going back drops the column.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0193"
down_revision = "0192"
branch_labels = None
depends_on = None

GROUPS = ["agents", "workers", "curator"]  # run_access_rules.REACH_GROUPS when this revision was written
col = sa.column


def upgrade() -> None:
    op.add_column("users", sa.Column("run_reach", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False))
    op.create_check_constraint(
        "users_run_reach_check", "users", col("run_reach").op("<@")(postgresql.array(GROUPS, type_=sa.Text()))
    )


def downgrade() -> None:
    op.drop_constraint("users_run_reach_check", "users", type_="check")
    op.drop_column("users", "run_reach")
