"""When a run's owner let it run as them (``evo_agents.hub.server.run_access``): runs.as_owner_since.

Revision ID: 0168
Revises: 0167
Create Date: 2026-10-27

runs gains ``as_owner_since``, the time its owner let it call the hub's API as them: when it was queued for a run
dispatched or rerun so, when the owner said so later (PUT .../runs/{id}/access, or the answer to a credential request),
and the time of the run before it for the run that resumes a parked run and the next attempt of a lost one. It is NULL
while the run does not run as its owner, which a check keeps: a run that stops running as them loses it. A run of 0167
that runs as its owner gets the time it was queued, the closest the hub knows.

Going back drops the column: the hub of 0167 says only whether a run runs as its owner.
"""

import sqlalchemy as sa
from alembic import op

revision = "0168"
down_revision = "0167"
branch_labels = None
depends_on = None

TABLE = "runs"
COLUMN = "as_owner_since"
CHECK = "runs_as_owner_since_check"

col = sa.column


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True)))
    runs = sa.table(TABLE, col("as_owner"), col("queued_at"), col(COLUMN))
    op.execute(runs.update().values({COLUMN: runs.c.queued_at}).where(runs.c.as_owner.is_(True)))
    op.create_check_constraint(CHECK, TABLE, sa.or_(col("as_owner").is_(True), col(COLUMN).is_(None)))


def downgrade() -> None:
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
