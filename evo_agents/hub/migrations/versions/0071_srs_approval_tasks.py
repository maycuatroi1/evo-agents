"""The tasks of an approved SRS version: what the hub did to a project's tasks when a version of a BA document was
approved while the project enabled the plugin tasks.

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-21

srs_approval_tasks holds one row a version of the core plugin srs (``evo_agents.core_plugins.srs.auto_tasks``): the
version approved before it that it was compared with, the member whose approval made it approved, the task run of @ba
that splits the frame tasks into child tasks (whom the tool tasks__create of that run acts for: one run a row) and
what was done (``outcome``: the frame tasks made, the tasks commented on and cancelled, why no run was handed). The
row makes the work idempotent by version: a second call, such as an event delivered twice, finds it and does nothing.

The names below are copies of ``evo_agents.hub.tables_srs`` as of this revision, never imports, so a later change to
it takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Going back drops srs_approval_tasks; the tasks it made stay, as any task does.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

TABLE = "srs_approval_tasks"


def _key(column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{TABLE}_{column}_fkey")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_version", sa.Integer(), nullable=True),
        sa.Column("approved_by", sa.BigInteger(), nullable=False),
        sa.Column("split_run_id", sa.BigInteger(), nullable=True),
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _key("version_id", "srs_versions", ondelete="CASCADE"),
        _key("approved_by", "users", ondelete="RESTRICT"),
        _key("split_run_id", "runs", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=f"{TABLE}_pkey"),
        sa.UniqueConstraint("version_id", name=f"{TABLE}_version_id_key"),
        sa.UniqueConstraint("split_run_id", name=f"{TABLE}_split_run_id_key"),
    )
    outcome = sa.column("outcome")
    op.create_check_constraint(f"{TABLE}_outcome_check", TABLE, sa.func.jsonb_typeof(outcome) == "object")


def downgrade() -> None:
    op.drop_table(TABLE)
