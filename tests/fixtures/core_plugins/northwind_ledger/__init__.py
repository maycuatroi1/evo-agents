"""northwind-ledger, the sample core plugin of API 1.4 outside evo_agents: a ledger of what a project owes and is
owed, written against ``evo_agents.hub.server.core_plugin_api`` alone, as a plugin of another repository would be.

- its tables (``tables``) and its chain of migrations (``migrations``), which ``evo-agents hub migrate`` runs after
  the hub's own, recorded in ``alembic_version_northwind_ledger``: the first revision adopts the entries table an
  older chain made, rows untouched, the second adds the notes table;
- its routes (``server``) write its own tables with SQLAlchemy Core and reach the hub through the API: a caller's
  access to a project, the hub's reads, an event, a signal, a notice of its own kind, a join link, a build of the
  knowledge graph, a task for its agent type, the configs of all projects and the audit log;
- its page ``ledger`` (``ui/pages/ledger.html``) is framed in the hub's sandbox and talks to it through the bridge
  evo-plugin/1 with the hub's UI kit: no code of the hub's web;
- its commands ``evo-agents hub northwind-ledger list|add|remind``, which the CLI reads from the hub.
"""

from evo_agents.hub.server import core_plugin_api as hub

API = (1, 4)  # the version of the API this plugin is written for
PREFIX = "northwind_ledger_"
OVERDUE = PREFIX + "overdue"  # its notice kind
SERVER = "northwind_ledger.server"

MANIFEST = {
    "id": "northwind-ledger",
    "version": "1.0.0",
    "publisher": "northwind",
    "tier": "core",
    "name": "Northwind Ledger",
    "description": "What a project owes and is owed, with reminders of what is overdue.",
    "engines": {"evo-agents": ">=0.23.1"},
    "plugin_api": 1,
    "permissions": [],
    "schema": {
        "metadata": "northwind_ledger.tables:metadata",
        "migrations": "northwind_ledger.migrations",
        "adopts": ["northwind_ledger_entries"],
    },
    "ui": "northwind_ledger:ui",
    "contributes": {
        "routes": [{"name": "ledger", "title": "The ledger", "python": f"{SERVER}:router"}],
        "pages": [{"name": "ledger", "title": "Ledger", "entry": "pages/ledger.html", "icon": "table"}],
        "notice_kinds": [{"name": OVERDUE, "title": "An entry of the ledger is overdue"}],
        "agent_types": [
            {
                "name": "bookkeeper",
                "title": "Bookkeeper",
                "description": "Checks the ledger of a project",
                "profile": {
                    "handle": "bookkeeper",
                    "name": "Bookkeeper",
                    "description": "Checks the entries of the ledger",
                    "instructions": "Answer in a few plain sentences.",
                },
            }
        ],
        "cli": [
            {
                "name": "list",
                "help": "the entries of a project's ledger",
                "http": {"method": "GET", "path": "entries"},
                "project": True,
                "args": [{"name": "open", "type": "boolean", "in": "query", "help": "only those not paid"}],
                "output": {"json": "array", "table": ["id", "memo", "amount_cents", "due_on", "paid"]},
            },
            {
                "name": "add",
                "help": "add an entry to a project's ledger",
                "http": {"method": "POST", "path": "entries"},
                "project": True,
                "args": [
                    {"name": "memo", "type": "string", "in": "body", "positional": True, "required": True},
                    {"name": "amount_cents", "type": "integer", "in": "body", "required": True},
                    {"name": "due_on", "type": "string", "in": "body", "help": "YYYY-MM-DD"},
                ],
                "output": {"text": "id"},
            },
            {
                "name": "remind",
                "help": "send the members of a project a notice of what is overdue",
                "http": {"method": "POST", "path": "remind"},
                "project": True,
                "output": {"text": "sent"},
            },
        ],
    },
}


def manifest() -> dict:
    """The manifest, once this hub serves the version of the API the plugin is written for."""
    hub.require(*API)
    return MANIFEST
