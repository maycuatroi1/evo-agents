"""Browser runs: runs.browser and workers.browser.

Revision ID: 0106
Revises: 0105
Create Date: 2026-10-23

A run dispatched with browser gets a Chromium of its own on its worker, started by the daemon for its agent (plan
agent-browser, criterion a3); runs.browser says so, false for every run before this revision and for one dispatched
without it. workers.browser is what the worker's last heartbeat said of its Chromium (version, headless or with a
screen, how many browsers at once, whether it runs sandboxed), an object as
``evo_agents.hub.server.runs.models.browser.BrowserReport`` describes it; NULL when that heartbeat said nothing of one,
as an older daemon or one without Chromium does. The hub hands a browser run only to a worker whose heartbeat lists
the feature browser, and no more of them at once than its report allows. Every statement is an Alembic operation on
SQLAlchemy Core expressions, as ``evo_agents.hub.tables`` describes the tables.

Going back drops both columns, which is the schema that schema 0105 runs on: a run asks for no browser, and the
heartbeat neither takes nor keeps one.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None

CHECK = "workers_browser_check"


def upgrade() -> None:
    op.add_column("runs", sa.Column("browser", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("workers", sa.Column("browser", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(CHECK, "workers", sa.func.jsonb_typeof(sa.column("browser")) == "object")


def downgrade() -> None:
    op.drop_constraint(CHECK, "workers", type_="check")
    op.drop_column("workers", "browser")
    op.drop_column("runs", "browser")
