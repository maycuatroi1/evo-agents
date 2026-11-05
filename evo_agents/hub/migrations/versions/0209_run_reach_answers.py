"""Run reach: answers joins the groups a member's runs that run as them may reach.

Revision ID: 0209
Revises: 0208
Create Date: 2026-11-05

The check of users.run_reach takes answers besides agents, workers, curator and releases: a run's token (``evr_...``)
of a member who opened it asks an answer job of a project's graph (POST /v1/kg/{project}/answers,
``run_access_rules.ANSWERS``), which ``kg_answers.ask`` refused every run's token. No row changes: everyone keeps what
they chose, and nobody reaches answers until they open it with PUT /v1/autonomy/run-reach.

Going back takes answers out of every member's reach and restores the check of 0208.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0209"
down_revision = "0208"
branch_labels = None
depends_on = None

CHECK = "users_run_reach_check"
BEFORE = ["agents", "workers", "curator", "releases"]  # the groups of 0208
GROUPS = [*BEFORE, "answers"]  # run_access_rules.REACH_GROUPS when this revision was written
col = sa.column


def _check(groups: list[str]) -> None:
    op.create_check_constraint(CHECK, "users", col("run_reach").op("<@")(postgresql.array(groups, type_=sa.Text())))


def upgrade() -> None:
    op.drop_constraint(CHECK, "users", type_="check")
    _check(GROUPS)


def downgrade() -> None:
    op.drop_constraint(CHECK, "users", type_="check")
    users = sa.table("users", col("run_reach", postgresql.ARRAY(sa.Text())))
    reach = users.c.run_reach
    op.execute(
        users.update()
        .where(sa.literal("answers") == sa.any_(reach))
        .values(run_reach=sa.func.array_remove(reach, "answers"))
    )
    _check(BEFORE)
