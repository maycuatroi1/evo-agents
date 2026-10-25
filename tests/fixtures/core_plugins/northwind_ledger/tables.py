"""The tables of northwind-ledger, each named with its prefix, its keys pointing at the hub's projects and users."""

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Table, Text, false, func

from evo_agents.hub.server import core_plugin_api as hub

metadata = hub.plugin_metadata()

entries = Table(
    "northwind_ledger_entries",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("memo", Text, nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("due_on", Date),
    Column("paid", Boolean, nullable=False, server_default=false()),
    Column("created_by", BigInteger, ForeignKey("users.id", ondelete="SET NULL")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

notes = Table(
    "northwind_ledger_notes",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column(
        "entry_id",
        BigInteger,
        ForeignKey("northwind_ledger_entries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("text", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
