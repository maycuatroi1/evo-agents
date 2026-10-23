"""The stop of autonomy S4: autonomy_stops.

Revision ID: 0116
Revises: 0115
Create Date: 2026-10-23

The names below are copies of ``evo_agents.hub.tables_org`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

autonomy_stops holds every stop of autonomy (plan autonomy-levels, criterion a11), never deleted: its scope (login, a
person; unit, a unit and every unit below it; all, the whole hub), the person or the unit it names, the reason, who
stopped and when, and who lifted it and when (NULL while it holds). While a stop holds, the hub queues no run whose
principal it covers.

Going back drops autonomy_stops; the stops and their history are lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None

SCOPES = ("login", "unit", "all")  # evo_agents.hub.server.autonomy_stops.SCOPES
MAX_REASON_CHARS = 1000

col = sa.column


def _key(column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"autonomy_stops_{column}_fkey")


def upgrade() -> None:
    named = sa.and_(
        (col("scope") == "login") == col("user_id").is_not(None),
        (col("scope") == "unit") == col("unit_id").is_not(None),
    )
    op.create_table(
        "autonomy_stops",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("unit_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("stopped_by", sa.BigInteger(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lifted_by", sa.BigInteger(), nullable=True),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        _key("user_id", "users", "CASCADE"),
        _key("unit_id", "org_units", "RESTRICT"),
        _key("stopped_by", "users", "RESTRICT"),
        _key("lifted_by", "users", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="autonomy_stops_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("scope").in_(SCOPES),
                named,
                sa.func.char_length(col("reason")) <= MAX_REASON_CHARS,
                col("lifted_at").is_(None) == col("lifted_by").is_(None),
            ),
            name="autonomy_stops_check",
        ),
    )
    op.create_index("autonomy_stops_live_idx", "autonomy_stops", ["scope"], postgresql_where=col("lifted_at").is_(None))


def downgrade() -> None:
    op.drop_index("autonomy_stops_live_idx", table_name="autonomy_stops")
    op.drop_table("autonomy_stops")
