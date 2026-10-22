"""The tables of accounts (0027), of members' emails (0068) and of credential shares (0100), which
``evo_agents.hub.tables`` adds to its metadata and exposes as its own attributes (``tables.accounts``,
``tables.account_policies``, ``tables.run_accounts``, ``tables.user_emails``, ``tables.credential_shares``,
``tables.credential_share_projects``): queries name them there.

accounts holds what a member set on one of their env secrets as an account, and where it stands: its weight and
priority, when its owner paused it, until when it cools down and why, when a run last took it, its place in the
rotation (``evo_agents.hub.accounts.pick``), and the last usage a run reported for it (0028), and for an account that signed in with OAuth the sealed set of its
tokens (0066: oauth_sealed, oauth_nonce, oauth_key_id, all null for any other account). A secret without a row
has the defaults. account_policies holds a member's policy for a project; none means DEFAULT_POLICY. run_accounts
holds the account each run took for each variable and how, so the run keeps it on every ask for its leases.

user_emails holds each email a member listed, or a hub admin listed for them, as it was written: one member an
address, whatever its case (user_emails_email_key, on lower(email)). ``evo_agents.hub.user_emails`` names the login of
an address from these rows, or from GitHub's noreply address, which needs none.

credential_shares holds each share an owner gave of a secret or an account (``evo_agents.hub.credential_shares``): to
one member, a unit of the org chart or everyone, with its level, budget, usage ceiling, most runs at once, end, the
moment its owner acknowledged the terms warning, and when it was revoked; never deleted. credential_share_projects
holds the projects each share is for. credential_leases (``evo_agents.hub.tables``) gains here who each lease was
made for and the share it came under (0101).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Double,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
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


def email_table(metadata: MetaData) -> Table:
    """user_emails, added to ``metadata``."""
    user_emails = Table(
        "user_emails",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("user_id", BigInteger, nullable=False),
        Column("email", Text, nullable=False),  # as it was written; compared in lower case
        _stamp("created_at"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="user_emails_user_id_fkey"),
        PrimaryKeyConstraint("id", name="user_emails_pkey"),
        Index("user_emails_user_idx", "user_id"),
    )
    Index("user_emails_email_key", func.lower(user_emails.c.email), unique=True)
    return user_emails


def account_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table]:
    """(accounts, account_policies, run_accounts, user_emails), added to ``metadata``."""
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
        Column("last_usage", JSONB),  # 0028: the last usage a run reported (rate_limits), never a value
        _when("last_usage_at"),
        Column("oauth_sealed", LargeBinary),  # 0066: the sealed set of an OAuth account's tokens
        Column("oauth_nonce", LargeBinary),
        Column("oauth_key_id", Text),
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
    return accounts, account_policies, run_accounts, email_table(metadata)


def share_tables(metadata: MetaData, credential_leases: Table) -> tuple[Table, Table]:
    """(credential_shares, credential_share_projects), added to ``metadata`` (0100); the principal and share columns
    added to ``credential_leases`` (0101)."""

    def key(table: str, column: str, target: str, ondelete: str) -> ForeignKeyConstraint:
        return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")

    shares = Table(
        "credential_shares",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("secret_id", BigInteger, nullable=False),
        Column("grantee_kind", Text, nullable=False),
        Column("grantee_user_id", BigInteger),
        Column("grantee_unit_id", BigInteger),
        Column("level", Text),
        Column("budget_usd", Numeric(12, 2)),
        Column("usage_ceiling_pct", SmallInteger),
        Column("max_running", Integer),
        _when("expires_at"),
        _when("terms_ack_at"),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        _when("revoked_at"),
        Column("revoked_by", BigInteger),
        key("credential_shares", "secret_id", "secrets", "CASCADE"),
        key("credential_shares", "grantee_user_id", "users", "CASCADE"),
        key("credential_shares", "grantee_unit_id", "org_units", "RESTRICT"),
        key("credential_shares", "created_by", "users", "RESTRICT"),
        key("credential_shares", "revoked_by", "users", "RESTRICT"),
        PrimaryKeyConstraint("id", name="credential_shares_pkey"),
        Index("credential_shares_user_idx", "grantee_user_id"),
        Index("credential_shares_unit_idx", "grantee_unit_id"),
    )
    Index("credential_shares_secret_idx", shares.c.secret_id, postgresql_where=shares.c.revoked_at.is_(None))
    projects = Table(
        "credential_share_projects",
        metadata,
        Column("share_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        key("credential_share_projects", "share_id", "credential_shares", "CASCADE"),
        key("credential_share_projects", "project_id", "projects", "CASCADE"),
        PrimaryKeyConstraint("share_id", "project_id", name="credential_share_projects_pkey"),
        Index("credential_share_projects_project_idx", "project_id"),
    )
    credential_leases.append_column(Column("principal_id", BigInteger))  # who the lease was made for
    credential_leases.append_column(Column("share_id", BigInteger))  # the share it came under; null for one's own
    credential_leases.append_constraint(key("credential_leases", "principal_id", "users", ondelete="SET NULL"))
    credential_leases.append_constraint(key("credential_leases", "share_id", "credential_shares", ondelete="SET NULL"))
    leases = credential_leases.c
    Index("credential_leases_share_idx", leases.share_id, postgresql_where=leases.share_id.is_not(None))
    return shares, projects
