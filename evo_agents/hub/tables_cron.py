"""The tables of a project's cron jobs (0243, plan hub-cron-jobs), which ``evo_agents.hub.tables`` adds to its metadata
and exposes as its own attributes (``tables.cron_jobs``, ``tables.cron_fires``): queries name them there.
``evo_agents.hub.cron_store`` reads and writes them; ``docs/hub.md``, section Cron jobs, describes the model.

- cron_jobs: each cron job of a project: its name (unique in the project), five cron fields and IANA time zone, the
  kind of work it starts (``agent``: an agent profile, the chat its threads go in, NULL for the owner's direct chat
  with the agent, and a prompt; ``plan``: a plan of the project; ``command``: a shell command line, the repo it runs
  in, NULL for an empty directory, and its time limit in minutes), its owner (who made it; runs are dispatched as
  them), its revision, who changed it last and when, its pause: when, by whom (NULL: the hub paused it) and why, and
  when it was resumed last (0245; NULL: never), from which it fires and its breaker counts.
- cron_fires: each fire of a job, one per (job, scheduled minute) (``cron_fires_job_id_scheduled_for_key``): when it
  was due, whether someone pressed "run now" (``manual``, its ``actor_id``), its outcome (``started``, ``busy``,
  ``missed`` or ``refused``; NULL while the hub has not handled it yet), why, and the run it started.

runs gain ``cron_job_id``, the job whose fire queued the run (NULL for any other run; ``runs_cron_job_idx``), which
counts a job busy and sums the cost of its runs; ``cron_columns`` adds it to the runs table it is given.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
)

KINDS = ("agent", "plan", "command")
OUTCOMES = ("started", "busy", "missed", "refused")
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


def _key(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def _jobs(metadata: MetaData) -> Table:
    name = "cron_jobs"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("cron", Text, nullable=False),
        Column("timezone", Text, nullable=False),
        Column("kind", Text, nullable=False),
        Column("profile_id", BigInteger),
        Column("chat_id", BigInteger),
        Column("prompt", Text),
        Column("plan_id", Text),
        Column("command", Text),
        Column("repo", Text),
        Column("timeout_minutes", Integer),
        Column("owner_id", BigInteger, nullable=False),
        Column("revision", Integer, nullable=False, server_default="1"),
        Column("updated_by", BigInteger),
        _stamp("created_at"),
        _stamp("updated_at"),
        _when("paused_at"),
        Column("paused_by", BigInteger),
        Column("paused_reason", Text),
        _when("resumed_at"),
        _key(name, "project_id", "projects", ondelete="CASCADE"),
        _key(name, "profile_id", "agent_profiles", ondelete="SET NULL"),
        _key(name, "chat_id", "chats", ondelete="SET NULL"),
        _key(name, "owner_id", "users", ondelete="CASCADE"),
        _key(name, "updated_by", "users", ondelete="SET NULL"),
        _key(name, "paused_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        UniqueConstraint("project_id", "name", name=f"{name}_project_id_name_key"),
    )


def _fires(metadata: MetaData) -> Table:
    name = "cron_fires"
    table = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("job_id", BigInteger, nullable=False),
        Column("scheduled_for", DateTime(timezone=True), nullable=False),
        Column("manual", Boolean, nullable=False, server_default=false()),
        Column("actor_id", BigInteger),
        Column("outcome", Text),
        Column("reason", Text),
        Column("run_id", BigInteger),
        _stamp("created_at"),
        _when("handled_at"),
        _key(name, "job_id", "cron_jobs", ondelete="CASCADE"),
        _key(name, "actor_id", "users", ondelete="SET NULL"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        UniqueConstraint("job_id", "scheduled_for", name=f"{name}_job_id_scheduled_for_key"),
    )
    Index(f"{name}_pending_idx", table.c.id, postgresql_where=table.c.outcome.is_(None))
    return table


def cron_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(cron_jobs, cron_fires), added to ``metadata``."""
    return _jobs(metadata), _fires(metadata)


def cron_columns(runs: Table) -> None:
    """Give ``runs`` the job whose fire queued it (0243)."""
    runs.append_column(Column("cron_job_id", BigInteger))
    runs.append_constraint(
        ForeignKeyConstraint(["cron_job_id"], ["cron_jobs.id"], ondelete="SET NULL", name="runs_cron_job_id_fkey")
    )
    Index("runs_cron_job_idx", runs.c.cron_job_id, runs.c.id.desc(), postgresql_where=runs.c.cron_job_id.is_not(None))
