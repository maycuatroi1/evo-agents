"""The core plugin pm: the daily report of a project's delivery, read from the hub's tables (criteria a3, a4 and a5 of
plan pm-daily-tracking; ``docs/pm-plugin.md``).

For a project that enables pm and tasks, the report of a day answers what was done on the working day before it (the
use cases and tasks that reached done), where the active sprint stands against what it held when it opened, which
tasks are late or at risk, how long tasks wait in review, which have had no activity for ``stale_days`` working days,
which are blocked and by what, what to escalate, and the load of each member. Days are counted in working days of the
project's time zone (``timezone``, ``working_days``). Nothing is written.

The route is ``GET /v1/x/pm/<project>/daily?date=`` (``server``); the tools ``pm__daily`` and ``pm__member`` of the
hub's /mcp (``agent``) answer the same data for the caller; ``evo-agents hub pm daily`` is the client of the route. pm
reads the tables of the plugins tasks and srs, the plans, runs and decisions of the hub itself and imports neither
plugin (import-linter contract ``pm-plugin-own-package``); the status groups and the prefix come from the config of the
plugin tasks in the project.

The hub imports this package for the manifest only: ``workdays`` counts days, ``settings`` reads the configs, ``reads``
reads the facts, ``sprint_view``, ``report`` and ``outcome`` compute from them (no I/O), ``service`` joins the two,
``models`` and ``server`` are the API, ``agent`` the tools.
"""

SERVER = "evo_agents.core_plugins.pm.server"
AGENT = "evo_agents.core_plugins.pm.agent"

_DAYS = {"type": "integer", "minimum": 0, "maximum": 365}

CONFIG = {
    "type": "object",
    "properties": {
        "pm": {
            "title": "Project manager",
            "description": "The login of the member who manages the project: who the report names to escalate to.",
            "type": "string",
            "maxLength": 100,
        },
        "timezone": {
            "title": "Time zone",
            "description": "The IANA time zone whose days the report counts, such as Asia/Ho_Chi_Minh.",
            "type": "string",
            "default": "Asia/Ho_Chi_Minh",
        },
        "working_days": {
            "title": "Working days",
            "description": "The ISO weekdays that count as working days, 1 (Monday) to 7 (Sunday).",
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1, "maximum": 7},
            "default": [1, 2, 3, 4, 5],
        },
        "stale_days": {
            "title": "Stale after",
            "description": "Working days without activity after which a task being worked on is stale.",
            "type": "integer",
            "minimum": 1,
            "maximum": 365,
            "default": 2,
        },
        "escalate_after_days": {
            "title": "Escalate after",
            "description": "Working days a task is blocked or late after which it is to escalate.",
            "type": "integer",
            "minimum": 0,
            "maximum": 365,
            "default": 1,
        },
        "at_risk_days": {
            "title": "At risk within",
            "description": "A task still to do whose due date is within this many working days is at risk.",
            "type": "integer",
            "minimum": 0,
            "maximum": 365,
            "default": 2,
        },
        "wip_limit": {
            "title": "Work in progress limit",
            "description": "Tasks a member may have in progress before the report calls them overloaded.",
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 2,
        },
        "digest_chat": {
            "title": "Digest chat",
            "description": "The id of the group chat the daily digest is posted to (optional); left unset, none is posted.",
            "type": "integer",
            "minimum": 1,
        },
    },
}

_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "description": "YYYY-MM-DD; today when left out"}
DAILY_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"date": _DATE}}
MEMBER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["login"],
    "properties": {
        "login": {"type": "string", "minLength": 1, "maxLength": 100, "description": "the member's login"},
        "date": _DATE,
    },
}

MANIFEST = {
    "id": "pm",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "PM",
    "description": "The daily report of a project: done, sprint, late, review, stale, blocked, escalate and load.",
    "engines": {"evo-agents": ">=0.21"},
    "plugin_api": 1,
    "permissions": ["notifications:send", "chats:write"],
    "config": CONFIG,
    "contributes": {
        "routes": [{"name": "pm", "title": "The daily report of a project", "python": f"{SERVER}:router"}],
        "mcp_tools": [
            {
                "name": "daily",
                "title": "Daily report",
                "description": "The daily report of the project for a day (today when left out): what was done, the "
                "sprint against what it held at opening, late, in review, stale, blocked, to escalate, and the load "
                "of each member, as the caller may see the tasks.",
                "python": f"{AGENT}:daily",
                "input_schema": DAILY_SCHEMA,
            },
            {
                "name": "member",
                "title": "Daily view of a member",
                "description": "The daily report as one member sees it: their tasks in progress, in review, late, "
                "stale and blocked, and their load, for a day (today when left out).",
                "python": f"{AGENT}:member",
                "input_schema": MEMBER_SCHEMA,
            },
        ],
    },
}

__all__ = ["MANIFEST"]
