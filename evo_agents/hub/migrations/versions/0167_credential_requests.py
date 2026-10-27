"""A decision that asks a run's owner for a credential keeps what it asks for (``evo_agents.hub.credential_requests``).

Revision ID: 0167
Revises: 0166
Create Date: 2026-10-27

decisions gains ``credential_request``, a JSON object of the variable the run needs (``env_var``), the name of the
secret that keeps its value (``secret_name``) and whether it asks for the owner's account on the hub
(``hub_account``), set only on a decision of category credential that the agent asked as such; NULL for every decision
before, and for any other. It never holds a value: the value the owner pastes goes to the owner's secrets alone.

Going back drops the column: the hub of 0166 shows those decisions as decisions of category credential with their
options, and answers them with no value and no grant.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0167"
down_revision = "0166"
branch_labels = None
depends_on = None

TABLE = "decisions"
COLUMN = "credential_request"
CATEGORY = "credential"  # evo_agents.hub.credential_requests.CATEGORY
CHECK = "decisions_credential_request_check"

col = sa.column


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, postgresql.JSONB()))
    request = col(COLUMN)
    shape = sa.and_(col("category") == CATEGORY, sa.func.jsonb_typeof(request) == "object")
    op.create_check_constraint(CHECK, TABLE, sa.or_(request.is_(None), shape))


def downgrade() -> None:
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
