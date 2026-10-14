"""The tables of agent profiles (0030), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.agent_profiles``, ``tables.agent_profile_versions``): queries name them there.

agent_profiles holds each profile as it is now (``evo_agents.hub.profiles``): its project, its handle (unique on the
hub, soft-deleted profiles included, so an old message keeps its author), name, description, instructions, colour,
avatar (a blob of kind agent-avatar the project holds), whether it is its project's default (one per project), its
permissions on the seven axes, its version, who created it and when, and when it was soft-deleted.
agent_profile_versions holds the profile as each version had it, written in the transaction that made the version.

The same revision gives runs and credential_leases a profile_id and profile_version: the profile a run was
dispatched under and the version whose rules the hub applies to it, and the profile a lease was handed out under.
``profile_tables`` adds those columns, their foreign key to the version and the index of a profile's runs to the
tables it is given, so that ``evo_agents.hub.tables`` describes runs and credential_leases whole.
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
from sqlalchemy.dialects.postgresql import JSONB


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _profile_of(table: Table) -> None:
    """Give ``table`` (runs or credential_leases) the profile and version it was made under, NULL for one made before
    0030, with a foreign key to the version."""
    table.append_column(Column("profile_id", BigInteger))
    table.append_column(Column("profile_version", Integer))
    table.append_constraint(
        ForeignKeyConstraint(
            ["profile_id", "profile_version"],
            ["agent_profile_versions.profile_id", "agent_profile_versions.version"],
            ondelete="RESTRICT",
            name=f"{table.name}_profile_fkey",
        )
    )


def profile_tables(metadata: MetaData, runs: Table, credential_leases: Table) -> tuple[Table, Table]:
    """(agent_profiles, agent_profile_versions), added to ``metadata``; the profile columns added to ``runs`` and
    ``credential_leases``."""
    agent_profiles = Table(
        "agent_profiles",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("handle", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("description", Text, nullable=False, server_default=""),
        Column("instructions", Text, nullable=False, server_default=""),
        Column("color", Text, nullable=False),
        Column("avatar_sha256", Text),
        Column("is_default", Boolean, nullable=False, server_default=false()),
        Column("permissions", JSONB, nullable=False),
        Column("version", Integer, nullable=False, server_default="1"),
        Column("created_by", BigInteger, nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        Column("deleted_at", DateTime(timezone=True)),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="agent_profiles_created_by_fkey"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT", name="agent_profiles_project_id_fkey"
        ),
        PrimaryKeyConstraint("id", name="agent_profiles_pkey"),
        UniqueConstraint("handle", name="agent_profiles_handle_key"),
        Index("agent_profiles_project_idx", "project_id", "handle"),
    )
    Index(  # a project has one default profile
        "agent_profiles_default_key",
        agent_profiles.c.project_id,
        unique=True,
        postgresql_where=agent_profiles.c.is_default,
    )
    agent_profile_versions = Table(
        "agent_profile_versions",
        metadata,
        Column("profile_id", BigInteger, nullable=False),
        Column("version", Integer, nullable=False),
        Column("body", JSONB, nullable=False),
        Column("changed_by", BigInteger, nullable=False),
        _stamp("changed_at"),
        ForeignKeyConstraint(
            ["changed_by"], ["users.id"], ondelete="RESTRICT", name="agent_profile_versions_changed_by_fkey"
        ),
        ForeignKeyConstraint(
            ["profile_id"], ["agent_profiles.id"], ondelete="RESTRICT", name="agent_profile_versions_profile_id_fkey"
        ),
        PrimaryKeyConstraint("profile_id", "version", name="agent_profile_versions_pkey"),
    )
    _profile_of(runs)
    _profile_of(credential_leases)
    Index(
        "runs_profile_idx",
        runs.c.profile_id,
        runs.c.id.desc(),
        postgresql_where=runs.c.profile_id.is_not(None),
    )
    return agent_profiles, agent_profile_versions
