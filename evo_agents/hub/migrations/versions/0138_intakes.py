"""The intakes of a project: one reading of vague context by a member's session.

Revision ID: 0138
Revises: 0137
Create Date: 2026-10-24

The names below are copies of ``evo_agents.hub.tables_intake`` and ``evo_agents.hub.intakes`` as of this revision,
never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy
Core expressions.

intakes holds one row per intake of a project (plan problem-intake, criterion a2), numbered within the project (the
code I-<number>, 1 and up): the member who asked for it, the list of sources the session read (a JSON array, never raw
content), a note (1 to MAX_NOTE_CHARS, or NULL), its status (STATUSES) and, exactly when it is done, the result the
session put (``evo-intake/1``), what the hub made of it, who put it and when. A project's intakes go with it, and a
member an intake names is not deleted under it. No other table changes.

Going back drops the table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None

TABLE = "intakes"
STATUSES = ("open", "done")
MAX_NOTE_CHARS = 1000

col = sa.column


def _key(column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        sa.Column("put_by", sa.BigInteger(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        _key("project_id", "projects", "CASCADE"),
        _key("requested_by", "users", "RESTRICT"),
        _key("put_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.UniqueConstraint("project_id", "number", name=f"{TABLE}_number_key"),
        sa.CheckConstraint(
            sa.and_(
                col("number") >= 1,
                col("status").in_(STATUSES),
                sa.func.jsonb_typeof(col("sources")) == "array",
                sa.or_(
                    col("note").is_(None),
                    sa.func.char_length(sa.func.btrim(col("note"))).between(1, MAX_NOTE_CHARS),
                ),
                (col("status") == "done") == col("result").is_not(None),
                (col("status") == "done") == col("outcome").is_not(None),
                (col("status") == "done") == col("put_by").is_not(None),
                (col("status") == "done") == col("done_at").is_not(None),
            ),
            name=f"{TABLE}_check",
        ),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
