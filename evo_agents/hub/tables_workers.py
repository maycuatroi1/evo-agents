"""The tables of workers (0009), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own attributes
(``tables.workers``, ``tables.worker_projects``, ``tables.worker_pairings``): queries name them there.

workers holds each machine a member registered to run plan steps for them: its owner, its token, its name (unique
among the owner's workers not revoked), what its host and daemon reported, its settings (slots, labels, plugins,
dispatch_from, version_locked) and when it was drained or revoked (``evo_agents.hub.server.workers``). The columns of
0048 keep the daemon's own updating: version_locked, its owner's lock on the version its daemon runs (false by
default); agent_commit, the commit of evo-agents its last heartbeat said it runs; self_update, what that heartbeat said
of updating itself to the tag nightly; self_update_since, when the hub first got the state self_update holds, in an
unbroken line of heartbeats that said it. kg_sync (0053) is what its last heartbeat said of keeping fresh the graphs of
the projects whose night shift it is on duty for (``evo_agents.worker.kg_sync``). The columns of 0065 keep its owner's
Update now: update_requested_at, when the last one was asked for; update_ended_at, when it ended (NULL while it is
open, or when none was asked for), update_outcome how (``runs.UPDATE_NOW_OUTCOMES``) and update_reason why, when the
daemon said. The columns of 0108 keep the newest report of its daemon's doctor (``evo_agents.hub.server.worker_doctor``):
doctor_at, when the hub took it; doctor_high and doctor_medium, its findings of each severity; doctor_findings, the
findings (code, severity, detail). hub_nightly (0065) is one row, the commit the tag nightly pointed to when the hub last read it and when
that was (``evo_agents.hub.nightly``), absent until the hub first read it. browser (0119) is what its last heartbeat
said of the Chromium its daemon starts for a run (``evo_agents.worker.browser``), NULL when it said nothing of one.
The columns of 0146 keep its owner's Install browser as those of 0065 keep Update now: browser_install_requested_at,
browser_install_ended_at, browser_install_outcome (``runs.BROWSER_INSTALL_OUTCOMES``) and browser_install_reason
(``evo_agents.hub.server.browser_install``).
worker_projects lists the projects a worker takes runs of; worker_pairings holds the pairing codes a member creates
for a machine to join with.
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
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


def _stamp(name: str) -> Column:
    """A timestamptz column that defaults to now() and is never NULL."""
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    """A timestamptz column that is NULL until it happens."""
    return Column(name, DateTime(timezone=True))


def _id() -> Column:
    """``id bigint GENERATED ALWAYS AS IDENTITY``."""
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def workers_table(metadata: MetaData) -> Table:
    """workers, added to ``metadata`` with its index of names."""
    workers = Table(
        "workers",
        metadata,
        _id(),
        Column("owner_id", BigInteger, nullable=False),
        Column("token_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("hostname", Text, nullable=False),
        Column("os", Text, nullable=False),
        Column("arch", Text, nullable=False),
        Column("agent_version", Text, nullable=False),
        Column("slots", SmallInteger, nullable=False, server_default="1"),
        Column("labels", ARRAY(Text), nullable=False, server_default="{}"),
        Column("runtimes", JSONB, nullable=False, server_default="{}"),
        Column("checkouts", JSONB, nullable=False, server_default="{}"),
        Column("free_slots", SmallInteger),
        Column("allow_web_terminal", Boolean, nullable=False, server_default=false()),
        _stamp("created_at"),
        _when("last_heartbeat_at"),
        _when("drained_at"),
        _when("revoked_at"),
        Column("dispatch_from", Text, nullable=False, server_default="any"),
        Column("run_kinds", ARRAY(Text)),
        Column("features", ARRAY(Text)),  # what its daemon says it does besides its kinds of run (0025)
        _when("steady_since"),  # the first heartbeat of the worker's current run of them, none late (0023)
        Column("version_locked", Boolean, nullable=False, server_default=false()),  # 0048
        Column("agent_commit", Text),  # 0048
        Column("self_update", JSONB),  # 0048
        _when("self_update_since"),  # 0048
        Column("kg_sync", JSONB),  # 0053
        Column("shared", Boolean, nullable=False, server_default=false()),  # 0062
        _when("update_requested_at"),  # 0065
        _when("update_ended_at"),  # 0065
        Column("update_outcome", Text),  # 0065
        Column("update_reason", Text),  # 0065
        Column("browser", JSONB),  # 0119
        _when("browser_install_requested_at"),  # 0146
        _when("browser_install_ended_at"),  # 0146
        Column("browser_install_outcome", Text),  # 0146
        Column("browser_install_reason", Text),  # 0146
        _when("doctor_at"),  # 0108
        Column("doctor_high", SmallInteger),  # 0108
        Column("doctor_medium", SmallInteger),  # 0108
        Column("doctor_findings", JSONB),  # 0108
        ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="workers_owner_id_fkey"),
        ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="RESTRICT", name="workers_token_id_fkey"),
        PrimaryKeyConstraint("id", name="workers_pkey"),
        UniqueConstraint("token_id", name="workers_token_id_key"),
    )
    Index(
        "workers_name_key",
        workers.c.owner_id,
        func.lower(workers.c.name),
        unique=True,
        postgresql_where=workers.c.revoked_at.is_(None),
    )
    return workers


def nightly_table(metadata: MetaData) -> Table:
    """hub_nightly, added to ``metadata``: at most one row, id 1."""
    return Table(
        "hub_nightly",
        metadata,
        Column("id", SmallInteger, nullable=False, server_default="1"),
        Column("commit_sha", Text, nullable=False),
        Column("read_at", DateTime(timezone=True), nullable=False),
        PrimaryKeyConstraint("id", name="hub_nightly_pkey"),
    )


def worker_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table]:
    """workers, worker_projects, worker_pairings and hub_nightly, added to ``metadata``."""
    workers = workers_table(metadata)
    worker_projects = Table(
        "worker_projects",
        metadata,
        Column("worker_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="worker_projects_project_id_fkey"
        ),
        ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="CASCADE", name="worker_projects_worker_id_fkey"),
        PrimaryKeyConstraint("worker_id", "project_id", name="worker_projects_pkey"),
    )
    worker_pairings = Table(
        "worker_pairings",
        metadata,
        _id(),
        Column("code_selector", Text, nullable=False),
        Column("code_hash", Text, nullable=False),
        Column("owner_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("projects", ARRAY(BigInteger), nullable=False),
        Column("slots", SmallInteger, nullable=False, server_default="1"),
        Column("labels", ARRAY(Text), nullable=False, server_default="{}"),
        Column("allow_web_terminal", Boolean, nullable=False, server_default=false()),
        Column("attempts", SmallInteger, nullable=False, server_default="0"),
        _stamp("created_at"),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        _when("used_at"),
        Column("worker_id", BigInteger),
        ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE", name="worker_pairings_owner_id_fkey"),
        ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="CASCADE", name="worker_pairings_worker_id_fkey"),
        PrimaryKeyConstraint("id", name="worker_pairings_pkey"),
        UniqueConstraint("worker_id", name="worker_pairings_worker_id_key"),
    )
    Index(
        "worker_pairings_selector_key",
        worker_pairings.c.code_selector,
        unique=True,
        postgresql_where=worker_pairings.c.used_at.is_(None),
    )
    Index("worker_pairings_owner_idx", worker_pairings.c.owner_id, postgresql_where=worker_pairings.c.used_at.is_(None))
    return workers, worker_projects, worker_pairings, nightly_table(metadata)
