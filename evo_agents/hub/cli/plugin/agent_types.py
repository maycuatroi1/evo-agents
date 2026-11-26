"""``evo-agents hub plugin agent-type list|enable|disable --project P``: the agent types the plugins a project enabled
contribute (``evo_agents.hub.server.agent_types``; ``docs/plugins.md``, section Agent types).

``list`` prints each agent type of the plugins P enabled, to any member of P: its handle, whether P enabled it, the
member its runs go under, its cap of cost for 24 hours, the floor of one run and what its task runs cost in them. ``enable PLUGIN NAME``, for
an admin of P, enables one or changes how (PUT .../agent-types/{plugin}/{name}): ``--run-as`` the writer or admin of P
whose name its runs go under, ``--daily-usd`` the cap, ``--min-run-usd`` the least cap of one run, and ``--handle`` the handle of the profile its first enable
makes. ``disable PLUGIN NAME`` turns it off and keeps its profile. ``--json`` prints what the hub answered.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes); :class:`AgentTypes`
is the gateway to the routes, :data:`AGENT_TYPE` the group ``hub plugin`` attaches. Standard library only, like the
rest of the client.
"""

from __future__ import annotations

from typing import Any

from evo_agents.clikit import Arg, Group, Result, Table, Text
from evo_agents.hub.cli.base import HubCommand, HubContext, ProjectResource, call_object, when
from evo_agents.hub.contract import returns_array, returns_object

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
    "min_run_usd",
    "effective_min_run_usd",
    "spent_usd",
    "enabled_by",
    "enabled_at",
    "disabled_at",
)
PROJECT = Arg("--project", metavar="PROJECT", required=True, help="the project")
NAMED = (
    Arg("plugin", metavar="PLUGIN", help="the plugin's id"),
    Arg("name", metavar="NAME", help="the agent type, as the plugin's manifest names it"),
    PROJECT,
)


class AgentTypes(ProjectResource):
    """The agent types of a project's plugins: GET .../agent-types, PUT and DELETE .../agent-types/{plugin}/{name}."""

    collection = "agent-types"

    def list(self) -> Any:
        return self.call("GET")

    def enable(self, plugin: str, name: str, body: dict) -> dict:
        return call_object(self.hub, "PUT", self.path(plugin, name), body)

    def disable(self, plugin: str, name: str) -> dict:
        return call_object(self.hub, "DELETE", self.path(plugin, name))


def state_of(item: dict) -> str:
    if item["enabled"]:
        return "enabled"
    return "turned off" if item["disabled_at"] else "not enabled"


def usd(value) -> str:
    return "-" if value is None else f"${value:.2f}"


def enable_body(args) -> dict:
    """The body of PUT .../agent-types/{plugin}/{name} for the command line ``args``."""
    body = {"run_as": args.run_as.removeprefix("@"), "daily_usd": args.daily_usd}
    if args.min_run_usd is not None:
        body["min_run_usd"] = args.min_run_usd
    if args.handle:
        body["handle"] = args.handle.removeprefix("@")
    return body


def agent_type_row(item: dict) -> tuple:
    return (
        item["plugin"],
        item["name"],
        f"@{item['handle']}",
        state_of(item),
        item["run_as"] or "-",
        usd(item["daily_usd"]),
        usd(item["effective_min_run_usd"]),
        usd(item["spent_usd"]),
    )


class ListAgentTypes(HubCommand):
    name = "list"
    help = "the agent types the plugins a project enabled contribute, and how"
    arguments = (PROJECT,)
    output = returns_array(*AGENT_TYPE_KEYS, schema="AgentType")
    columns = ("PLUGIN", "NAME", "HANDLE", "STATE", "RUN AS", "DAILY CAP", "MIN RUN", "SPENT 24H")

    def run(self, ctx: HubContext) -> Result:
        found = AgentTypes.of(ctx).list()
        empty = f"No plugin project {ctx.args.project} enabled contributes an agent type."
        return Result(found, Table(self.columns, [agent_type_row(item) for item in found], empty))


class EnableAgentType(HubCommand):
    name = "enable"
    help = "enable an agent type for a project, or change its run-as and cap"
    arguments = (
        *NAMED,
        Arg("--run-as", metavar="LOGIN", required=True, help="the writer or admin whose name its runs go under"),
        Arg("--daily-usd", metavar="USD", type=float, required=True, help="what its task runs may cost in 24 hours"),
        Arg(
            "--min-run-usd",
            metavar="USD",
            type=float,
            help="the least cap of one run (default: keep the one set, else $1.00; at most --daily-usd)",
        ),
        Arg("--handle", metavar="HANDLE", help="the handle of the profile its first enable makes"),
    )
    output = returns_object(*AGENT_TYPE_KEYS, schema="AgentType")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = AgentTypes.of(ctx).enable(args.plugin, args.name, enable_body(args))
        return Result(
            found,
            Text(
                f"Enabled agent type {args.name} of plugin {args.plugin} for project {args.project} as "
                f"@{found['handle']}: its runs go under {found['run_as']}, at most {usd(found['daily_usd'])} in 24 hours."
            ),
        )


class DisableAgentType(HubCommand):
    name = "disable"
    help = "turn an agent type off for a project; its profile stays"
    arguments = NAMED
    output = returns_object(*AGENT_TYPE_KEYS, schema="AgentType")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = AgentTypes.of(ctx).disable(args.plugin, args.name)
        since = f" at {when(found['disabled_at'])} UTC" if found.get("disabled_at") else ""
        return Result(
            found,
            Text(
                f"Turned agent type {args.name} of plugin {args.plugin} off for project {args.project}{since}; "
                f"@{found['handle']} stays."
            ),
        )


AGENT_TYPE = Group(
    "agent-type",
    "the agent types of a project's plugins: list, enable, turn off",
    [ListAgentTypes(), EnableAgentType(), DisableAgentType()],
    dest="agent_type_command",
)

__all__ = ["AGENT_TYPE", "AGENT_TYPE_KEYS", "AgentTypes"]
