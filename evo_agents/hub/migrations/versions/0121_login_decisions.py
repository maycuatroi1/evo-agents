"""The decision a run's agent asks to have a browser identity logged in.

Revision ID: 0121
Revises: 0120
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

The agent of a run with a browser that meets a login wall asks its owner to sign in (plan agent-browser, criterion a8):
decisions.category takes ``login``, a decision with the options done and skip. No column of a table changes.

Going back deletes the decisions of category login and puts the check of 0115 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

CATEGORIES_0115 = (  # decisions_category_check as 0115 set it
    "deploy",
    "delete_data",
    "live_migration",
    "external_send",
    "spend_money",
    "architecture",
    "scope",
    "question",
    "triage",
    "release",
    "loop",
    "autonomy",
    "merge",
    "credential",
    "access",
)
CATEGORY = "login"
CATEGORIES = (*CATEGORIES_0115, CATEGORY)  # evo_agents.hub.runs.SHOWN_DECISION_CATEGORIES

col = sa.column


def _check(values) -> None:
    op.drop_constraint("decisions_category_check", "decisions", type_="check")
    op.create_check_constraint("decisions_category_check", "decisions", col("category").in_(values))


def upgrade() -> None:
    _check(CATEGORIES)


def downgrade() -> None:
    decisions = sa.table("decisions", col("category"))
    op.execute(decisions.delete().where(decisions.c.category == CATEGORY))
    _check(CATEGORIES_0115)
