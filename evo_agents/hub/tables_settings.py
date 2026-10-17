"""The table of the hub's own settings (0036), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its
own attribute (``tables.hub_settings``): queries name it there.

hub_settings holds every version of each layer of a setting a hub admin changes, never changed: its kind (branding,
the hub's alone; features, the hub's own layer or a project's), the project of a project's layer (NULL for the
hub's), the version (1 and up within the layer), the values it sets (an object), the version a rollback copied, who
wrote it and when. A layer's current version is its highest (``evo_agents.hub.server.hub_settings``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

KINDS = ("branding", "features")  # hub_settings.kind (0036)


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
