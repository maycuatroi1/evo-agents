"""The problems of its project a claim of the core plugin canvas points to.

Revision ID: 0135
Revises: 0134
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.tables_canvas`` and ``evo_agents.core_plugins.canvas.content`` as of
this revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation
on SQLAlchemy Core expressions.

canvas_claims gains ``problems`` (plan problem-oriented-hub, criterion a3): the codes (P-<number>) of problems of the
project the claim answers, an array of at most 20 texts beside ``use_cases`` and ``signal_keys`` of 0078, empty for
every claim made before. The check of 0078 takes the new column in. A rev's content is JSON, so it holds the list with
no change of canvas_claim_revs; a rev written before reads as linking no problem.

Going back puts the check of 0078 back and drops the column, and with it every claim's problems; the revs keep theirs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None

MAX_LINKS = 20  # content.MAX_LINKS
LINKS_0078 = ("use_cases", "signal_keys")
PROBLEMS = "problems"
TABLE = "canvas_claims"
CHECK = "canvas_claims_links_check"

col = sa.column


def _check(names) -> None:
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.create_check_constraint(CHECK, TABLE, sa.and_(*(sa.func.cardinality(col(name)) <= MAX_LINKS for name in names)))


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(PROBLEMS, postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False))
    _check((*LINKS_0078, PROBLEMS))


def downgrade() -> None:
    _check(LINKS_0078)
    op.drop_column(TABLE, PROBLEMS)
