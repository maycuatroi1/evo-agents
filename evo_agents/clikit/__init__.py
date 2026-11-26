"""The command kit of evo-agents: commands as objects over argparse (docs/structure.md, section Commands).

Standard library only, and it imports no other package of evo_agents, so the hub, the worker and the knowledge graph
build their commands on it alike.
"""

from evo_agents.clikit.command import (
    EXIT_USAGE,
    Arg,
    Command,
    Context,
    Group,
    Legacy,
    ListCommand,
    Node,
    UsageError,
)
from evo_agents.clikit.json_output import JsonOutput, json_option, returns_array, returns_map, returns_object
from evo_agents.clikit.views import (
    NOTHING,
    Result,
    Stderr,
    Table,
    Text,
    Verbatim,
    View,
    Views,
    json_text,
    table_lines,
)

__all__ = [
    "EXIT_USAGE",
    "NOTHING",
    "Arg",
    "Command",
    "Context",
    "Group",
    "JsonOutput",
    "Legacy",
    "ListCommand",
    "Node",
    "Result",
    "Stderr",
    "Table",
    "Text",
    "Verbatim",
    "UsageError",
    "View",
    "Views",
    "json_option",
    "json_text",
    "returns_array",
    "returns_map",
    "returns_object",
    "table_lines",
]
