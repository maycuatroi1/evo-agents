"""``evo-agents hub plugin list --project|enable|disable|config|rollback``: the plugins of a project and the layers of
a plugin's config (``evo_agents.hub.server.project_plugins``), and the hub's own layer for hub admins
(``--instance``, ``evo_agents.hub.server.plugins``).

``list --project P`` prints each plugin installed on the hub, whether P enabled it and what it asks for. ``enable
NAME --project P`` prints what the installed version asks for, each permission with what it lets the plugin do, and
grants exactly that once the person at the terminal answers yes, or with ``--yes``; without a terminal it needs
``--yes``. ``--records-level LEVEL`` sets the highest level of P's ladder its jobs and events, which have no caller,
reach in its records (records:use). ``disable NAME --project P`` turns it off for P, its grants going with it.
``agent-type list|enable|disable`` keeps the agent types of P's plugins (``cli.plugin.agent_types``).

``config NAME --project P`` prints the project's layer of the plugin's config (``--agent H`` an agent profile's,
``--instance`` instead of ``--project`` the hub's), ``--versions`` every version of it. ``--set FIELD=VALUE`` (VALUE
read as JSON when it parses, else as text) and ``--unset FIELD`` write its next version, and ``--secret FIELD`` sets a
secret field from stdin, or without echo at a terminal, never from the command line. ``config --project P
--effective`` prints the effective config of each plugin P enabled (or of NAME) for runs under ``--agent`` (the
project's default profile without one): every field with its value, a secret's never, the layer and version that set
it, what does not fit the schema, and the digest. ``config --project P --run ID`` prints the plugins run ID ran with,
each with its version and digest. ``rollback NAME --to N`` copies version N of the layer as its next version.
``--json`` prints what the hub answered.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`ProjectPlugins`
is the gateway to a project's plugins, :class:`ConfigLayer` to one layer of a plugin's config and :class:`RunPlugins`
to the plugins of a run; ``hub plugin`` (``evo_agents.hub.cli.plugin``) lists :data:`ENABLE`, :data:`DISABLE`,
:data:`CONFIG` and :data:`ROLLBACK` among its children. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.parse import urlencode

from evo_agents.clikit import Arg, Result, Table, Text, View, Views
from evo_agents.hub.api import route
from evo_agents.hub.cli.base import HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.cli.secret.values import Refused, read_value
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.contract import returns_array, returns_object

# The keys of the hub's answers with --json (the contract, `evo-agents hub contract print`).
PROJECT_PLUGIN_KEYS = (
    "name",
    "publisher",
    "version",
    "state",
    "disabled_reason",
    "enabled",
    "builtin",
    "permissions",
    "granted",
    "pending",
    "config_schema",
    "enabled_by",
    "enabled_at",
    "granted_by",
    "granted_at",
    "title",
    "description",
    "tier",
    "pages",
    "records_level",
)
LAYER_KEYS = (
    "plugin",
    "layer",
    "project",
    "agent",
    "version",
    "values",
    "secrets",
    "rolled_back_to",
    "changed_by",
    "changed_at",
)
EFFECTIVE_KEYS = ("plugin", "version", "project", "agent", "fields", "problems", "digest")
RUN_PLUGIN_KEYS = ("name", "version", "config_digest", "recorded_at")
YES = ("y", "yes")
PROJECT_LIST = returns_array(*PROJECT_PLUGIN_KEYS, schema="ProjectPlugin")  # `hub plugin list --project` with --json
NAME = Arg("name", metavar="NAME", help="the plugin's id, as its manifest gives it")
PROJECT = Arg("--project", metavar="PROJECT", required=True, help="the project")
LAYER = (
    Arg("--project", metavar="PROJECT", required=False, help="the project"),
    Arg("--instance", action="store_true", help="the hub's own layer (hub admins)"),
    Arg("--agent", metavar="HANDLE", help="an agent profile of the project: its layer, its runs"),
)
NAME_LAYER = "name the layer: --project P (with --agent H for an agent profile's), or --instance"


# The gateways


class ProjectPlugins(ProjectResource):
    """The plugins of a project: GET .../plugins and .../plugins/{name}, POST .../plugins/{name}/{action}, GET
    .../plugins/{name}/config/effective."""

    collection = "plugins"

    def list(self) -> Any:
        return self.call("GET")

    def get(self, name: str) -> Any:
        return self.call("GET", name)

    def act(self, name: str, action: str, body: dict | None = None) -> Any:
        """POST .../plugins/{name}/{action}: enable, disable or remove."""
        return self.call("POST", name, action, body=body)

    def effective(self, name: str, agent: str | None) -> Any:
        return self.call("GET", name, "config", "effective", agent=agent)


class RunPlugins(ProjectResource):
    """The plugins a run ran with: GET .../runs/{id}/plugins."""

    collection = "runs"

    def of_run(self, run_id: int) -> Any:
        return self.call("GET", run_id, "plugins")


class ConfigLayer:
    """One layer of a plugin's config (Gateway): the hub's own (/v1/plugins/{name}/config) with ``instance``, else
    the project's or its agent profile's (/v1/projects/{p}/plugins/{name}/config?agent=H)."""

    def __init__(self, hub: Hub, args: argparse.Namespace):
        self.hub, self.args = hub, args

    def path(self, action: str | None = None) -> str:
        args = self.args
        if args.instance:
            path = route("plugins", args.name, "config")
        else:
            path = ProjectPlugins(self.hub, args.project).path(args.name, "config")
        if action:
            path += f"/{action}"
        if not args.instance and args.agent:
            path += "?" + urlencode({"agent": args.agent.removeprefix("@")})
        return path

    def get(self) -> Any:
        return self.hub.call("GET", self.path())

    def write(self, body: dict) -> Any:
        return self.hub.call("POST", self.path(), body)

    def versions(self) -> Any:
        return self.hub.call("GET", self.path("versions"))

    def rollback(self, version: int) -> Any:
        return self.hub.call("POST", self.path("rollback"), {"version": version})


# Listing


def state_of(plugin: dict) -> str:
    if plugin["state"] == "disabled":
        return "turned off on the hub"
    if not plugin["enabled"]:
        return "not enabled"
    return "enabled, grant pending" if plugin["pending"] else "enabled"


def project_list(ctx: HubContext) -> Result:
    """``hub plugin list --project P``, which ``hub plugin list`` runs with --project."""
    found = ProjectPlugins(ctx.hub, ctx.args.project).list()
    rows = [
        (p["name"], p["version"], state_of(p), ", ".join(item["name"] for item in p["permissions"]) or "-")
        for p in found
    ]
    empty = "No plugin is installed on the hub: a hub admin installs one with `evo-agents hub plugin install`."
    return Result(found, Table(("NAME", "VERSION", "STATE", "ASKS FOR"), rows, empty))


# Enabling


def asks_lines(plugin: dict) -> list[str]:
    """What the installed version of ``plugin`` asks for, each permission with what it lets the plugin do."""
    asked = plugin["permissions"]
    if not asked:
        return [f"Plugin {plugin['name']} {plugin['version']} asks for no permission."]
    lines = [f"Plugin {plugin['name']} {plugin['version']} of {plugin['publisher']} asks to:"]
    for item in asked:
        mark = " (granted)" if item["granted"] else ""
        lines.append(f"  {item['name']}: {item['text']}{mark}")
    return lines


def agreed(args, plugin: dict) -> bool:
    if args.yes:
        return True
    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        raise HubError("enabling a plugin grants what it asks for: read the list above and pass --yes to grant it")
    answer = input(f"Grant these to plugin {plugin['name']} on project {args.project}? [y/N] ")
    return answer.strip().lower() in YES


def enable_body(plugin: dict, level: str | None) -> dict:
    """The body of POST .../enable: exactly what ``plugin`` asks for, and the records level when one is given."""
    asked = [item["name"] for item in plugin["permissions"]]
    return {"permissions": asked} if level is None else {"permissions": asked, "records_level": level}


class EnablePlugin(HubCommand):
    name = "enable"
    help = "enable an installed plugin for a project, granting what it asks for"
    arguments = (
        NAME,
        PROJECT,
        Arg("--yes", action="store_true", help="grant what it asks for without asking"),
        Arg(
            "--records-level",
            metavar="LEVEL",
            help="the highest level of the project's ladder its jobs and events reach in its records (records:use)",
        ),
    )
    output = returns_object(*PROJECT_PLUGIN_KEYS, schema="ProjectPlugin")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        plugins = ProjectPlugins(ctx.hub, args.project)
        plugin = plugins.get(args.name)
        if not args.json:
            Text(*asks_lines(plugin)).write(ctx.out)  # what it asks for comes before the question
        level = args.records_level
        if plugin["enabled"] and not plugin["pending"] and level in (None, (plugin or {}).get("records_level")):
            done = f"Plugin {args.name} is enabled for project {args.project} already, with all of that granted."
            return Result(plugin, Text(done))
        if args.json and not args.yes:
            raise HubError("with --json, pass --yes to grant what the plugin asks for; nothing was granted")
        if not args.json and not agreed(args, plugin):
            return Result(None, Text("Nothing granted."), status=1)
        enabled = plugins.act(args.name, "enable", enable_body(plugin, level))
        reach = f", its jobs and events reaching records up to {level}" if level is not None else ""
        return Result(
            enabled, Text(f"Enabled plugin {args.name} {enabled['version']} for project {args.project}{reach}.")
        )


class DisablePlugin(HubCommand):
    name = "disable"
    help = "disable a plugin for a project; its config stays"
    arguments = (NAME, PROJECT)
    output = returns_object(*PROJECT_PLUGIN_KEYS, schema="ProjectPlugin")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        disabled = ProjectPlugins(ctx.hub, args.project).act(args.name, "disable")
        return Result(
            disabled, Text(f"Disabled plugin {args.name} for project {args.project}; the versions of its config stay.")
        )


# Config


def field_value(text: str):
    """VALUE of ``--set FIELD=VALUE``: JSON when it parses, else the text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def written_body(args) -> dict:
    """The body of a write from --set, --unset and --secret; {} when none is given."""
    values = {}
    for item in args.set or []:
        field, sep, text = item.partition("=")
        if not sep or not field:
            raise HubError(f"--set takes FIELD=VALUE, not {item!r}")
        values[field] = field_value(text)
    if args.secret:
        if args.secret in values:
            raise HubError(f"{args.secret} is given by --set and by --secret: give it once")
        try:
            values[args.secret] = read_value(
                args.secret,
                prompt=f"Value of secret field {args.secret} (not shown): ",
                missing=f"no value for secret field {args.secret}: give it on stdin, or at the prompt",
            )
        except Refused as exc:
            raise HubError(str(exc)) from None
    unset = list(args.unset or [])
    return {"values": values, "unset": unset} if values or unset else {}


def where_of(layer: dict) -> str:
    if layer["layer"] == "instance":
        return "the hub's own layer"
    if layer["layer"] == "agent":
        return f"the layer of @{layer['agent']} in project {layer['project']}"
    return f"the layer of project {layer['project']}"


def layer_lines(layer: dict) -> list[str]:
    head = f"Plugin {layer['plugin']}, {where_of(layer)}"
    if not layer["version"]:
        return [f"{head}: never written"]
    back = f", a rollback to v{layer['rolled_back_to']}" if layer["rolled_back_to"] else ""
    lines = [f"{head}, v{layer['version']}{back}, by {layer['changed_by']} at {when(layer['changed_at'])} UTC"]
    lines += [
        f"  {field} = {json.dumps(value, ensure_ascii=False)}" for field, value in sorted(layer["values"].items())
    ]
    lines += [f"  {field} = (secret, set)" for field in layer["secrets"]]
    return lines


def effective_lines(found: dict) -> list[str]:
    lines = [f"Plugin {found['plugin']} {found['version']} in project {found['project']}, for @{found['agent']}:"]
    for field in found["fields"]:
        value = "(secret, set)" if field["secret"] else json.dumps(field["value"], ensure_ascii=False)
        where = field["layer"] if field["version"] is None else f"{field['layer']} v{field['version']}"
        lines.append(f"  {field['name']} = {value}  [{where}]")
    lines += [f"  problem: {problem}" for problem in found["problems"]]
    lines.append(f"  digest {found['digest']}")
    return lines


def effective(hub: Hub, args) -> Result:
    """``config --effective``: the effective config of NAME, or of each plugin the project enabled."""
    plugins = ProjectPlugins(hub, args.project)
    names = [args.name] if args.name else [p["name"] for p in plugins.list() if p["enabled"]]
    agent = args.agent.removeprefix("@") if args.agent else None
    found = [plugins.effective(name, agent) for name in names]
    if not found:
        return Result(found, Text(f"Project {args.project} has no plugin enabled."))
    return Result(found, Text(*(line for item in found for line in effective_lines(item))))


def run_plugins(hub: Hub, args) -> Result:
    """``config --run ID``: the plugins run ID ran with, each with its version and config digest."""
    found = RunPlugins(hub, args.project).of_run(args.run)
    if not found:
        return Result(found, Text(f"Run {args.run} ran with no plugin, or no worker claimed it yet."))
    rows = [(p["name"], p["version"], p["config_digest"]) for p in found]
    return Result(found, Table(("PLUGIN", "VERSION", "CONFIG DIGEST"), rows))


class Config(HubCommand):
    name = "config"
    help = "a layer of a plugin's config: print it, write it; or the effective one"
    arguments = (
        Arg("name", metavar="NAME", nargs="?", help="the plugin's id; optional with --effective or --run"),
        *LAYER,
    )
    output = returns_object(
        *LAYER_KEYS,
        schema="PluginConfigLayer",
        variants=[
            ("--effective", returns_array(*EFFECTIVE_KEYS, schema="EffectiveConfig")),
            ("--versions", returns_array(*LAYER_KEYS, schema="PluginConfigLayer")),
            ("--run", returns_array(*RUN_PLUGIN_KEYS, schema="RunPlugin")),
        ],
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        shown = parser.add_mutually_exclusive_group()
        shown.add_argument("--effective", action="store_true", help="the effective config, for runs under --agent")
        shown.add_argument("--versions", action="store_true", help="every version of the layer, the newest first")
        shown.add_argument("--run", metavar="RUN_ID", type=int, help="the plugins a run ran with, and digests")
        parser.add_argument(
            "--set", metavar="FIELD=VALUE", action="append", help="set a field (VALUE as JSON, or text)"
        )
        parser.add_argument("--unset", metavar="FIELD", action="append", help="clear a field the layer sets")
        parser.add_argument("--secret", metavar="FIELD", help="set a secret field, its value from stdin or a prompt")

    def check(self, args: argparse.Namespace) -> None:
        if args.instance == bool(args.project):
            raise HubError(NAME_LAYER)
        if args.instance and (args.agent or args.effective or args.run):
            raise HubError("--instance is the hub's own layer: it takes no --agent, --effective or --run")
        if args.name is None and not (args.effective or args.run):
            raise HubError("name the plugin: evo-agents hub plugin config NAME --project P")

    def run(self, ctx: HubContext) -> Result:
        args, hub = ctx.args, ctx.hub  # signed in before a secret is asked for
        body = written_body(args)
        if body and (args.effective or args.versions or args.run):
            raise HubError("--set, --unset and --secret write the layer: leave out --effective, --versions and --run")
        if args.effective:
            return effective(hub, args)
        if args.run:
            return run_plugins(hub, args)
        layer = ConfigLayer(hub, args)
        if args.versions:
            found = layer.versions()
            return Result(found, Views(*(layer_view(item) for item in found)))
        found = layer.write(body) if body else layer.get()
        return Result(found, layer_view(found))


def layer_view(layer: dict) -> View:
    return Text(*layer_lines(layer))


class Rollback(HubCommand):
    name = "rollback"
    help = "copy an older version of a layer of a plugin's config as its next"
    arguments = (
        NAME,
        *LAYER,
        Arg("--to", metavar="VERSION", type=int, required=True, help="the version to copy"),
    )
    output = returns_object(*LAYER_KEYS, schema="PluginConfigLayer")

    def check(self, args: argparse.Namespace) -> None:
        if args.instance == bool(args.project) or (args.instance and args.agent):
            raise HubError(NAME_LAYER)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        layer = ConfigLayer(ctx.hub, args).rollback(args.to)
        said = f"Rolled {where_of(layer)} of plugin {args.name} back to v{args.to}, as v{layer['version']}."
        return Result(layer, Text(said))


ENABLE = EnablePlugin()
DISABLE = DisablePlugin()
CONFIG = Config()
ROLLBACK = Rollback()
