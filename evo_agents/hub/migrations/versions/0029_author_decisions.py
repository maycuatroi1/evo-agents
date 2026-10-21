"""The agent of an author run asks its owner decisions too, before its run has a plan.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-13

Schema 0010 made decisions.plan_id NOT NULL: only a plan run, which always has a plan, asked a decision. An author run
asks them as well (``evo_agents.hub.server.decisions``), and one that has put no plan on the hub yet has none, so this
revision lets decisions.plan_id be NULL: the plan the run had when its agent asked, NULL when it had none. A decision
of a plan run keeps its plan as before. The statement is an Alembic operation on SQLAlchemy Core expressions, as
``evo_agents.hub.tables`` describes the table.

Going back deletes every decision whose plan_id is NULL, the questions author runs asked before they put a plan, with
the inbox messages that answered them and the notifications that named them (run_inbox.decision_id and
notifications.decision_id cascade), then makes the column NOT NULL again, which is the schema that schema 0028 runs
on. The decisions author runs asked once they had a plan stay, as schema 0028 can hold them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

col = sa.column


def upgrade() -> None:
    op.alter_column("decisions", "plan_id", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    decisions = sa.table("decisions", col("plan_id"))
    op.execute(decisions.delete().where(decisions.c.plan_id.is_(None)))
    op.alter_column("decisions", "plan_id", existing_type=sa.Text(), nullable=False)
