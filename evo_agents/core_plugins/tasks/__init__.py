"""The core plugin tasks: a project's backlog on the hub, with sprints, people and links (criterion a3 of plan
srs-tasks-plugins; ``docs/plugins.md``, Core plugins).

A task has a number in the project (shown with the config's prefix, ``T-42``), a type, a title, a description, a
status from the project's list of statuses, a priority, a due date, a sprint and a parent; its assignees and its BA,
QA and Dev, each a member with a grant on the project; and links: to a coded item of the plugin srs while the project
enables it (``UC ORD-3``, ``BR ORD3-2``, a screen, an NFR), to a step of a plan, to a run, and to a pull or merge
request. A step's status is read from the plan each time a task is shown, never copied; when a step turns done, the
hub's ``plan.step`` event (granted ``plans:read``) writes it into the history of every task linked to it, and the
config's ``on_steps_done`` moves a task to a status once every step it links is done. Every change of a task is a row
of its history, with who made it, or the hub and why.

The routes are under /v1/x/tasks/<project>/ (``server``) for members with a grant on a project that enabled the
plugin; readers read, writers write. ``evo-agents hub task list|show|create|edit|import`` is their client; an import
reads ``evo-tasks/1`` JSON once, linking its tasks to use cases by code. The tables are the hub's
(``evo_agents.hub.tables_tasks``, migration 0047), every task carrying its label, the project's by default. Every task
is a Ticket of the project's knowledge graph (``kg``), which implements the use cases and requirements it links to and
is implemented by the plan steps it links to.

The hub imports this package for the manifest only: ``settings`` reads the config, ``store`` reads tasks, ``links``
resolves and shows what they link to, ``writes`` keeps them, ``imports`` reads a file, ``events`` takes the plan
steps, ``models`` and ``server`` are the API, ``kg`` gives the knowledge graph the tasks.
"""

SERVER = "evo_agents.core_plugins.tasks.server"
EVENTS = "evo_agents.core_plugins.tasks.events"
KG = "evo_agents.core_plugins.tasks.kg"
GROUPS = ("todo", "doing", "review", "done", "cancelled")  # what a status means, whatever a project names it
FINISHED = ("done", "cancelled")  # the groups of a task nobody works on any more
DEFAULT_STATUSES = (
    {"key": "backlog", "name": "Backlog", "group": "todo"},
    {"key": "todo", "name": "To do", "group": "todo"},
    {"key": "in_progress", "name": "In progress", "group": "doing"},
    {"key": "in_review", "name": "In review", "group": "review"},
    {"key": "done", "name": "Done", "group": "done"},
    {"key": "cancelled", "name": "Cancelled", "group": "cancelled"},
)
DEFAULT_TYPES = ("task", "bug", "story")
PREFIX = r"^[A-Z][A-Z0-9]{0,9}$"
KEY = r"^[a-z][a-z0-9_]{0,39}$"  # evo_agents.hub.tables_tasks.KEY
PRIORITIES = ("urgent", "high", "medium", "low")  # evo_agents.hub.tables_tasks.PRIORITIES

_KEY = {"type": "string", "pattern": KEY}

CONFIG = {
    "type": "object",
    "properties": {
        "prefix": {
            "title": "Prefix",
            "description": "What a task's number is shown after, as T in T-42.",
            "type": "string",
            "pattern": PREFIX,
            "default": "T",
        },
        "statuses": {
            "title": "Statuses",
            "description": "The statuses of a task, in the order of the board: a key, a name and a group (todo, doing, "
            "review, done, cancelled). A new task takes the first.",
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "name", "group"],
                "properties": {
                    "key": _KEY,
                    "name": {"type": "string", "minLength": 1, "maxLength": 60},
                    "group": {"enum": list(GROUPS)},
                },
            },
            "default": [dict(status) for status in DEFAULT_STATUSES],
        },
        "types": {
            "title": "Types",
            "description": "The types a task may have; a new task takes the first.",
            "type": "array",
            "minItems": 1,
            "items": _KEY,
            "default": list(DEFAULT_TYPES),
        },
        "on_steps_done": {
            "title": "When its plan steps are done",
            "description": "The status a task moves to once every plan step it links is done; left unset, a task "
            "shows the steps' statuses and keeps its own.",
            "type": "string",
            "pattern": KEY,
        },
    },
}

TABLES = (
    ("tasks_sprints", "A project's sprints, at most one of them active"),
    ("tasks_items", "The tasks of a project and the label each carries"),
    ("tasks_people", "The assignees and the BA, QA and Dev of each task"),
    ("tasks_links", "What a task links to: an SRS item, a plan step, a run or a pull request"),
    ("tasks_events", "The history of each task"),
    ("tasks_comments", "Comments on a task"),
)

MANIFEST = {
    "id": "tasks",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Tasks",
    "description": "Backlog, sprints and people of a project's tasks, linked to SRS items, plan steps, runs and PRs.",
    "engines": {"evo-agents": ">=0.21"},
    "plugin_api": 1,
    "permissions": ["plans:read"],
    "config": CONFIG,
    "contributes": {
        "routes": [{"name": "tasks", "title": "The tasks of a project", "python": f"{SERVER}:router"}],
        "events": [{"name": "steps", "on": ["plan.step"], "python": f"{EVENTS}:on_plan_step"}],
        "kg_sources": [{"name": "backlog", "title": "The tasks of a project", "python": f"{KG}:kg_source"}],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
    },
}

__all__ = ["DEFAULT_STATUSES", "DEFAULT_TYPES", "FINISHED", "GROUPS", "MANIFEST", "PRIORITIES"]
