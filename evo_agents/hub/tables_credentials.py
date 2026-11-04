"""The tables of credentials (0011), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.secrets``, ``tables.secret_bindings``, ``tables.credential_leases``): queries name them there.

secrets holds each sealed secret of a member or of an agent profile (0104), secret_bindings the projects, workers and
pools (0150) a secret is bound to, and credential_leases each credential a run was handed, with its sealed value
while the worker has not fetched it and the run's own hub token for a lease of provider run-access (0166).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    column,
    func,
    or_,
)


def _stamp(name: str) -> Column:
    """A timestamptz column that defaults to now() and is never NULL."""
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _when(name: str) -> Column:
    """A timestamptz column that is NULL until it happens."""
    return Column(name, DateTime(timezone=True))


def _id() -> Column:
    """``id bigint GENERATED ALWAYS AS IDENTITY``."""
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def credential_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """secrets, secret_bindings and credential_leases (0011), added to ``metadata``."""
    secrets = Table(
        "secrets",
        metadata,
        _id(),
        Column("owner_id", BigInteger),  # a member's secret; null for one an agent profile owns (0104)
        Column("name", Text, nullable=False),
        Column("kind", Text, nullable=False),
        Column("env_var", Text),
        Column("url_prefix", Text),
        Column("username", Text),
        Column("sealed", LargeBinary),
        Column("nonce", LargeBinary),
        Column("key_id", Text),
        _stamp("created_at"),
        _stamp("updated_at"),
        _when("expires_at"),
        _when("deleted_at"),
        Column("profile_id", BigInteger),  # 0104: the agent profile that owns it, null for a member's
        ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT", name="secrets_owner_id_fkey"),
        ForeignKeyConstraint(
            ["profile_id"], ["agent_profiles.id"], ondelete="RESTRICT", name="secrets_profile_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="secrets_pkey"),
    )
    Index(
        "secrets_name_key",
        secrets.c.owner_id,
        secrets.c.name,
        unique=True,
        postgresql_where=secrets.c.deleted_at.is_(None),
    )
    Index(  # 0104: an agent's secret is unique by (profile, name) among the live ones
        "secrets_profile_name_key",
        secrets.c.profile_id,
        secrets.c.name,
        unique=True,
        postgresql_where=secrets.c.deleted_at.is_(None),
    )

    secret_bindings = Table(
        "secret_bindings",
        metadata,
        Column("secret_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("worker_id", BigInteger),
        Column("pool_id", BigInteger),  # bound to each node of the pool, those that join later too (0150)
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="secret_bindings_project_id_fkey"
        ),
        ForeignKeyConstraint(["secret_id"], ["secrets.id"], ondelete="CASCADE", name="secret_bindings_secret_id_fkey"),
        ForeignKeyConstraint(["worker_id"], ["workers.id"], ondelete="CASCADE", name="secret_bindings_worker_id_fkey"),
        ForeignKeyConstraint(["pool_id"], ["worker_pools.id"], ondelete="CASCADE", name="secret_bindings_pool_id_fkey"),
        CheckConstraint(
            or_(column("worker_id").is_(None), column("pool_id").is_(None)), name="secret_bindings_worker_or_pool_check"
        ),
        Index("secret_bindings_project_idx", "project_id"),
    )
    Index(
        "secret_bindings_key",
        secret_bindings.c.secret_id,
        secret_bindings.c.project_id,
        func.coalesce(secret_bindings.c.worker_id, 0),
        func.coalesce(secret_bindings.c.pool_id, 0),
        unique=True,
    )
    Index(
        "secret_bindings_pool_idx",
        secret_bindings.c.pool_id,
        postgresql_where=secret_bindings.c.pool_id.is_not(None),
    )
    Index(
        "secret_bindings_worker_idx",
        secret_bindings.c.worker_id,
        postgresql_where=secret_bindings.c.worker_id.is_not(None),
    )

    credential_leases = Table(
        "credential_leases",
        metadata,
        _id(),
        Column("run_id", BigInteger, nullable=False),
        Column("worker_id", BigInteger, nullable=False),
        Column("secret_id", BigInteger),
        Column("provider", Text, nullable=False),
        Column("target", Text, nullable=False),
        Column("external_id", Text),
        _stamp("issued_at"),
        _when("expires_at"),
        _when("revoked_at"),
        Column("sealed_value", LargeBinary),
        Column("nonce", LargeBinary),
        Column("key_id", Text),
        Column("token_id", BigInteger),  # the run's own token of the hub, of a lease of provider run-access (0166)
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE", name="credential_leases_run_id_fkey"),
        ForeignKeyConstraint(
            ["secret_id"], ["secrets.id"], ondelete="RESTRICT", name="credential_leases_secret_id_fkey"
        ),
        ForeignKeyConstraint(
            ["worker_id"], ["workers.id"], ondelete="RESTRICT", name="credential_leases_worker_id_fkey"
        ),
        ForeignKeyConstraint(["token_id"], ["tokens.id"], ondelete="RESTRICT", name="credential_leases_token_id_fkey"),
        PrimaryKeyConstraint("id", name="credential_leases_pkey"),
        Index("credential_leases_run_idx", "run_id", "id"),
    )
    Index(
        "credential_leases_token_id_idx",
        credential_leases.c.token_id,
        unique=True,
        postgresql_where=credential_leases.c.token_id.is_not(None),
    )
    Index(
        "credential_leases_worker_idx",
        credential_leases.c.worker_id,
        postgresql_where=credential_leases.c.revoked_at.is_(None),
    )
    Index(
        "credential_leases_secret_idx",
        credential_leases.c.secret_id,
        postgresql_where=credential_leases.c.revoked_at.is_(None),
    )
    Index(
        "credential_leases_sealed_idx",
        credential_leases.c.expires_at,
        postgresql_where=credential_leases.c.sealed_value.is_not(None),
    )
    return secrets, secret_bindings, credential_leases
