"""The tables of the hub's own settings (0037), of the proposals of @builder (0043) and of the autonomy policies of
projects (0079), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own attributes
(``tables.hub_settings``, ``tables.builder_proposals``, ``tables.autonomy_policies``): queries name them there.

hub_settings holds every version of each layer of a setting a hub admin changes, never changed: its kind (branding,
the hub's alone; features, the hub's own layer or a project's), the project of a project's layer (NULL for the
hub's), the version (1 and up within the layer), the values it sets (an object), the version a rollback copied, who
wrote it and when. A layer's current version is its highest (``evo_agents.hub.server.hub_settings``).

builder_proposals holds each change of a setting the agent of a chat run of @builder proposed in a project (the core
plugin builder, ``evo_agents.core_plugins.builder``): its kind, where it applies and what it changes, the version of
the setting it was proposed on, the run, chat and thread it came from, and what became of it: pending until an admin
approves or rejects it, applied as the setting's next version, failed with why, or rolled back.

autonomy_policies holds every revision of each project's autonomy policy (``evo_agents.hub.autonomy``), never
changed: the project, the revision (1 and up), the keys it sets (an object), the writer's note, who wrote it and
when. A project's policy is its highest revision (``evo_agents.hub.server.autonomy_policies``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

KINDS = ("branding", "features")  # hub_settings.kind (0037)
PROPOSAL_KINDS = ("branding", "features", "plugin_config", "agent", "policy")  # builder_proposals.kind (0043, 0079)
PROPOSAL_STATES = ("pending", "applied", "rejected", "failed", "rolled_back")  # builder_proposals.state (0043)


def settings_table(metadata: MetaData) -> Table:
    """hub_settings, added to ``metadata``."""
    return Table(
        "hub_settings",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("kind", Text, nullable=False),
        Column("project_id", BigInteger),
        Column("version", Integer, nullable=False),
        Column("values", JSONB, nullable=False, server_default="{}"),
        Column("rolled_back_to", Integer),
        Column("changed_by", BigInteger, nullable=False),
        Column("changed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT", name="hub_settings_project_id_fkey"),
        ForeignKeyConstraint(["changed_by"], ["users.id"], ondelete="RESTRICT", name="hub_settings_changed_by_fkey"),
        PrimaryKeyConstraint("id", name="hub_settings_pkey"),
        UniqueConstraint(
            "kind", "project_id", "version", name="hub_settings_version_key", postgresql_nulls_not_distinct=True
        ),
    )


def _proposal_key(column: str, table: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{table}.id"], ondelete=ondelete, name=f"builder_proposals_{column}_fkey")


def proposals_table(metadata: MetaData) -> Table:
    """builder_proposals, added to ``metadata``."""
    return Table(
        "builder_proposals",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("run_id", BigInteger),
        Column("chat_id", BigInteger),
        Column("thread_root_id", BigInteger),
        Column("message_id", BigInteger),  # the hub's message that shows the proposal in the thread
        Column("kind", Text, nullable=False),
        Column("target", JSONB, nullable=False, server_default="{}"),
        Column("change", JSONB, nullable=False),
        Column("summary", Text, nullable=False),
        Column("base_version", Integer, nullable=False),
        Column("state", Text, nullable=False, server_default="pending"),
        Column("proposed_by", BigInteger, nullable=False),
        Column("proposed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("decided_by", BigInteger),
        Column("decided_at", DateTime(timezone=True)),
        Column("applied_version", Integer),
        Column("rollback_version", Integer),
        Column("error", Text),
        _proposal_key("project_id", "projects", ondelete="RESTRICT"),
        _proposal_key("run_id", "runs", ondelete="SET NULL"),
        _proposal_key("chat_id", "chats", ondelete="SET NULL"),
        _proposal_key("thread_root_id", "chat_messages", ondelete="SET NULL"),
        _proposal_key("message_id", "chat_messages", ondelete="SET NULL"),
        _proposal_key("proposed_by", "users", ondelete="RESTRICT"),
        _proposal_key("decided_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="builder_proposals_pkey"),
        Index("builder_proposals_project_idx", "project_id", "id"),
        Index("builder_proposals_run_idx", "run_id"),
    )


def policies_table(metadata: MetaData) -> Table:
    """autonomy_policies, added to ``metadata``."""
    return Table(
        "autonomy_policies",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("revision", Integer, nullable=False),
        Column("body", JSONB, nullable=False, server_default="{}"),
        Column("note", Text),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="autonomy_policies_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT", name="autonomy_policies_created_by_fkey"
        ),
        PrimaryKeyConstraint("project_id", "revision", name="autonomy_policies_pkey"),
    )


def settings_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """hub_settings, builder_proposals and autonomy_policies, added to ``metadata``."""
    return settings_table(metadata), proposals_table(metadata), policies_table(metadata)
