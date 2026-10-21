"""What a task came to, as a field of its own: tasks_items.outcome_md.

Revision ID: 0088
Revises: 0087
Create Date: 2026-10-21

outcome_md is Markdown, empty until someone states the outcome. The pm daily report reads it before the section
``## Kết quả`` or ``## Outcome`` of the description, which it falls back to while the field is empty (plan
pm-daily-tracking, criterion a13). Every task keeps an empty outcome_md: the revision reads no description. The
statement is an Alembic operation.

Going back drops the column and what was written to it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks_items", sa.Column("outcome_md", sa.Text(), server_default="", nullable=False))


def downgrade() -> None:
    op.drop_column("tasks_items", "outcome_md")
