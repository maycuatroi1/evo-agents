"""What a merge approval is bound to: the head and the scope of its request.

Revision ID: 0184
Revises: 0183
Create Date: 2026-10-30

decisions.approval is new (plan super-board-parity-2, criterion a3; ``evo_agents.hub.approval_heads``): for a decision
of category merge that names its pull request, and one the gate of autonomy asks for a merge, the head the answer
holds for and the hash of the request's scope (its files, its migrations, its needs-you lines), with the request,
``{"pull", "head", "scope"}``. A push that moves the head or changes the scope leaves the answer behind, and the
question is asked again. Every decision asked before has none and opens the gate as it did.

Going back drops the column.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0184"
down_revision = "0183"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decisions", sa.Column("approval", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_check_constraint(
        "decisions_approval_check",
        "decisions",
        "approval IS NULL OR (jsonb_typeof(approval) = 'object'"
        " AND coalesce(jsonb_typeof(approval -> 'head'), '') = 'string')",
    )


def downgrade() -> None:
    op.drop_constraint("decisions_approval_check", "decisions", type_="check")
    op.drop_column("decisions", "approval")
