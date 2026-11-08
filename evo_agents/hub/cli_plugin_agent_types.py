"""``evo-agents hub plugin agent-type list|enable|disable --project P``: the agent types the plugins a project enabled
contribute (``evo_agents.hub.server.agent_types``; ``docs/plugins.md``, section Agent types).

``list`` prints each agent type of the plugins P enabled, to any member of P: its handle, whether P enabled it, the
member its runs go under, its cap of cost for 24 hours and what its task runs cost in them. ``enable PLUGIN NAME``, for
an admin of P, enables one or changes how (PUT .../agent-types/{plugin}/{name}): ``--run-as`` the writer or admin of P
whose name its runs go under, ``--daily-usd`` the cap, and ``--handle`` the handle of the profile its first enable
makes. ``disable PLUGIN NAME`` turns it off and keeps its profile. ``--json`` prints what the hub answered. Standard
library only, like the rest of the client.
"""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.hub.cli_client import call_object, client_command, print_json, signed_in, table, when
from evo_agents.hub.contract import json_option, returns_array, returns_object

# The keys of the hub's answers with --json (the contract, `evo-agents hub contract print`).
AGENT_TYPE_KEYS = (
    "plugin",
    "name",
    "title",
    "description",
    "handle",
    "environment",
    "packs",
    "enabled",
    "run_as",
    "daily_usd",
    "spent_usd",
    "enabled_by",
    "enabled_at",
    "disabled_at",
)


def _path(project: str, plugin: str | None = None, name: str | None = None) -> str:
    path = f"/v1/projects/{quote(project, safe='')}/agent-types"
    if plugin is not None and name is not None:
        path += f"/{quote(plugin, safe='')}/{quote(name, safe='')}"
    return path


def _state(item: dict) -> str:
    if item["enabled"]:
        return "enabled"
    return "turned off" if item["disabled_at"] else "not enabled"


def _usd(value) -> str:
    return "-" if value is None else f"${value:.2f}"


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(args.project))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"No plugin project {args.project} enabled contributes an agent type.")
        return 0
    rows = [
        (
            item["plugin"],
            item["name"],
            f"@{item['handle']}",
            _state(item),
            item["run_as"] or "-",
            _usd(item["daily_usd"]),
            _usd(item["spent_usd"]),
        )
        for item in found
    ]
    table(("PLUGIN", "NAME", "HANDLE", "STATE", "RUN AS", "DAILY CAP", "SPENT 24H"), rows)
    return 0


@client_command
def cmd_enable(args) -> int:
    hub, _ = signed_in()
    body = {"run_as": args.run_as.removeprefix("@"), "daily_usd": args.daily_usd}
    if args.handle:
        body["handle"] = args.handle.removeprefix("@")
    found = call_object(hub, "PUT", _path(args.project, args.plugin, args.name), body)
    if args.json:
        print_json(found)
    else:
        print(
            f"Enabled agent type {args.name} of plugin {args.plugin} for project {args.project} as @{found['handle']}: "
            f"its runs go under {found['run_as']}, at most {_usd(found['daily_usd'])} in 24 hours."
        )
    return 0


@client_command
def cmd_disable(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "DELETE", _path(args.project, args.plugin, args.name))
    if args.json:
        print_json(found)
    else:
        since = f" at {when(found['disabled_at'])} UTC" if found.get("disabled_at") else ""
        print(
            f"Turned agent type {args.name} of plugin {args.plugin} off for project {args.project}{since}; "
            f"@{found['handle']} stays."
        )
    return 0


def _named(parser) -> None:
    parser.add_argument("plugin", metavar="PLUGIN", help="the plugin's id")
    parser.add_argument("name", metavar="NAME", help="the agent type, as the plugin's manifest names it")
    parser.add_argument("--project", metavar="PROJECT", required=True, help="the project")


def register_agent_type_commands(psub) -> None:
    """Add ``agent-type list|enable|disable`` to ``hub plugin``."""
    output = returns_object(*AGENT_TYPE_KEYS, schema="AgentType")
    group = psub.add_parser("agent-type", help="the agent types of a project's plugins: list, enable, turn off")
    tsub = group.add_subparsers(dest="agent_type_command", required=True)

    listed = tsub.add_parser("list", help="the agent types the plugins a project enabled contribute, and how")
    listed.add_argument("--project", metavar="PROJECT", required=True, help="the project")
    json_option(listed, returns_array(*AGENT_TYPE_KEYS, schema="AgentType"))
    listed.set_defaults(func=cmd_list)

    enabled = tsub.add_parser("enable", help="enable an agent type for a project, or change its run-as and cap")
    _named(enabled)
    enabled.add_argument(
        "--run-as", metavar="LOGIN", required=True, help="the writer or admin whose name its runs go under"
    )
    enabled.add_argument(
        "--daily-usd", metavar="USD", type=float, required=True, help="what its task runs may cost in 24 hours"
    )
    enabled.add_argument("--handle", metavar="HANDLE", help="the handle of the profile its first enable makes")
    json_option(enabled, output)
    enabled.set_defaults(func=cmd_enable)

    disabled = tsub.add_parser("disable", help="turn an agent type off for a project; its profile stays")
    _named(disabled)
    json_option(disabled, output)
    disabled.set_defaults(func=cmd_disable)


__all__ = ["AGENT_TYPE_KEYS", "register_agent_type_commands"]
