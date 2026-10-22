"""Autonomy levels: the class overrides, environments and workers of a ceiling, and autonomy_prefs.

Revision ID: 0101
Revises: 0100
Create Date: 2026-10-22

The names below are copies of ``evo_agents.hub.autonomy_levels`` and ``evo_agents.hub.tables_org`` as of this
revision, never imports, so a later change to them takes a new revision. Every statement is an Alembic operation on
SQLAlchemy Core expressions.

autonomy_ceilings gains three columns (plan autonomy-levels, criterion a2): overrides, an object of classes K1 to K12
each set to a mode (``deny``, ``ask``, ``notify``, ``auto``, with an optional ``:condition``) that the service checks;
environments and workers, the lists a ceiling allows, empty for no limit. Every revision written before reads as its
level alone, with no limit.

autonomy_prefs holds every revision of a person's own preference: the level A0 to A5 and the class overrides; the
revisions only grow. A person without a row prefers A5, so the hub reads as before.

Going back drops autonomy_prefs and the three columns; the preferences and the class overrides are lost.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

LEVELS = ("A0", "A1", "A2", "A3", "A4", "A5")
MAX_PLACES = 100  # environments or workers of one ceiling

col = sa.column


def _ceilings() -> None:
    op.add_column(
        "autonomy_ceilings",
        sa.Column("overrides", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column(
        "autonomy_ceilings", sa.Column("environments", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False)
    )
    op.add_column(
        "autonomy_ceilings", sa.Column("workers", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False)
    )
    op.create_check_constraint(
        "autonomy_ceilings_classes_check",
        "autonomy_ceilings",
        sa.and_(
            sa.func.jsonb_typeof(col("overrides")) == "object",
            sa.func.cardinality(col("environments")) <= MAX_PLACES,
            sa.func.cardinality(col("workers")) <= MAX_PLACES,
        ),
    )


def _prefs() -> None:
    op.create_table(
        "autonomy_prefs",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("overrides", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="autonomy_prefs_user_id_fkey"),
        sa.PrimaryKeyConstraint("user_id", "revision", name="autonomy_prefs_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("level").in_(LEVELS),
                col("revision") >= 1,
                sa.func.jsonb_typeof(col("overrides")) == "object",
            ),
            name="autonomy_prefs_check",
        ),
    )


def upgrade() -> None:
    _ceilings()
    _prefs()


def downgrade() -> None:
    op.drop_table("autonomy_prefs")
    op.drop_constraint("autonomy_ceilings_classes_check", "autonomy_ceilings", type_="check")
    op.drop_column("autonomy_ceilings", "workers")
    op.drop_column("autonomy_ceilings", "environments")
    op.drop_column("autonomy_ceilings", "overrides")
