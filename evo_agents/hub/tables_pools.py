"""The tables of worker pools (0147), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.worker_pools``, ``tables.worker_pool_projects``, ``tables.worker_join_tokens``): queries name them
there.

worker_pools holds each pool a member made for machines that join with a join token instead of a pairing code
(``evo_agents.hub.server.worker_pools``): its owner, its name (unique among the owner's pools in any case), the slots,
labels and taints every node takes, the codes of doctor findings the pool accepts (``evo_agents.worker.doctor``), the
days a node stays offline before the hub revokes it (1 to 90), and when it was made and last changed.
worker_pool_projects lists the projects the nodes of a pool serve. worker_join_tokens holds the join tokens of a pool:
the hex SHA-256 of the ``evj_`` token, never the token, who made it, when it expires, how many machines it takes at
most and how many joined with it, and when it was revoked.

Their CHECK constraints are the migration's alone. ``pool_tables`` also gives workers ``pool_id``, the pool a node joined, NULL for a worker paired or registered.
``placement_columns`` gives workers ``taints`` and runs ``selector`` and ``tolerations`` (0148), which place a run.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
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
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _id() -> Column:
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def _texts(name: str) -> Column:
    return Column(name, ARRAY(Text), nullable=False, server_default="{}")


def pools_table(metadata: MetaData) -> Table:
    pools = Table(
        "worker_pools",
        metadata,
        _id(),
        Column("owner_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("slots", SmallInteger, nullable=False, server_default="1"),
        _texts("labels"),
        _texts("taints"),
        _texts("accepted_findings"),
        Column("reap_after_days", SmallInteger, nullable=False, server_default="7"),
        _stamp("created_at"),
        _stamp("updated_at"),
        ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="worker_pools_owner_id_fkey"),
        PrimaryKeyConstraint("id", name="worker_pools_pkey"),
    )
    Index("worker_pools_name_key", pools.c.owner_id, func.lower(pools.c.name), unique=True)
    return pools


def pool_projects_table(metadata: MetaData) -> Table:
    return Table(
        "worker_pool_projects",
        metadata,
        Column("pool_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        ForeignKeyConstraint(
            ["pool_id"], ["worker_pools.id"], ondelete="CASCADE", name="worker_pool_projects_pool_id_fkey"
        ),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="worker_pool_projects_project_id_fkey"
        ),
        PrimaryKeyConstraint("pool_id", "project_id", name="worker_pool_projects_pkey"),
    )


def join_tokens_table(metadata: MetaData) -> Table:
    tokens = Table(
        "worker_join_tokens",
        metadata,
        _id(),
        Column("pool_id", BigInteger, nullable=False),
        Column("token_hash", Text, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("max_uses", SmallInteger, nullable=False),
        Column("uses", SmallInteger, nullable=False, server_default="0"),
        Column("last_used_at", DateTime(timezone=True)),
        Column("revoked_at", DateTime(timezone=True)),
        ForeignKeyConstraint(
            ["pool_id"], ["worker_pools.id"], ondelete="CASCADE", name="worker_join_tokens_pool_id_fkey"
        ),
        ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT", name="worker_join_tokens_created_by_fkey"
        ),
        PrimaryKeyConstraint("id", name="worker_join_tokens_pkey"),
        UniqueConstraint("token_hash", name="worker_join_tokens_token_hash_key"),
    )
    Index("worker_join_tokens_pool_idx", tokens.c.pool_id)
    return tokens


def pool_tables(metadata: MetaData, workers: Table) -> tuple[Table, Table, Table]:
    """(worker_pools, worker_pool_projects, worker_join_tokens), added to ``metadata``, and the pool_id of
    ``workers``."""
    pools = pools_table(metadata)
    workers.append_column(Column("pool_id", BigInteger))
    workers.append_constraint(
        ForeignKeyConstraint(["pool_id"], ["worker_pools.id"], ondelete="RESTRICT", name="workers_pool_id_fkey")
    )
    Index("workers_pool_idx", workers.c.pool_id, postgresql_where=workers.c.pool_id.is_not(None))
    return pools, pool_projects_table(metadata), join_tokens_table(metadata)


def placement_columns(workers: Table, runs: Table) -> None:
    """The taints of ``workers`` and the selector and tolerations of ``runs`` (0148): the labels a run asks of the
    worker that claims it and the taints it tolerates."""
    workers.append_column(_texts("taints"))
    runs.append_column(_texts("selector"))
    runs.append_column(_texts("tolerations"))
