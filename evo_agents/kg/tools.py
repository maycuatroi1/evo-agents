"""The kg tools as MCP lists them: names, descriptions and input schemas, the instructions a server sends with its
handshake, and the argument names each tool takes. ``kg serve`` (``evo_agents.kg.serve``) answers them on this machine;
the hub lists them unchanged (``evo_agents.hub.mcp_tools``) and answers them with the same code.

The instructions and descriptions name only what a graph holds: the statuses its stages give (``parsed`` and
``resolved``) and the forms of its ids.
"""

from __future__ import annotations

IMPACT_LIMIT = 20  # dependents kg_impact lists per hop, and a quarter as many documents, unless a call passes limit
IMPACT_MAX_LIMIT = 500

INSTRUCTIONS = (
    "Knowledge graph of the bound project: repos, files, symbols, documents and sections, plans and steps, "
    "seams, and the explicit links between them, each with provenance and a sensitivity label. Use kg_search "
    "or kg_context before grepping for a named requirement, plan, document, seam or feature; use grep for exact "
    "strings and code you just edited. Before editing, renaming or changing the signature of a symbol or file, call "
    "kg_impact (ids, repo:path or a diff) and check every dependent it lists, then grep for what its coverage note "
    "says the seed's language lacks. kg_path traces a requirement to its steps, code and tests, or shows how two "
    "nodes connect. Ids read repo:path, repo:path::Symbol and repo:path#section. Status 'parsed' was read from a "
    "source; 'resolved' matched a name to one target and can be wrong. The graph can lag the sources: read the live "
    "file (kg_node gives its uri) before quoting or editing it."
)

TOOLS = [
    {
        "name": "kg_search",
        "description": "Find nodes by name, id, path, code or words. Returns ids to pass to kg_node or kg_context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "name, id, path, code or words"},
                "kinds": {"type": "array", "items": {"type": "string"}, "description": "e.g. Plan, Symbol, Section"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kg_context",
        "description": (
            "A connected subgraph around a question or a set of ids. Its nodes and edges together stay within "
            "budget_tokens; what did not fit is counted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "string"}},
                "budget_tokens": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 8000,
                    "default": 3000,
                    "description": "size of the whole text, as UTF-8 bytes / 3",
                },
                "hops": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
            },
        },
    },
    {
        "name": "kg_node",
        "description": "One node: properties, status, label, evidence (source, revision, span, uri) and its edges.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "kg_impact",
        "description": (
            "What a change reaches: give node ids, repo:path paths or a unified diff. Walks back over callers (in "
            "other files too), importers, members, definers, dependants, implementations, tests, subclasses "
            "(inherits) and overriding methods (overrides); documents that mention a reached node come back as a "
            "separate group to re-check. Each hop lists its first `limit` dependents, strongest first, and counts "
            "the rest. Each result notes, per language of its seeds, whether calls across files, inheritance and "
            "overrides are covered, and names a seed with more incoming edges than it reads, with their number."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "string"}},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "the files to change: repo:path, or a bare path",
                },
                "diff": {"type": "string", "description": "a unified diff, e.g. the output of git diff"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": IMPACT_MAX_LIMIT,
                    "default": IMPACT_LIMIT,
                    "description": "dependents listed per hop, and a quarter as many documents; the rest are counted",
                },
            },
        },
    },
    {
        "name": "kg_path",
        "description": (
            "How two nodes connect: the shortest path from one to the other, preferring stronger status. Without "
            "'to', the chains from one node along implements, schedules, verifies, has_step and touches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string"},
                "to": {"type": "string"},
                "max_hops": {"type": "integer", "minimum": 1, "maximum": 6, "default": 6},
            },
            "required": ["from"],
        },
    },
    {
        "name": "kg_status",
        "description": "Bound project, sink profile, snapshot age, and coverage per source.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "kg_more",
        "description": "Continue a truncated result.",
        "inputSchema": {"type": "object", "properties": {"handle": {"type": "string"}}, "required": ["handle"]},
    },
]

# Per tool: its argument names, and those that take a list.
SIGNATURES = {
    tool["name"]: (
        set(tool["inputSchema"]["properties"]),
        {key for key, spec in tool["inputSchema"]["properties"].items() if spec.get("type") == "array"},
    )
    for tool in TOOLS
}
