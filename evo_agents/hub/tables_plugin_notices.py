"""The table of the notices an isolated plugin sends (0097), which ``evo_agents.hub.tables`` adds to its metadata as
``tables.plugin_notice_keys``.

A plugin that sends a notice with a ``key`` (``POST /v1/plugin-api/projects/{p}/notifications``) gets a row here, in the
same transaction as the notifications: the unique key (project, plugin, key) is what makes a second send with the same
key create nothing and answer ``result``, the answer of the first.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB


def plugin_notice_tables(metadata: MetaData) -> Table:
    """plugin_notice_keys (0097), added to ``metadata``."""
    return Table(
        "plugin_notice_keys",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("plugin_id", BigInteger, nullable=False),
        Column("key", Text, nullable=False),
        Column("result", JSONB, nullable=False),
        Column("sent_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="plugin_notice_keys_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["plugin_id"], ["plugins.id"], ondelete="CASCADE", name="plugin_notice_keys_plugin_id_fkey"
        ),
        PrimaryKeyConstraint("project_id", "plugin_id", "key", name="plugin_notice_keys_pkey"),
    )
