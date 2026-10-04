"""The tools of the hub's MCP endpoint, as ``tools/list`` names them: the server answers with this list, and the stdio
proxy ``evo-agents hub mcp`` answers with it too while the hub cannot be reached, so a session sees the same tools
either way.

The seven kg_* tools are ``evo_agents.kg.serve.TOOLS`` unchanged, names and schemas, so a session reads the graph of
its project the same way whether ``kg serve`` answers on the machine or the hub does. The eight others read and write
what the hub holds: memories, plans, skills and the caller's projects. Those about a project take an optional
``project``; without it the session's project counts (X-Evo-Project, which the proxy sends). Their arguments are
checked against these schemas before anything is read.

Standard library only, besides ``evo_agents.kg.serve`` and the hub's pure modules: the proxy runs on a core install.
"""

from __future__ import annotations

from evo_agents.hub.memory import TYPES
from evo_agents.hub.plans import AREAS, STEP_STATUSES
from evo_agents.kg.serve import INSTRUCTIONS as KG_INSTRUCTIONS
from evo_agents.kg.serve import TOOLS as KG_TOOLS

SERVER_NAME = "evo-hub"
PROJECT_HEADER = "X-Evo-Project"
SINK_HEADER = "X-Evo-Sink"
PROTOCOL_HEADER = "MCP-Protocol-Version"
# The versions served with the initialize handshake, oldest first. The last is what a client of this package asks for
# when it calls a tool without a handshake of its own, and what the proxy offers while the hub does not answer.
HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
PROTOCOL_VERSION = HANDSHAKE_VERSIONS[-1]
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,99}$"  # a project or plan id, as evo_agents.hub.server.admin.PROJECT_NAME
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MAX_REVISION = 2**31 - 1
MAX_ID = 2**63 - 1

INSTRUCTIONS = (
    "Team hub of the session's project: its knowledge graph, memories, plans and skills, read with your grant on the "
    "hub and the clearance of this session's sink. "
    + KG_INSTRUCTIONS
    + " Use memory_search before relying on what you remember about the project or the person, and memory_get for a "
    "memory's whole text. In a harness whose plans the hub manages, the YAML files under plans/ are read-only "
    "copies: read a plan with plan_show and mark a step with plan_step, never by editing the file. memory_write and "
    "plan_step write to the hub only; nothing is written on this machine."
)

_PROJECT = {
    "type": "string",
    "pattern": NAME_PATTERN,
    "description": "a project on the hub (see hub_projects); default: the session's project",
}
_STEP = {"type": ["string", "integer"], "description": "the step's id, as in the plan"}

HUB_TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Search the memories you can see on the hub by words of their name or text, best first. In a session "
            "bound to a project, that project's memories; scope personal searches your personal ones instead. "
            "Returns ids for memory_get."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": 'words, "a phrase", or -excluded',
                },
                "project": _PROJECT,
                "scope": {"type": "string", "enum": ["project", "personal"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_get",
        "description": "One memory by its id: the whole file, frontmatter included, with its type, label and revision.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "minimum": 1, "maximum": MAX_ID}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_write",
        "description": (
            "Create or change one memory on the hub, as a file of Claude Code's auto-memory: name is the file name "
            "(ending in .md), body the whole file with its frontmatter (name, description, metadata.type). To change "
            "a memory, pass the revision memory_get showed as if_revision. Writes to the hub only, never to this "
            "machine: the memory directory gets it at the next `evo-agents hub memory pull`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 4, "maxLength": 255, "description": "such as topic-notes.md"},
                "body": {"type": "string", "description": "the whole file, frontmatter included"},
                "type": {
                    "type": "string",
                    "enum": list(TYPES),
                    "description": "default: metadata.type of the frontmatter, else user (only you see it)",
                },
                "project": _PROJECT,
                "scope": {
                    "type": "string",
                    "enum": ["project", "personal"],
                    "description": "default: project when there is one",
                },
                "location": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "description": "harness (default) or a repo of the project; for scope personal, required",
                },
                "if_revision": {"type": "integer", "minimum": 1, "maximum": MAX_REVISION},
                "label": {
                    "type": "object",
                    "properties": {
                        "level": {"type": "string", "minLength": 1, "maxLength": 100},
                        "location": {"type": "string", "minLength": 1, "maxLength": 100},
                        "integrity": {"type": "string", "enum": ["T", "U"]},
                    },
                    "required": ["level"],
                    "additionalProperties": False,
                    "description": "default: the memory's current label, else the project's",
                },
            },
            "required": ["name", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "plan_list",
        "description": "The plans of a project on the hub with their revision and how many steps are done.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": _PROJECT, "area": {"type": "string", "enum": list(AREAS)}},
            "additionalProperties": False,
        },
    },
    {
        "name": "plan_show",
        "description": (
            "A plan as the hub holds it, as the YAML of its copy in git, with its revision; with step, that step only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "pattern": NAME_PATTERN},
                "step": _STEP,
                "project": _PROJECT,
            },
            "required": ["plan_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "plan_step",
        "description": (
            "Set the status of a plan step on the hub, as `evo harness step` does, with evidence or a note. done "
            "also sets done_at (default: today). Writes to the hub only: the YAML copy in git changes at the next "
            "`evo-agents hub plan export`, never by editing it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "pattern": NAME_PATTERN},
                "step": _STEP,
                "status": {"type": "string", "enum": list(STEP_STATUSES)},
                "evidence": {"type": "string", "minLength": 1, "maxLength": 65536},
                "note": {"type": "string", "minLength": 1, "maxLength": 65536},
                "done_at": {"type": "string", "pattern": DATE_PATTERN, "description": "YYYY-MM-DD, with status done"},
                "if_revision": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_REVISION,
                    "description": "the revision you read; default: the latest, retried when another write lands",
                },
                "project": _PROJECT,
            },
            "required": ["plan_id", "step", "status"],
            "additionalProperties": False,
        },
    },
    {
        "name": "skill_list",
        "description": "The skills on the hub you can see, global and of your projects, each with its latest version.",
        "inputSchema": {
            "type": "object",
            "properties": {"scope": {"type": "string", "enum": ["global", "project"]}, "project": _PROJECT},
            "additionalProperties": False,
        },
    },
    {
        "name": "hub_projects",
        "description": "The projects on the hub you hold a grant on, with your role, max level, sinks and repos.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

TOOLS = [*KG_TOOLS, *HUB_TOOLS]
KG_TOOL_NAMES = frozenset(tool["name"] for tool in KG_TOOLS)
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
