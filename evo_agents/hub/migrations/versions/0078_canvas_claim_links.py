"""What a claim of the core plugin canvas links: the use cases of the plugin srs that realize it and the signal keys
that measure it.

Revision ID: 0078
Revises: 0077
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_canvas`` and ``evo_agents.core_plugins.canvas.content`` as of
this revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation
on SQLAlchemy Core expressions.

canvas_claims gains ``use_cases`` (the codes of use cases, as the SRS names them) and ``signal_keys`` (keys of the
project's registry of signals), each an array of at most 20 texts, empty for every claim made before. A rev's content
is JSON, so it holds the two lists with no change of canvas_claim_revs; a rev written before reads as linking nothing.

Going back drops the check and the two columns, and with them every claim's links; the revs keep theirs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None

MAX_LINKS = 20  # content.MAX_LINKS
LINKS = ("use_cases", "signal_keys")
TABLE = "canvas_claims"
CHECK = "canvas_claims_links_check"

col = sa.column


def upgrade() -> None:
    for name in LINKS:
        op.add_column(TABLE, sa.Column(name, postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False))
    op.create_check_constraint(CHECK, TABLE, sa.and_(*(sa.func.cardinality(col(name)) <= MAX_LINKS for name in LINKS)))


def downgrade() -> None:
    op.drop_constraint(CHECK, TABLE, type_="check")
    for name in reversed(LINKS):
        op.drop_column(TABLE, name)
