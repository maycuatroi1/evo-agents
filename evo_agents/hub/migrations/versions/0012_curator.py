"""The night shift: the charter of a project at every revision, its schedules, and the runs a schedule queues.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-08

``evo_agents.hub.curator`` describes the design these tables hold the state of; the lists below are copies of that
module's and of ``evo_agents.hub.credentials``'s as of this revision, never imports, so a later change to them takes a
new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions, as ``evo_agents.hub.tables``
describes the tables.

charters keeps every revision of a project's charter, numbered from 1: the body the admin wrote (a JSON object), the
worker on duty it names, who wrote it and when. A charter goes with its project; a worker is never deleted.

schedules is what a charter makes run: one row per project and kind (``night_shift``), with the member it dispatches
as (the owner of the worker on duty) and that worker. paused_at and paused_by are set together while it is paused.

runs.schedule_id names the schedule that queued a run, and schedule_night the night it belongs to, the local date the
window opened on; the two are set together. runs.budget holds the run's caps, a JSON object. dispatched_via takes a
third value, 'schedule', exactly for the runs a schedule queued.

Going back drops the tables and the three columns, which is the schema that release 0.6.0 runs on; the runs a schedule
queued stay, as dispatched with a machine token.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

SCHEDULE_KINDS = ("night_shift",)  # evo_agents.hub.curator.SCHEDULE_KINDS
DISPATCHED_VIA_0011 = ("machine", "web")
DISPATCHED_VIA = ("machine", "web", "schedule")  # evo_agents.hub.credentials.DISPATCHED_VIA

col = sa.column


def _stamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _together(first: str, second: str):
    """Both columns set, or neither."""
    return col(first).is_(None) == col(second).is_(None)


def upgrade() -> None:
    op.create_table(
        "charters",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        sa.CheckConstraint(col("revision") >= 1, name="charters_revision_check"),
        sa.CheckConstraint(sa.func.jsonb_typeof(col("body")) == "object", name="charters_body_check"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="charters_created_by_fkey"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="charters_project_id_fkey"),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="RESTRICT", name="charters_worker_id_fkey"),
        sa.PrimaryKeyConstraint("project_id", "revision", name="charters_pkey"),
    )
    op.create_table(
        "schedules",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(col("kind").in_(SCHEDULE_KINDS), name="schedules_kind_check"),
        sa.CheckConstraint(_together("paused_at", "paused_by"), name="schedules_paused_check"),
        sa.CheckConstraint(col("updated_at") >= col("created_at"), name="schedules_updated_check"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="schedules_owner_id_fkey"),
        sa.ForeignKeyConstraint(["paused_by"], ["users.id"], ondelete="RESTRICT", name="schedules_paused_by_fkey"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="schedules_project_id_fkey"),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="RESTRICT", name="schedules_worker_id_fkey"),
        sa.PrimaryKeyConstraint("id", name="schedules_pkey"),
        sa.UniqueConstraint("project_id", "kind", name="schedules_project_id_kind_key"),
    )
    op.add_column("runs", sa.Column("schedule_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("schedule_night", sa.Date(), nullable=True))
    op.add_column("runs", sa.Column("budget", postgresql.JSONB(), nullable=True))
    op.create_foreign_key("runs_schedule_id_fkey", "runs", "schedules", ["schedule_id"], ["id"], ondelete="RESTRICT")
    op.create_index(
        "runs_schedule_idx",
        "runs",
        ["schedule_id", "schedule_night"],
        postgresql_where=col("schedule_id").is_not(None),
    )
    op.create_check_constraint("runs_schedule_night_check", "runs", _together("schedule_id", "schedule_night"))
    op.create_check_constraint("runs_budget_check", "runs", sa.func.jsonb_typeof(col("budget")) == "object")
    op.drop_constraint("runs_dispatched_via_check", "runs", type_="check")
    op.create_check_constraint("runs_dispatched_via_check", "runs", col("dispatched_via").in_(DISPATCHED_VIA))
    op.create_check_constraint(
        "runs_schedule_check", "runs", (col("dispatched_via") == "schedule") == col("schedule_id").is_not(None)
    )


def downgrade() -> None:
    runs = sa.table("runs", col("dispatched_via"))
    op.drop_constraint("runs_schedule_check", "runs", type_="check")
    op.drop_constraint("runs_budget_check", "runs", type_="check")
    op.drop_constraint("runs_schedule_night_check", "runs", type_="check")
    op.drop_index("runs_schedule_idx", table_name="runs")
    op.drop_constraint("runs_schedule_id_fkey", "runs", type_="foreignkey")
    op.drop_column("runs", "budget")
    op.drop_column("runs", "schedule_night")
    op.drop_column("runs", "schedule_id")
    op.drop_constraint("runs_dispatched_via_check", "runs", type_="check")
    op.execute(runs.update().where(runs.c.dispatched_via == "schedule").values(dispatched_via="machine"))
    op.create_check_constraint("runs_dispatched_via_check", "runs", col("dispatched_via").in_(DISPATCHED_VIA_0011))
    op.drop_table("schedules")
    op.drop_table("charters")
