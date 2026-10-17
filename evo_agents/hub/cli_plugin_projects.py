"""``evo-agents hub plugin list --project|enable|disable|config|rollback``: the plugins of a project and the layers of
a plugin's config (``evo_agents.hub.server.project_plugins``), and the hub's own layer for hub admins
(``--instance``, ``evo_agents.hub.server.plugins``).

``list --project P`` prints each plugin installed on the hub, whether P enabled it and what it asks for. ``enable
NAME --project P`` prints what the installed version asks for, each permission with what it lets the plugin do, and
grants exactly that once the person at the terminal answers yes, or with ``--yes``; without a terminal it needs
``--yes``. ``disable NAME --project P`` turns it off for P, its grants going with it.

``config NAME --project P`` prints the project's layer of the plugin's config (``--agent H`` an agent profile's,
``--instance`` instead of ``--project`` the hub's), ``--versions`` every version of it. ``--set FIELD=VALUE`` (VALUE
read as JSON when it parses, else as text) and ``--unset FIELD`` write its next version, and ``--secret FIELD`` sets a
secret field from stdin, or without echo at a terminal, never from the command line. ``config --project P
--effective`` prints the effective config of each plugin P enabled (or of NAME) for runs under ``--agent`` (the
project's default profile without one): every field with its value, a secret's never, the layer and version that set
it, what does not fit the schema, and the digest. ``config --project P --run ID`` prints the plugins run ID ran with,
each with its version and digest. ``rollback NAME --to N`` copies version N of the layer as its next version.
``--json`` prints what the hub answered. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.cli_secrets import Refused, read_value
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import JsonOutput, json_option, returns_array, returns_object

# The keys of the hub's answers with --json (the contract, `evo-agents hub contract print`).
PROJECT_PLUGIN_KEYS = (
    "name",
    "publisher",
    "version",
    "state",
    "disabled_reason",
    "enabled",
    "permissions",
    "granted",
    "pending",
    "config_schema",
    "enabled_by",
    "enabled_at",
    "granted_by",
    "granted_at",
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


def _project_path(project: str, name: str | None = None, action: str | None = None) -> str:
    path = f"/v1/projects/{quote(project, safe='')}/plugins"
    if name is not None:
        path += f"/{quote(name, safe='')}"
    return path if action is None else f"{path}/{action}"


def _layer_path(args, action: str | None = None) -> str:
    """The path of the layer ``args`` name: the hub's own with --instance, else the project's or --agent's."""
    if args.instance:
        path = f"/v1/plugins/{quote(args.name, safe='')}/config"
    else:
        path = _project_path(args.project, args.name, "config")
    if action:
        path += f"/{action}"
    if not args.instance and args.agent:
        path += "?" + urlencode({"agent": args.agent.removeprefix("@")})
    return path


# Listing


def _state(plugin: dict) -> str:
    if plugin["state"] == "disabled":
        return "turned off on the hub"
    if not plugin["enabled"]:
        return "not enabled"
    return "enabled, grant pending" if plugin["pending"] else "enabled"


def list_project(args) -> int:
    """``hub plugin list --project P``, inside ``cli_plugins.cmd_list``."""
    hub, _ = signed_in()
    found = hub.call("GET", _project_path(args.project))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print("No plugin is installed on the hub: a hub admin installs one with `evo-agents hub plugin install`.")
        return 0
    rows = [
        (p["name"], p["version"], _state(p), ", ".join(item["name"] for item in p["permissions"]) or "-") for p in found
    ]
    table(("NAME", "VERSION", "STATE", "ASKS FOR"), rows)
    return 0


# Enabling


def _asks(plugin: dict) -> None:
    asked = plugin["permissions"]
    if not asked:
        print(f"Plugin {plugin['name']} {plugin['version']} asks for no permission.")
        return
    print(f"Plugin {plugin['name']} {plugin['version']} of {plugin['publisher']} asks to:")
    for item in asked:
        mark = " (granted)" if item["granted"] else ""
        print(f"  {item['name']}: {item['text']}{mark}")


def _agreed(args, plugin: dict) -> bool:
    if args.yes:
        return True
    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        raise HubError("enabling a plugin grants what it asks for: read the list above and pass --yes to grant it")
    answer = input(f"Grant these to plugin {plugin['name']} on project {args.project}? [y/N] ")
    return answer.strip().lower() in YES


@client_command
def cmd_enable(args) -> int:
    hub, _ = signed_in()
    plugin = hub.call("GET", _project_path(args.project, args.name))
    if not args.json:
        _asks(plugin)
    if plugin["enabled"] and not plugin["pending"]:
        if args.json:
            print_json(plugin)
        else:
            print(f"Plugin {args.name} is enabled for project {args.project} already, with all of that granted.")
        return 0
    if args.json and not args.yes:
        raise HubError("with --json, pass --yes to grant what the plugin asks for; nothing was granted")
    if not args.json and not _agreed(args, plugin):
        print("Nothing granted.")
        return 1
    asked = [item["name"] for item in plugin["permissions"]]
    enabled = hub.call("POST", _project_path(args.project, args.name, "enable"), {"permissions": asked})
    if args.json:
        print_json(enabled)
    else:
        print(f"Enabled plugin {args.name} {enabled['version']} for project {args.project}.")
    return 0


@client_command
def cmd_disable(args) -> int:
    hub, _ = signed_in()
    disabled = hub.call("POST", _project_path(args.project, args.name, "disable"))
    if args.json:
        print_json(disabled)
    else:
        print(f"Disabled plugin {args.name} for project {args.project}; the versions of its config stay.")
    return 0


# Config


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _written(args) -> dict:
    """The body of a write from --set, --unset and --secret; {} when none is given."""
    values = {}
    for item in args.set or []:
        field, sep, text = item.partition("=")
        if not sep or not field:
            raise HubError(f"--set takes FIELD=VALUE, not {item!r}")
        values[field] = _value(text)
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


def _where(layer: dict) -> str:
    if layer["layer"] == "instance":
        return "the hub's own layer"
    if layer["layer"] == "agent":
        return f"the layer of @{layer['agent']} in project {layer['project']}"
    return f"the layer of project {layer['project']}"


def _show_layer(layer: dict) -> None:
    head = f"Plugin {layer['plugin']}, {_where(layer)}"
    if not layer["version"]:
        print(f"{head}: never written")
        return
    back = f", a rollback to v{layer['rolled_back_to']}" if layer["rolled_back_to"] else ""
    print(f"{head}, v{layer['version']}{back}, by {layer['changed_by']} at {when(layer['changed_at'])} UTC")
    for field, value in sorted(layer["values"].items()):
        print(f"  {field} = {json.dumps(value, ensure_ascii=False)}")
    for field in layer["secrets"]:
        print(f"  {field} = (secret, set)")


def _show_effective(found: dict) -> None:
    print(f"Plugin {found['plugin']} {found['version']} in project {found['project']}, for @{found['agent']}:")
    for field in found["fields"]:
        value = "(secret, set)" if field["secret"] else json.dumps(field["value"], ensure_ascii=False)
        where = field["layer"] if field["version"] is None else f"{field['layer']} v{field['version']}"
        print(f"  {field['name']} = {value}  [{where}]")
    for problem in found["problems"]:
        print(f"  problem: {problem}")
    print(f"  digest {found['digest']}")


def _effective(hub, args) -> int:
    if args.name:
        names = [args.name]
    else:
        names = [p["name"] for p in hub.call("GET", _project_path(args.project)) if p["enabled"]]
    query = "?" + urlencode({"agent": args.agent.removeprefix("@")}) if args.agent else ""
    found = [hub.call("GET", _project_path(args.project, name, "config/effective") + query) for name in names]
    if args.json:
        print_json(found)
    elif not found:
        print(f"Project {args.project} has no plugin enabled.")
    for item in [] if args.json else found:
        _show_effective(item)
    return 0


def _run(hub, args) -> int:
    path = f"/v1/projects/{quote(args.project, safe='')}/runs/{args.run}/plugins"
    found = hub.call("GET", path)
    if args.json:
        print_json(found)
    elif not found:
        print(f"Run {args.run} ran with no plugin, or no worker claimed it yet.")
    else:
        table(("PLUGIN", "VERSION", "CONFIG DIGEST"), [(p["name"], p["version"], p["config_digest"]) for p in found])
    return 0


def _check_config_args(args) -> None:
    if args.instance == bool(args.project):
        raise HubError("name the layer: --project P (with --agent H for an agent profile's), or --instance")
    if args.instance and (args.agent or args.effective or args.run):
        raise HubError("--instance is the hub's own layer: it takes no --agent, --effective or --run")
    if args.name is None and not (args.effective or args.run):
        raise HubError("name the plugin: evo-agents hub plugin config NAME --project P")


@client_command
def cmd_config(args) -> int:
    _check_config_args(args)
    hub, _ = signed_in()
    body = _written(args)
    if body and (args.effective or args.versions or args.run):
        raise HubError("--set, --unset and --secret write the layer: leave out --effective, --versions and --run")
    if args.effective:
        return _effective(hub, args)
    if args.run:
        return _run(hub, args)
    if args.versions:
        found = hub.call("GET", _layer_path(args, "versions"))
        if args.json:
            print_json(found)
        for layer in [] if args.json else found:
            _show_layer(layer)
        return 0
    layer = hub.call("POST", _layer_path(args), body) if body else hub.call("GET", _layer_path(args))
    if args.json:
        print_json(layer)
    else:
        _show_layer(layer)
    return 0


@client_command
def cmd_rollback(args) -> int:
    if args.instance == bool(args.project) or (args.instance and args.agent):
        raise HubError("name the layer: --project P (with --agent H for an agent profile's), or --instance")
    hub, _ = signed_in()
    layer = hub.call("POST", _layer_path(args, "rollback"), {"version": args.to})
    if args.json:
        print_json(layer)
    else:
        print(f"Rolled {_where(layer)} of plugin {args.name} back to v{args.to}, as v{layer['version']}.")
    return 0


# The parsers


def _layer_options(parser, project_required: bool = False) -> None:
    parser.add_argument("--project", metavar="PROJECT", required=project_required, help="the project")
    if not project_required:
        parser.add_argument("--instance", action="store_true", help="the hub's own layer (hub admins)")
    parser.add_argument("--agent", metavar="HANDLE", help="an agent profile of the project: its layer, its runs")


def project_list_output() -> JsonOutput:
    """What ``hub plugin list --project`` prints with --json, a variant of the store's list."""
    return returns_array(*PROJECT_PLUGIN_KEYS, schema="ProjectPlugin")


def register_project_commands(psub, name) -> None:
    """Add enable, disable, config and rollback to ``hub plugin``; ``name`` adds the NAME argument."""
    output = returns_object(*PROJECT_PLUGIN_KEYS, schema="ProjectPlugin")
    enabled = psub.add_parser("enable", help="enable an installed plugin for a project, granting what it asks for")
    name(enabled)
    enabled.add_argument("--project", metavar="PROJECT", required=True, help="the project")
    enabled.add_argument("--yes", action="store_true", help="grant what it asks for without asking")
    json_option(enabled, output)
    enabled.set_defaults(func=cmd_enable)

    disabled = psub.add_parser("disable", help="disable a plugin for a project; its config stays")
    name(disabled)
    disabled.add_argument("--project", metavar="PROJECT", required=True, help="the project")
    json_option(disabled, output)
    disabled.set_defaults(func=cmd_disable)

    config = psub.add_parser("config", help="a layer of a plugin's config: print it, write it; or the effective one")
    config.add_argument("name", metavar="NAME", nargs="?", help="the plugin's id; optional with --effective or --run")
    _layer_options(config)
    shown = config.add_mutually_exclusive_group()
    shown.add_argument("--effective", action="store_true", help="the effective config, for runs under --agent")
    shown.add_argument("--versions", action="store_true", help="every version of the layer, the newest first")
    shown.add_argument("--run", metavar="RUN_ID", type=int, help="the plugins a run ran with, and digests")
    config.add_argument("--set", metavar="FIELD=VALUE", action="append", help="set a field (VALUE as JSON, or text)")
    config.add_argument("--unset", metavar="FIELD", action="append", help="clear a field the layer sets")
    config.add_argument("--secret", metavar="FIELD", help="set a secret field, its value from stdin or a prompt")
    layer = returns_object(
        *LAYER_KEYS,
        schema="PluginConfigLayer",
        variants=[
            ("--effective", returns_array(*EFFECTIVE_KEYS, schema="EffectiveConfig")),
            ("--versions", returns_array(*LAYER_KEYS, schema="PluginConfigLayer")),
            ("--run", returns_array(*RUN_PLUGIN_KEYS, schema="RunPlugin")),
        ],
    )
    json_option(config, layer)
    config.set_defaults(func=cmd_config)

    rolled = psub.add_parser("rollback", help="copy an older version of a layer of a plugin's config as its next")
    name(rolled)
    _layer_options(rolled)
    rolled.add_argument("--to", metavar="VERSION", type=int, required=True, help="the version to copy")
    json_option(rolled, returns_object(*LAYER_KEYS, schema="PluginConfigLayer"))
    rolled.set_defaults(func=cmd_rollback)
