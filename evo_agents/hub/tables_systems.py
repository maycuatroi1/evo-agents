"""The tables of agent systems (0217), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.agent_systems``, ``tables.agent_system_revisions``): queries name them there.

agent_systems holds each agent system of a project as it is now (``evo_agents.hub.system_spec``): its slug (unique in
the project), name, spec, revision, the profile that is its entry agent (NULL for none; a profile is the entry agent of
at most one system, which ``agent_systems_entry_profile_key`` holds), who made it and changed it last, and when, and
its ops chat (``ops_chat_id``, 0221), where the hub opens the threads to its Orchestrator, whether it is the
project's default system (``is_default``, at most one per project: ``agent_systems_default_key``, 0240) and when its
ops were paused and why (0240).
agent_system_revisions holds the spec as each revision had it, written in the transaction that made the revision; a
system's revisions go with it when it is deleted.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"  # system_spec.SLUG_PATTERN; a test keeps the two equal


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def system_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(agent_systems, agent_system_revisions), added to ``metadata``."""
    name = "agent_systems"
    systems = Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("slug", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("spec", JSONB, nullable=False),
        Column("revision", Integer, nullable=False, server_default="1"),
        Column("entry_profile_id", BigInteger),
        Column("created_by", BigInteger),
        Column("updated_by", BigInteger),
        Column("ops_chat_id", BigInteger),  # the chat of the threads to its Orchestrator (0221)
        _stamp("created_at"),
        _stamp("updated_at"),
        Column("is_default", Boolean, nullable=False, server_default=false()),  # 0240
        Column("ops_paused_at", DateTime(timezone=True)),  # 0240
        Column("ops_paused_reason", Text),  # 0240
        Column(
            "ops_failures", Integer, nullable=False, server_default="0"
        ),  # ops runs in a row failed of the agent (0241)
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name=f"{name}_project_id_fkey"),
        ForeignKeyConstraint(
            ["entry_profile_id"], ["agent_profiles.id"], ondelete="SET NULL", name=f"{name}_entry_profile_id_fkey"
        ),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL", name=f"{name}_created_by_fkey"),
        ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL", name=f"{name}_updated_by_fkey"),
        ForeignKeyConstraint(["ops_chat_id"], ["chats.id"], ondelete="SET NULL", name=f"{name}_ops_chat_id_fkey"),
        PrimaryKeyConstraint("id", name=f"{name}_pkey"),
        UniqueConstraint("project_id", "slug", name=f"{name}_project_id_slug_key"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name=f"{name}_slug_check"),
        CheckConstraint("jsonb_typeof(spec) = 'object'", name=f"{name}_spec_check"),
    )
    Index(  # a profile is the entry agent of at most one system
        f"{name}_entry_profile_key",
        systems.c.entry_profile_id,
        unique=True,
        postgresql_where=systems.c.entry_profile_id.is_not(None),
    )
    Index(f"{name}_default_key", systems.c.project_id, unique=True, postgresql_where=systems.c.is_default)
    rname = "agent_system_revisions"
    revisions = Table(
        rname,
        metadata,
        Column("system_id", BigInteger, nullable=False),
        Column("revision", Integer, nullable=False),
        Column("spec", JSONB, nullable=False),
        Column("actor_id", BigInteger),
        _stamp("created_at"),
        ForeignKeyConstraint(["system_id"], ["agent_systems.id"], ondelete="CASCADE", name=f"{rname}_system_id_fkey"),
        ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL", name=f"{rname}_actor_id_fkey"),
        PrimaryKeyConstraint("system_id", "revision", name=f"{rname}_pkey"),
    )
    return systems, revisions
