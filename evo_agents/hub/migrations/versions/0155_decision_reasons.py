"""A decision says why its option is recommended, whether its act can be undone and how, and what it rests on.

Revision ID: 0155
Revises: 0154
Create Date: 2026-10-25

decisions gains ``recommended_reason``, why the agent recommends the option it marks recommended (1 to 500
characters, and only on a decision with an option marked so), ``reversible``, whether the act the decision asks about
can be undone, ``undo``, how (1 to 500 characters, and only when ``reversible`` is true), and ``evidence``, the refs
of what the question rests on: a JSON array of 1 to 20 texts that are not empty, each in a form
``evo_agents.hub.decision_asks`` reads, which the hub checks before it stores them. Every column is NULL for a
decision made before, and for one asked without them. The bounds below are copies of ``evo_agents.hub.decision_asks``
as of this revision, never imports, so a later change to them takes a new revision.

Going back drops the four columns, which is the schema 0154 runs on: its hub shows no reason, undo or evidence.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0155"
down_revision = "0154"
branch_labels = None
depends_on = None

TABLE = "decisions"
MAX_REASON_CHARS = 500  # evo_agents.hub.decision_asks.MAX_REASON_CHARS
MAX_UNDO_CHARS = 500  # evo_agents.hub.decision_asks.MAX_UNDO_CHARS
MAX_EVIDENCE = 20  # evo_agents.hub.decision_asks.MAX_EVIDENCE
RECOMMENDED = "$[*] ? (@.recommended == true)"  # an option the agent recommends
NOT_A_REF = '$[*] ? (@.type() != "string" || @ == "")'  # an item that is no text, or an empty one
REASON, UNDO, EVIDENCE = "decisions_recommended_reason_check", "decisions_undo_check", "decisions_evidence_check"
COLUMNS = ("recommended_reason", "reversible", "undo", "evidence")

col = sa.column


def _jsonpath(text: str):
    return sa.cast(sa.literal(text), postgresql.JSONPATH)


def _text(name: str, limit: int):
    return sa.func.char_length(col(name)).between(1, limit)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("recommended_reason", sa.Text()))
    op.add_column(TABLE, sa.Column("reversible", sa.Boolean()))
    op.add_column(TABLE, sa.Column("undo", sa.Text()))
    op.add_column(TABLE, sa.Column("evidence", postgresql.JSONB()))
    recommended = sa.func.jsonb_path_exists(col("options"), _jsonpath(RECOMMENDED))
    reason = sa.and_(_text("recommended_reason", MAX_REASON_CHARS), recommended)
    op.create_check_constraint(REASON, TABLE, sa.or_(col("recommended_reason").is_(None), reason))
    undo = sa.and_(_text("undo", MAX_UNDO_CHARS), col("reversible").is_(sa.true()))
    op.create_check_constraint(UNDO, TABLE, sa.or_(col("undo").is_(None), undo))
    evidence = col("evidence")
    texts = sa.and_(
        sa.func.jsonb_array_length(evidence).between(1, MAX_EVIDENCE),
        ~sa.func.jsonb_path_exists(evidence, _jsonpath(NOT_A_REF)),
    )
    # jsonb_array_length fails on a value that is not an array: the CASE reads it only for one
    shape = sa.case((sa.func.jsonb_typeof(evidence) == "array", texts), else_=False)
    op.create_check_constraint(EVIDENCE, TABLE, sa.or_(evidence.is_(None), shape))


def downgrade() -> None:
    for name in (EVIDENCE, UNDO, REASON):
        op.drop_constraint(name, TABLE, type_="check")
    for name in reversed(COLUMNS):
        op.drop_column(TABLE, name)
