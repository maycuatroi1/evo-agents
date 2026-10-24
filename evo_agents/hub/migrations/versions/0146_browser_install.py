"""An owner's Install browser of a worker.

Revision ID: 0146
Revises: 0145
Create Date: 2026-10-25

workers.browser_install_requested_at is when the worker's owner, or a hub admin, last asked its daemon to install
Chromium (POST /v1/workers/{id}/browser-install); browser_install_ended_at is when that request ended, NULL while it
is open and when none was ever asked for; browser_install_outcome says how (installed, cancelled, failed or
impossible) and browser_install_reason why, when the daemon or the hub said. An outcome goes with an end and an end
with a request. The bound below is a copy of ``evo_agents.hub.runs.BROWSER_INSTALL_OUTCOMES`` as of this revision,
never an import, so a later change to it takes a new revision.

Going back drops the four columns, which is the schema 0145 runs on: its hub takes no Install browser.
"""

import sqlalchemy as sa
from alembic import op

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None

OUTCOMES = ("installed", "cancelled", "failed", "impossible")  # evo_agents.hub.runs.BROWSER_INSTALL_OUTCOMES
COLUMNS = (
    "browser_install_requested_at",
    "browser_install_ended_at",
    "browser_install_outcome",
    "browser_install_reason",
)
CHECK = "workers_browser_install_check"

col = sa.column


def upgrade() -> None:
    op.add_column("workers", sa.Column("browser_install_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workers", sa.Column("browser_install_ended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workers", sa.Column("browser_install_outcome", sa.Text(), nullable=True))
    op.add_column("workers", sa.Column("browser_install_reason", sa.Text(), nullable=True))
    ended, outcome = col("browser_install_ended_at"), col("browser_install_outcome")
    shape = sa.and_(
        sa.or_(ended.is_(None), col("browser_install_requested_at").is_not(None)),
        # outcome IS NOT NULL first: a NULL outcome makes IN NULL, and a CHECK passes on NULL
        sa.or_(
            sa.and_(ended.is_(None), outcome.is_(None)),
            sa.and_(ended.is_not(None), outcome.is_not(None), outcome.in_(OUTCOMES)),
        ),
        sa.or_(col("browser_install_reason").is_(None), outcome.is_not(None)),
    )
    op.create_check_constraint(CHECK, "workers", shape)


def downgrade() -> None:
    op.drop_constraint(CHECK, "workers", type_="check")
    for name in reversed(COLUMNS):
        op.drop_column("workers", name)
