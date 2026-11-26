"""``evo-agents hub <plugin> <command>``: the commands plugins add to the CLI (extension point ``cli``), read from the
hub rather than shipped with the CLI, so a plugin adds one without a release of evo-agents.

The hub lists them at ``GET /v1/cli/plugins`` (``evo_agents.hub.server.plugin_command_list``); the CLI keeps the answer in
``cli-plugins.json`` of the login directory (``~/.evo/hub``), and builds the commands of each plugin from
it when it builds its own parser. A ``hub`` command it does not know, or a command of a plugin it does not know, makes it ask
the hub again before it parses: the first run of a new command fetches it. A plugin whose id is a
command of the CLI's own is left out.

A command calls one route of its plugin with the member's token: ``http.path`` below ``/v1/x/<id>/`` (below
``/v1/x/<id>/<project>/`` when ``project`` is true, which adds ``--project``), each argument put in the path, the query
or the JSON body as ``in`` says; a ``file`` argument sends the file's text. The answer prints as JSON with ``--json``,
else as the table of ``output.table`` (the keys of the objects of an array answer, or of ``output.json`` the array at
that key), else as ``output.text`` (a key of an object answer), else as JSON; with ``output.render`` text the route
words the answer itself (query ``render=text``) and the CLI prints that text. ``when`` names other routes an argument
switches to, ``confirm`` a switch without which the command calls nothing, and ``client`` one of the CLI's own clients
(``CLIENTS``) that runs the command instead of one call.

The core plugins installed with the CLI (entry point group ``evo_agents.plugins``) ship their commands too: ``cli.json``
beside the package's ``__init__``, read without importing the plugin. They need no hub to be listed, and over the
list kept they win, so the commands of the plugins the CLI ships are the same on every machine (the CLI's contract,
``evo_agents.hub.contract``).

The commands are objects of ``evo_agents.clikit`` (docs/structure.md, section Command classes) built from the list:
:class:`Extensions` (:data:`EXTENSIONS`, which ``evo_agents.hub.cli`` attaches last) attaches a ``Group`` for each
plugin, each child a :class:`PluginCommand` made from one entry of the list, its arguments ``Arg`` values
(:func:`plugin_args`), which calls the plugin's route through :class:`PluginRoutes`. Standard library only, like the
rest of the client.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from evo_agents.clikit import NOTHING, Arg, Group, JsonOutput, Result, Table, Text, View, json_text
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.plugin_commands import (  # noqa: F401 (moved there; the callers take them from here)
    BUNDLED_FILE,
    BUNDLED_ONLY,
    CACHE_FILE,
    ENTRY_POINT_GROUP,
    PATH,
    VERSION,
    bundled,
    cache_path,
    cached,
    known_plugins,
    refresh,
    wants_refresh,
)

HOLE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TYPES = {"string": str, "integer": int, "number": float, "file": str}
# The clients of the CLI's own a command names with ``client``: (module, function), the function taking the parsed
# arguments and the plugin's id and giving (what --json prints, the line printed without it)
CLIENTS = {
    "browser_post": ("evo_agents.hub.browser_post_client", "post_command"),
    "browser_login": ("evo_agents.hub.browser_post_client", "login_command"),
}
EXIT_UNCONFIRMED = 2


# The arguments of a command, from its entry of the list


def arg_options(arg: dict) -> dict:
    """The options of ``add_argument`` that argument ``arg`` of a command gives whether it is positional or a flag."""
    options: dict = {"help": arg.get("help")}
    if arg["type"] == "boolean":
        options["action"] = "store_true"
        return options
    options["type"] = TYPES[arg["type"]]
    if arg.get("choices"):
        options["choices"] = arg["choices"]
    if arg.get("repeatable"):
        options["action"] = "append"
    if arg.get("default") is not None:
        options["default"] = arg["default"]
    return options


def arg_of(arg: dict) -> Arg:
    """Argument ``arg`` of a command's entry as an :class:`Arg`."""
    options = arg_options(arg)
    metavar = arg.get("metavar") or arg["name"].upper()
    if arg.get("positional"):
        if arg.get("repeatable"):
            options.pop("action", None)
            options["nargs"] = "+" if arg.get("required") else "*"
        elif not arg.get("required"):
            options["nargs"] = "?"
        return Arg(arg["name"], metavar=metavar, **options)
    if arg["type"] != "boolean":
        options["metavar"] = metavar
        options["required"] = bool(arg.get("required"))
    if arg.get("values"):
        options["nargs"] = len(arg["values"])
        options["metavar"] = tuple(name.upper() for name in arg["values"])
    return Arg(arg.get("flag") or "--" + arg["name"].replace("_", "-"), dest=arg["name"], **options)


def plugin_args(plugin: str, command: dict) -> list[tuple[str | None, Arg]]:
    """The arguments of ``command`` of ``plugin`` in their order, each with the group of flags that exclude each
    other it belongs to (None for none): ``--project``, those of the entry, the switch of ``confirm`` and ``--json``."""
    found: list[tuple[str | None, Arg]] = []
    if command.get("project"):
        found.append(
            (None, Arg("--project", required=True, metavar="PROJECT", help=f"the project that enabled plugin {plugin}"))
        )
    found += [(None if arg.get("positional") else arg.get("group"), arg_of(arg)) for arg in command.get("args") or []]
    confirm = command.get("confirm")
    if confirm:
        found.append(
            (None, Arg(confirm["flag"], dest="plugin_confirmed", action="store_true", help="act without asking"))
        )
    found.append((None, Arg("--json", action="store_true", help="print what the hub answered")))
    return found


def json_output_of(command: dict) -> JsonOutput | None:
    """What ``--json`` of ``command`` prints, when its entry declares the keys (the contract)."""
    output = command.get("output") or {}
    if output.get("keys") is None:
        return None
    return JsonOutput(output.get("json") or "object", tuple(output["keys"]), output.get("schema"))


# Running a command


def _given(value) -> bool:
    return value is not None and value is not False and value != []


def route_of(command: dict, values: dict) -> dict:
    """The ``http`` of ``command`` with argument ``values``: that of the first ``when`` whose argument is given."""
    for entry in command.get("when") or ():
        if _given(values.get(entry["arg"])):
            return entry["http"]
    return command["http"]


def request_of(plugin: str, command: dict, values: dict, render: str | None = None) -> tuple[str, str, dict | None]:
    """(method, path with its query, JSON body or None) of ``command`` of ``plugin`` with argument ``values``; a
    ``render`` asks the route for its own words in the query."""
    http = route_of(command, values)
    wanted = set(HOLE.findall(http["path"]))
    query, body, holes = [], {}, {}
    for arg in command.get("args") or []:
        value = values.get(arg["name"])
        if value is None or (arg["type"] == "boolean" and value is False and arg["in"] != "body"):
            continue
        if arg["type"] == "file":
            value = Path(value).read_text(encoding="utf-8")
        if arg["in"] == "path":
            if arg["name"] in wanted:
                holes[arg["name"]] = quote(str(value), safe="")
        elif arg["in"] == "query":
            for item in value if isinstance(value, list) else [value]:
                query.append((arg["name"], str(item).lower() if isinstance(item, bool) else str(item)))
        elif arg.get("values"):
            body.update(zip(arg["values"], value, strict=True))
        else:
            body[arg["name"]] = value
    path = HOLE.sub(lambda found: holes[found[1]], http["path"]).lstrip("/")
    base = f"/v1/x/{quote(plugin, safe='')}"
    if command.get("project"):
        base += f"/{quote(values['project'], safe='')}"
    full = f"{base}/{path}" if path else base
    if render:
        query.append(("render", render))
    if query:
        full += "?" + urlencode(query)
    method = http["method"]
    return method, full, (body if method in ("POST", "PUT", "PATCH") else None)


def answer_view(found, output: dict) -> View:
    """How a person reads the answer ``found`` of a route, as ``output`` of the command's entry says."""
    if output.get("render") == "text" and isinstance(found, dict) and isinstance(found.get("text"), str):
        return Text(found["text"]) if found["text"] else NOTHING
    rows = found
    if isinstance(found, dict) and output.get("json") == "array":
        rows = next((value for value in found.values() if isinstance(value, list)), found)
    columns = output.get("table")
    if columns and isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
        return Table(tuple(columns), [tuple("" if row.get(c) is None else row.get(c) for c in columns) for row in rows])
    key = output.get("text")
    if key and isinstance(found, dict) and key in found:
        return Text(str(found[key]))
    return Text(json_text(found))


class _Words(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class Unconfirmed(Exception):
    """A command whose entry has ``confirm`` was run without its switch: ``str`` is what to tell the person."""


class PluginRoutes:
    """The routes of one plugin's command below ``/v1/x/<id>/`` (Gateway): the request its arguments make, and the
    call of it with the member's token."""

    def __init__(self, plugin: str, command: dict):
        self.plugin, self.command = plugin, command

    def request(self, values: dict, render: str | None = None) -> tuple[str, str, dict | None]:
        return request_of(self.plugin, self.command, values, render)

    def call(self, hub: Hub, request: tuple[str, str, dict | None]) -> Any:
        method, path, body = request
        return hub.call(method, path, body)


class PluginCommand(HubCommand):
    """One command of a plugin, built from its entry of the hub's list."""

    def __init__(self, plugin: str, entry: dict):
        self.plugin, self.entry = plugin, entry

    def attach(self, subparsers: argparse._SubParsersAction) -> None:
        self.configure(subparsers.add_parser(self.entry["name"], help=self.entry.get("help")))

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        groups: dict[str, argparse._MutuallyExclusiveGroup] = {}
        for group, arg in plugin_args(self.plugin, self.entry):
            target: argparse.ArgumentParser | argparse._MutuallyExclusiveGroup = parser
            if group:
                if group not in groups:
                    groups[group] = parser.add_mutually_exclusive_group()
                target = groups[group]
            target.add_argument(*arg.flags, **arg.options)
        parser.set_defaults(plugin_id=self.plugin, plugin_spec=self.entry)
        output = json_output_of(self.entry)
        if output is not None:
            parser.set_defaults(json_output=output)

    def check(self, args: argparse.Namespace) -> None:
        confirm = self.entry.get("confirm")
        if confirm and not getattr(args, "plugin_confirmed", False):
            words = _Words({key: "" if value is None else value for key, value in vars(args).items()})
            raise Unconfirmed(confirm["text"].format_map(words))

    def failed(self, exc: Exception) -> int | None:
        if isinstance(exc, Unconfirmed):
            print(exc, file=sys.stderr)
            return EXIT_UNCONFIRMED
        return super().failed(exc)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        client = CLIENTS.get(self.entry.get("client") or "")
        if client is not None:
            module, name = client
            found, said = getattr(importlib.import_module(module), name)(args, self.plugin)
            return Result(found, Text(said) if said else NOTHING)
        if self.entry.get("client"):
            raise HubError(f"this CLI has no client {self.entry['client']}: update evo-agents")
        output = self.entry.get("output") or {}
        routes = PluginRoutes(self.plugin, self.entry)
        request = routes.request(vars(args), None if args.json else output.get("render"))
        found = routes.call(ctx.hub, request)
        return Result(found, answer_view(found, output))


class Extensions:
    """The commands of the plugins under ``hub`` (a node of ``evo_agents.hub.cli``): a ``Group`` for each plugin of
    ``plugins``, of the list kept (``known_plugins``) when none is given, its commands below it. A plugin whose id is a
    command attached already is left out."""

    def __init__(self, plugins: list[dict] | None = None):
        self.plugins = plugins

    def groups(self, taken: set[str]) -> list[Group]:
        plugins = known_plugins() if self.plugins is None else self.plugins
        found = []
        for plugin in plugins:
            name = plugin.get("id")
            if not isinstance(name, str) or name in taken or not plugin.get("commands"):
                continue
            commands = [PluginCommand(name, command) for command in plugin["commands"]]
            help = f"commands of plugin {name} {plugin.get('version', '')}".rstrip()
            found.append(Group(name, help, commands, dest="plugin_command"))
        return found

    def attach(self, subparsers: argparse._SubParsersAction) -> None:
        for group in self.groups(set(subparsers.choices)):
            group.attach(subparsers)


def refreshed(argv: list[str]) -> bool:
    """Ask the hub for its list again when ``argv`` names a hub command the CLI has no parser for (``wants_refresh``,
    against ``OWN_COMMANDS`` of ``evo_agents.hub.cli``, which building the parser filled); whether it did."""
    from evo_agents.hub.cli import OWN_COMMANDS

    if not wants_refresh(argv, set(OWN_COMMANDS), known_plugins()):
        return False
    refresh()
    return True


EXTENSIONS = Extensions()  # hub <plugin> <command>, from the hub's list
