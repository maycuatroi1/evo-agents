"""Guards of a graph element: kg_nodes.label_guards and kg_edges.label_guards.

Revision ID: 0267
Revises: 0266
Create Date: 2026-11-24

Who reads a node or an edge of a project's graph is now a disjunction of labels (``evo_agents.schema.policy.PolicyDNF``),
one for each independent way the sources reveal it. A row keeps its first atom in label_lvl, label_loc, label_int and
label_aud as before, so a reader that knows no other still reads it only when that atom lets it; an element of more
than one atom also keeps all of them in label_guards, a JSON list of objects (lvl, loc, int, aud when it has one, and
the props of the branch that atom reads). NULL for every row of one atom and for every row before this revision, so a
build writes it only for the rows that need it.

Going back drops the two columns; a reader then reads each element by its first atom alone.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0267"
down_revision = "0266"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("kg_nodes", "kg_edges"):
        op.add_column(table, sa.Column("label_guards", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    for table in ("kg_nodes", "kg_edges"):
        op.drop_column(table, "label_guards")
