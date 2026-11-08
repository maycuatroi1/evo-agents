"""Run reach: runs joins the groups a member's runs that run as them may reach.

Revision ID: 0223
Revises: 0222
Create Date: 2026-11-08

The check of users.run_reach takes runs besides agents, workers, curator, releases and answers: a run's token
(``evr_...``) of a member who opened it starts runs, messages them and hands agents work (a mention in a chat, a core
plugin's task), as ``run_access_rules.RUNS`` says, which every run's token was refused. The runs it starts never run as
the member (``run_access.refuse_onward``), so the owner's account goes no further than the run they let run as them. No
row changes: everyone keeps what they chose, and nobody reaches runs until they open it with PUT /v1/autonomy/run-reach.

Going back takes runs out of every member's reach and restores the check of 0209.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0223"
down_revision = "0222"
branch_labels = None
depends_on = None

CHECK = "users_run_reach_check"
BEFORE = ["agents", "workers", "curator", "releases", "answers"]  # the groups of 0209
GROUPS = [*BEFORE, "runs"]  # run_access_rules.REACH_GROUPS when this revision was written
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
        users.update().where(sa.literal("runs") == sa.any_(reach)).values(run_reach=sa.func.array_remove(reach, "runs"))
    )
    _check(BEFORE)
