"""The hub answers a credential request its owner's secret covers.

Revision ID: 0177
Revises: 0176
Create Date: 2026-10-28

When a run asks for a credential that a secret of its owner's sets, the hub answers the decision itself
(``evo_agents.hub.server.credential_resolver``): answered by nobody, as a wait is. decisions_answerer_check, which 0140
made to let only a wait be answered by nobody, takes a credential request too: a decision of category credential that
holds a ``credential_request``. The names below are copies of 0140's, never imports.

Going back gives each credential request the hub answered its asked_of, else the owner of its run, as the one who
answered it, and puts 0140's check back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0177"
down_revision = "0176"
branch_labels = None
depends_on = None

TABLE = "decisions"
ANSWERER = "decisions_answerer_check"
WAIT = "wait"
CREDENTIAL = "credential"

col = sa.column


def _answerer(*nobody) -> None:
    op.drop_constraint(ANSWERER, TABLE, type_="check")
    op.create_check_constraint(
        ANSWERER, TABLE, sa.or_(col("state") != "answered", col("answered_by").is_not(None), *nobody)
    )


def upgrade() -> None:
    credential = sa.and_(col("category") == CREDENTIAL, col("credential_request").is_not(None))
    _answerer(col("category") == WAIT, credential)


def downgrade() -> None:
    decisions = sa.table(
        TABLE, col("id"), col("run_id"), col("category"), col("state"), col("answered_by"), col("asked_of")
    )
    runs = sa.table("runs", col("id"), col("dispatched_by"))
    owner = sa.select(runs.c.dispatched_by).where(runs.c.id == decisions.c.run_id).scalar_subquery()
    op.execute(
        decisions.update()
        .where(
            decisions.c.category == CREDENTIAL,
            decisions.c.state == "answered",
            decisions.c.answered_by.is_(None),
        )
        .values(answered_by=sa.func.coalesce(decisions.c.asked_of, owner))
    )
    _answerer(col("category") == WAIT)
