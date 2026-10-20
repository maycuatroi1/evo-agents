"""The tables of the daily release, which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.release_schedules``, ``tables.release_cycles``, ``tables.release_runs``,
``tables.release_checks``): queries name them there.

release_schedules (0067) holds the release schedule of each project, at most one; release_cycles (0067) each cycle a
check opened, at most one unfinished per schedule (``release_cycles_unfinished_key``); release_runs (0067) the runs of
each cycle with their mode; release_checks (0067) every check, the scheduled one of a local day once
(``release_checks_day_key``, a check that failed aside). runs gains ``runs_active_release_key``: a project has one
release run active at a time. ``evo_agents.hub.releases`` describes the model.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Double,
    ForeignKeyConstraint,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from evo_agents.hub.releases import UNFINISHED


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def _id() -> Column:
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def _money(name: str) -> Column:
    return Column(name, Double, nullable=False)


def _schedules(metadata: MetaData) -> Table:
    return Table(
        "release_schedules",
        metadata,
        _id(),
        Column("project_id", BigInteger, nullable=False),
        Column("repo", Text, nullable=False),
        Column("check_at", Text, nullable=False),  # HH:MM, local
        Column("timezone", Text, nullable=False),
        Column("target_ref", Text, nullable=False),
        Column("tag_pattern", Text, nullable=False),
        Column("workflow", Text, nullable=False),
        Column("paths", JSONB, nullable=False),
        _money("cycle_budget_usd"),
        Column("owner_id", BigInteger, nullable=False),
        Column("updated_by", BigInteger, nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        _when("paused_at"),
        Column("paused_by", BigInteger),
        ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="release_schedules_owner_id_fkey"),
        ForeignKeyConstraint(["paused_by"], ["users.id"], ondelete="RESTRICT", name="release_schedules_paused_by_fkey"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="release_schedules_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["updated_by"], ["users.id"], ondelete="RESTRICT", name="release_schedules_updated_by_fkey"
        ),
        PrimaryKeyConstraint("id", name="release_schedules_pkey"),
        UniqueConstraint("project_id", name="release_schedules_project_id_key"),
    )


def _cycles(metadata: MetaData) -> Table:
    cycles = Table(
        "release_cycles",
        metadata,
        _id(),
        Column("project_id", BigInteger, nullable=False),
        Column("schedule_id", BigInteger, nullable=False),
        Column("state", Text, nullable=False, server_default="open"),
        Column("start", Text, nullable=False),  # the mode of its first run
        Column("owner_id", BigInteger, nullable=False),
        Column("local_day", Date, nullable=False),
        Column("base_tag", Text),
        Column("base_commit", Text),
        Column("target_ref", Text, nullable=False),
        Column("target_commit", Text),
        Column("changed_paths", JSONB, nullable=False, server_default="[]"),
        _money("budget_usd"),
        Column("stop_reason", Text),
        _stamp("opened_at"),
        _stamp("updated_at"),
        _when("ended_at"),
        ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="release_cycles_owner_id_fkey"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="release_cycles_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["schedule_id"], ["release_schedules.id"], ondelete="CASCADE", name="release_cycles_schedule_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="release_cycles_pkey"),
    )
    Index(
        "release_cycles_unfinished_key",
        cycles.c.schedule_id,
        unique=True,
        postgresql_where=cycles.c.state.in_(UNFINISHED),
    )
    Index("release_cycles_schedule_idx", cycles.c.schedule_id, cycles.c.id)
    return cycles


def _runs(metadata: MetaData) -> Table:
    return Table(
        "release_runs",
        metadata,
        Column("run_id", BigInteger, nullable=False),
        Column("cycle_id", BigInteger, nullable=False),
        Column("mode", Text, nullable=False),
        Column("tag", Text),  # the red tag a fix run fixes forward
        _stamp("created_at"),
        ForeignKeyConstraint(
            ["cycle_id"], ["release_cycles.id"], ondelete="CASCADE", name="release_runs_cycle_id_fkey"
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="release_runs_run_id_fkey"),
        PrimaryKeyConstraint("run_id", name="release_runs_pkey"),
        Index("release_runs_cycle_idx", "cycle_id", "run_id"),
    )


def _checks(metadata: MetaData) -> Table:
    checks = Table(
        "release_checks",
        metadata,
        _id(),
        Column("project_id", BigInteger, nullable=False),
        Column("schedule_id", BigInteger, nullable=False),
        Column("trigger", Text, nullable=False),
        Column("local_day", Date, nullable=False),
        Column("requested_by", BigInteger),  # the admin of a manual check
        Column("outcome", Text, nullable=False),
        Column("reason", Text),
        Column("base_tag", Text),
        Column("base_commit", Text),
        Column("target_commit", Text),
        Column("changed_paths", JSONB),
        Column("cycle_id", BigInteger),
        Column("run_id", BigInteger),
        _stamp("checked_at"),
        ForeignKeyConstraint(
            ["cycle_id"], ["release_cycles.id"], ondelete="SET NULL", name="release_checks_cycle_id_fkey"
        ),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="release_checks_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["requested_by"], ["users.id"], ondelete="RESTRICT", name="release_checks_requested_by_fkey"
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL", name="release_checks_run_id_fkey"),
        ForeignKeyConstraint(
            ["schedule_id"], ["release_schedules.id"], ondelete="CASCADE", name="release_checks_schedule_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="release_checks_pkey"),
    )
    Index(
        "release_checks_day_key",
        checks.c.schedule_id,
        checks.c.local_day,
        unique=True,
        postgresql_where=(checks.c.trigger == "schedule") & (checks.c.outcome != "error"),
    )
    Index("release_checks_schedule_idx", checks.c.schedule_id, checks.c.id)
    return checks


def release_tables(metadata: MetaData, runs: Table, active: tuple[str, ...]) -> tuple[Table, Table, Table, Table]:
    """The four tables of the daily release on ``metadata``, and ``runs_active_release_key`` on ``runs``, whose
    ``active`` states hold one release run of a project at a time."""
    Index(
        "runs_active_release_key",
        runs.c.project_id,
        unique=True,
        postgresql_where=(runs.c.kind == "release") & runs.c.state.in_(active),
    )
    return _schedules(metadata), _cycles(metadata), _runs(metadata), _checks(metadata)
