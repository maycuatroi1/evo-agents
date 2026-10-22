"""The categories of a decision for each class of action that takes ask, and the person a decision is asked of.

Revision ID: 0108
Revises: 0107
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Each class of action that takes ask has a category of decision of its own (plan autonomy-levels, criterion a12):
decisions.category takes ``merge`` (K5), ``credential`` (K8) and ``access`` (K12), beside deploy (K6), delete_data and
live_migration (K9), external_send (K10) and spend_money (K11), which keep their meaning. decisions gains asked_of,
the person the decision goes to: the person an intern reports to for a run whose principal is an intern, the sponsor
for a run the hub made on its own, the principal for any other; NULL for a decision asked before, which its run's
owner answers as before.

Going back deletes the decisions of the three categories, puts the check of 0107 back and drops the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

CATEGORIES_0107 = (  # decisions_category_check as 0107 set it
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
)
NEW = ("merge", "credential", "access")  # evo_agents.hub.autonomy_card.CLASS_CATEGORIES of K5, K8 and K12
CATEGORIES = (*CATEGORIES_0107, *NEW)  # evo_agents.hub.runs.SHOWN_DECISION_CATEGORIES

col = sa.column


def _check(values) -> None:
    op.drop_constraint("decisions_category_check", "decisions", type_="check")
    op.create_check_constraint("decisions_category_check", "decisions", col("category").in_(values))


def upgrade() -> None:
    _check(CATEGORIES)
    op.add_column("decisions", sa.Column("asked_of", sa.BigInteger(), nullable=True))
    op.create_foreign_key("decisions_asked_of_fkey", "decisions", "users", ["asked_of"], ["id"], ondelete="RESTRICT")


def downgrade() -> None:
    decisions = sa.table("decisions", col("category"))
    op.execute(decisions.delete().where(decisions.c.category.in_(NEW)))
    op.drop_constraint("decisions_asked_of_fkey", "decisions", type_="foreignkey")
    op.drop_column("decisions", "asked_of")
    _check(CATEGORIES_0107)
