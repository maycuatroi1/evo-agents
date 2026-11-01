"""The tables of account pools (0191), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.account_pools``, ``tables.account_pool_members``): queries name them there.

An account pool is a named set of accounts that an admin of a project (or of the hub) keeps for the agents of the
project (``evo_agents.hub.server.account_pools``). account_pools holds each pool: its project, its name (unique in the
project in any case) and who made it. account_pool_members lists its accounts, each a secret (``secrets``) of kind env
or provider: who added it and when. ``agent_profiles.account_pool_id`` names the one account pool of an agent; many
agents may name the same pool, and a pool in use cannot be deleted.

Their CHECK constraints are the migration's alone.
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
    Table,
    Text,
    func,
)


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def account_pool_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(account_pools, account_pool_members), added to ``metadata``."""
    pools = Table(
        "account_pools",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("name", Text, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="account_pools_project_id_fkey"),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="account_pools_created_by_fkey"),
        PrimaryKeyConstraint("id", name="account_pools_pkey"),
    )
    Index("account_pools_name_key", pools.c.project_id, func.lower(pools.c.name), unique=True)
    members = Table(
        "account_pool_members",
        metadata,
        Column("pool_id", BigInteger, nullable=False),
        Column("secret_id", BigInteger, nullable=False),
        Column("added_by", BigInteger, nullable=False),
        _stamp("added_at"),
        ForeignKeyConstraint(
            ["pool_id"], ["account_pools.id"], ondelete="CASCADE", name="account_pool_members_pool_id_fkey"
        ),
        ForeignKeyConstraint(
            ["secret_id"], ["secrets.id"], ondelete="CASCADE", name="account_pool_members_secret_id_fkey"
        ),
        ForeignKeyConstraint(
            ["added_by"], ["users.id"], ondelete="RESTRICT", name="account_pool_members_added_by_fkey"
        ),
        PrimaryKeyConstraint("pool_id", "secret_id", name="account_pool_members_pkey"),
        Index("account_pool_members_secret_idx", "secret_id"),
    )
    return pools, members
