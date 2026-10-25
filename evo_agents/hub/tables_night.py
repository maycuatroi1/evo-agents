"""The tables of the night shift (0012), which ``evo_agents.hub.tables`` adds to its metadata as ``tables.charters``
and ``tables.schedules``.

charters keeps each revision of a project's charter, the worker or pool its night runs go to (pool_id, 0149) and who
wrote it; schedules holds one row per project and kind of night job, who owns it, the worker or pool it runs on, and when and why it was paused (by a
member, or by the hub's circuit breaker: ``pause_reason``, 0018).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    column,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB


def _stamp(name: str) -> Column:
    """A timestamptz column that defaults to now() and is never NULL."""
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _one_of(table: str) -> CheckConstraint:
    """A worker or a pool, exactly one (0149)."""
    return CheckConstraint(
        column("worker_id").is_(None) != column("pool_id").is_(None), name=f"{table}_worker_or_pool_check"
    )


def night_tables(metadata: MetaData) -> tuple[Table, Table]:
    """charters and schedules (0012), added to ``metadata``."""
    charters = Table(
        "charters",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("revision", Integer, nullable=False),
        Column("body", JSONB, nullable=False),
        Column("worker_id", BigInteger),  # the worker its night runs go to, or NULL for a pool (0149)
        Column("pool_id", BigInteger),  # the pool whose ready nodes take them, or NULL for a worker (0149)
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="charters_created_by_fkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="charters_project_id_fkey"),
        ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="RESTRICT", name="charters_worker_id_fkey"),
        ForeignKeyConstraint(["pool_id"], ["worker_pools.id"], ondelete="RESTRICT", name="charters_pool_id_fkey"),
        _one_of("charters"),
        PrimaryKeyConstraint("project_id", "revision", name="charters_pkey"),
    )
    schedules = Table(
        "schedules",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("owner_id", BigInteger, nullable=False),
        Column("worker_id", BigInteger),  # NULL for a pool (0149)
        Column("pool_id", BigInteger),  # NULL for a worker (0149)
        _stamp("created_at"),
        _stamp("updated_at"),
        Column("paused_at", DateTime(timezone=True)),
        Column("paused_by", BigInteger),
        Column("pause_reason", Text),  # why the hub paused it, its circuit breaker; NULL when a member did (0018)
        ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="schedules_owner_id_fkey"),
        ForeignKeyConstraint(["paused_by"], ["users.id"], ondelete="RESTRICT", name="schedules_paused_by_fkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="schedules_project_id_fkey"),
        ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="RESTRICT", name="schedules_worker_id_fkey"),
        ForeignKeyConstraint(["pool_id"], ["worker_pools.id"], ondelete="RESTRICT", name="schedules_pool_id_fkey"),
        _one_of("schedules"),
        PrimaryKeyConstraint("id", name="schedules_pkey"),
        UniqueConstraint("project_id", "kind", name="schedules_project_id_kind_key"),
    )
    return charters, schedules
