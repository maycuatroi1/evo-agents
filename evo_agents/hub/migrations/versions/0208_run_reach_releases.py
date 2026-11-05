"""Run reach: releases joins the groups a member's runs that run as them may reach.

Revision ID: 0208
Revises: 0207
Create Date: 2026-11-05

The check of users.run_reach takes releases besides agents, workers and curator: a run's token (``evr_...``) of a
member who opened it reaches a project's release schedule, resume and the check now (``run_access_rules.RELEASES``),
which ``releases`` moved out of the group runs every run's token is refused. No row changes: everyone keeps what they
chose, and nobody reaches releases until they open it with PUT /v1/autonomy/run-reach.

Going back takes releases out of every member's reach and restores the check of 0193.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0208"
down_revision = "0207"
branch_labels = None
depends_on = None

CHECK = "users_run_reach_check"
BEFORE = ["agents", "workers", "curator"]  # the groups of 0193
GROUPS = [*BEFORE, "releases"]  # run_access_rules.REACH_GROUPS when this revision was written
col = sa.column


def _check(groups: list[str]) -> None:
    op.create_check_constraint(CHECK, "users", col("run_reach").op("<@")(postgresql.array(groups, type_=sa.Text())))


def upgrade() -> None:
    op.drop_constraint(CHECK, "users", type_="check")
    _check(GROUPS)


def downgrade() -> None:
    op.drop_constraint(CHECK, "users", type_="check")
    op.execute("UPDATE users SET run_reach = array_remove(run_reach, 'releases') WHERE 'releases' = ANY(run_reach)")
    _check(BEFORE)
