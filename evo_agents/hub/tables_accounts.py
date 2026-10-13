"""The tables of accounts (0026), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.accounts``, ``tables.account_policies``, ``tables.run_accounts``): queries name them there.

accounts holds what a member set on one of their env secrets as an account, and where it stands: its weight and
priority, when its owner paused it, until when it cools down and why, when a run last took it, its place in the
rotation (``evo_agents.hub.accounts.pick``), and the last usage a run reported for it (0027). A secret without a row
has the defaults. account_policies holds a member's policy for a project; none means DEFAULT_POLICY. run_accounts
holds the account each run took for each variable and how, so the run keeps it on every ask for its leases.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Double,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def account_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """(accounts, account_policies, run_accounts), added to ``metadata``."""
    accounts = Table(
        "accounts",
        metadata,
        Column("secret_id", BigInteger, nullable=False),
        Column("weight", Integer, nullable=False, server_default="1"),
        Column("priority", Integer, nullable=False, server_default="0"),
        _when("paused_at"),
        _when("cooldown_until"),
        Column("cooldown_reason", Text),
        _when("last_leased_at"),
        Column("rotation", Double, nullable=False, server_default="0"),
        _stamp("updated_at"),
        Column("last_usage", JSONB),  # 0027: the last usage a run reported (rate_limits), never a value
        _when("last_usage_at"),
        ForeignKeyConstraint(["secret_id"], ["secrets.id"], ondelete="CASCADE", name="accounts_secret_id_fkey"),
        PrimaryKeyConstraint("secret_id", name="accounts_pkey"),
    )
    account_policies = Table(
        "account_policies",
        metadata,
        Column("owner_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("strategy", Text, nullable=False, server_default="round-robin"),
        Column("cooldown_seconds", Integer, nullable=False, server_default="3600"),
        Column("failover", Boolean, nullable=False, server_default=true()),
        Column("max_failovers", SmallInteger, nullable=False, server_default="3"),
        _stamp("updated_at"),
        ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="account_policies_owner_id_fkey"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="account_policies_project_id_fkey"
        ),
        PrimaryKeyConstraint("owner_id", "project_id", name="account_policies_pkey"),
    )
    run_accounts = Table(
        "run_accounts",
        metadata,
        Column("run_id", BigInteger, nullable=False),
        Column("env_var", Text, nullable=False),
        Column("secret_id", BigInteger, nullable=False),
        Column("how", Text, nullable=False),
        _stamp("chosen_at"),
        Column("failovers", SmallInteger, nullable=False, server_default="0"),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="run_accounts_run_id_fkey"),
        ForeignKeyConstraint(["secret_id"], ["secrets.id"], ondelete="RESTRICT", name="run_accounts_secret_id_fkey"),
        PrimaryKeyConstraint("run_id", "env_var", name="run_accounts_pkey"),
        Index("run_accounts_secret_idx", "secret_id"),
    )
    return accounts, account_policies, run_accounts
