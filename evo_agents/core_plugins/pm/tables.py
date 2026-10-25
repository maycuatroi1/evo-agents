"""The table of the core plugin pm, in its own metadata (``schema.metadata`` of its manifest), carried by its own
chain of migrations (``migrations``), which adopts the table the hub's migration 0086 made. Its one key points at the
hub's projects (``HUB_KEYS``).

pm_sends holds one row for each thing pm sent a project on a day: its digest (kind ``digest``, ``key`` empty) or the
escalation of one blocker (kind ``escalation``, ``key`` names the blocker). The unique key (project, day, kind, key)
is what keeps the job from sending twice.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
)

from evo_agents.hub.server import core_plugin_api as hub

KINDS = ("digest", "escalation")
NAMES = ("pm_sends",)

metadata = hub.plugin_metadata()

pm_sends = Table(
    "pm_sends",
    metadata,
    Column("id", BigInteger, Identity(always=True), nullable=False),
    Column("project_id", BigInteger, nullable=False),
    Column("day", Date, nullable=False),
    Column("kind", Text, nullable=False),
    Column("key", Text, nullable=False, server_default=""),
    Column("sent_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="pm_sends_project_id_fkey"),
    PrimaryKeyConstraint("id", name="pm_sends_pkey"),
    UniqueConstraint("project_id", "day", "kind", "key", name="pm_sends_once_key"),
    CheckConstraint(f"kind IN ({', '.join(repr(k) for k in KINDS)})", name="pm_sends_kind_check"),
)

__all__ = ["KINDS", "NAMES", "metadata", "pm_sends"]
