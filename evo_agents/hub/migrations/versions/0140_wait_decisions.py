"""A plan run waits for a time or an event of the hub, and a decision stays open on an answer that says not yet.

Revision ID: 0140
Revises: 0139
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.runs`` as of this revision, never imports, so a later change to them
takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

decisions.category takes ``wait``: what the agent of a plan run opens with ``evo-agents worker wait``, which the hub
answers itself when its time or its event comes (``evo_agents.hub.server.run_waits``). decisions gains ``wake_at``, when
a wait wakes at the latest, ``wake_on``, the event it wakes on sooner (a kind of hub_events, the payload it contains and
the cursor past which it is looked for), and ``held_at``, when the person asked last chose an option that leaves the
decision open (an option stored with ``"waits": true``). Every column is NULL for a decision made before.

The hub answers a wait as nobody, so the check of 0010 that an answered decision has answered_by and only an answered
one does (decisions_check2) becomes two: answered_by only on an answered decision, and on every answered decision but
a wait.

Going back deletes the decisions of category wait, drops the three columns, and puts the checks of 0010 and 0121 back.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None

CATEGORIES_0121 = (  # decisions_category_check as 0121 set it
    "deploy",
    "delete_data",
    "live_migration",
    "external_send",
    "spend_money",
    "architecture",
    "scope",
    "question",
    "triage",
    "release",
    "loop",
    "autonomy",
    "merge",
    "credential",
    "access",
    "login",
)
CATEGORY = "wait"
CATEGORIES = (*CATEGORIES_0121, CATEGORY)  # evo_agents.hub.runs.SHOWN_DECISION_CATEGORIES
TABLE = "decisions"
ANSWERED_BY_0010 = "decisions_check2"  # (state = 'answered') = (answered_by IS NOT NULL), as 0010 named it
ANSWERED_BY = "decisions_answered_by_check"
ANSWERER = "decisions_answerer_check"

col = sa.column


def _check(values) -> None:
    op.drop_constraint("decisions_category_check", TABLE, type_="check")
    op.create_check_constraint("decisions_category_check", TABLE, col("category").in_(values))


def upgrade() -> None:
    _check(CATEGORIES)
    op.add_column(TABLE, sa.Column("wake_at", sa.DateTime(timezone=True)))
    op.add_column(TABLE, sa.Column("wake_on", postgresql.JSONB()))
    op.add_column(TABLE, sa.Column("held_at", sa.DateTime(timezone=True)))
    answered = col("state") == "answered"
    op.drop_constraint(ANSWERED_BY_0010, TABLE, type_="check")
    op.create_check_constraint(ANSWERED_BY, TABLE, sa.or_(col("answered_by").is_(None), answered))
    op.create_check_constraint(
        ANSWERER, TABLE, sa.or_(~answered, col("answered_by").is_not(None), col("category") == CATEGORY)
    )


def downgrade() -> None:
    decisions = sa.table(TABLE, col("category"))
    op.execute(decisions.delete().where(decisions.c.category == CATEGORY))
    op.drop_constraint(ANSWERER, TABLE, type_="check")
    op.drop_constraint(ANSWERED_BY, TABLE, type_="check")
    op.create_check_constraint(ANSWERED_BY_0010, TABLE, (col("state") == "answered") == col("answered_by").is_not(None))
    op.drop_column(TABLE, "held_at")
    op.drop_column(TABLE, "wake_on")
    op.drop_column(TABLE, "wake_at")
    _check(CATEGORIES_0121)
