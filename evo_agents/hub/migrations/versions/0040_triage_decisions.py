"""Decisions of category triage: the question the agent of a chat run asks its owner while it waits.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-18

The check decisions_category_check allows ``triage`` as well, so a chat run can ask a decision. Going back puts the
earlier check back; a decision of category triage that is still there stops it, and is deleted first.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

CATEGORIES_0033 = (
    "deploy",
    "delete_data",
    "live_migration",
    "external_send",
    "spend_money",
    "architecture",
    "scope",
    "question",
)
CATEGORIES = (*CATEGORIES_0033, "triage")  # evo_agents.hub.runs.SHOWN_DECISION_CATEGORIES


def _categories(categories) -> None:
    op.drop_constraint("decisions_category_check", "decisions", type_="check")
    op.create_check_constraint("decisions_category_check", "decisions", sa.column("category").in_(categories))


def upgrade() -> None:
    _categories(CATEGORIES)


def downgrade() -> None:
    op.execute(sa.delete(sa.table("decisions")).where(sa.column("category") == "triage"))
    _categories(CATEGORIES_0033)
