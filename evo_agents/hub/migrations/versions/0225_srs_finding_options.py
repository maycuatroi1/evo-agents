"""The choices of a finding of a BA document: srs_findings.options.

Revision ID: 0225
Revises: 0224
Create Date: 2026-11-08

A finding of level decide that @ba records may carry the choices members pick from (``evo_agents.hub.server.srs.findings``):
a list of short sentences in the document's language, two to four of them, in the order the reviewer sees them (A, B, C,
D). A member settles such a finding from the review of a use case by picking one, which becomes its resolution. Every
finding recorded before has none, so the column defaults to an empty list; the check keeps it a list of at most four. The
names below are copies of ``evo_agents.hub.server.srs.findings`` as of this revision, never imports. Every statement is
an Alembic operation on SQLAlchemy Core expressions.

Going back drops the column and its check.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0225"
down_revision = "0224"
branch_labels = None
depends_on = None

FINDINGS = "srs_findings"
CHECK = "srs_findings_options_check"
MAX_OPTIONS = 4


def upgrade() -> None:
    op.add_column(
        FINDINGS,
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
    )
    options = sa.column("options")
    op.create_check_constraint(
        CHECK,
        FINDINGS,
        sa.and_(sa.func.jsonb_typeof(options) == "array", sa.func.jsonb_array_length(options) <= MAX_OPTIONS),
    )


def downgrade() -> None:
    op.drop_constraint(CHECK, FINDINGS, type_="check")
    op.drop_column(FINDINGS, "options")
