"""``evo-agents hub <plugin> <command>``: the commands plugins add to the CLI (extension point ``cli``), read from the
hub rather than shipped with the CLI, so a plugin adds one without a release of evo-agents.

The hub lists them at ``GET /v1/cli/plugins`` (``evo_agents.hub.server.plugin_cli``); the CLI keeps the answer in
``cli-plugins.json`` of the login directory (``~/.evo/hub``), and registers a parser for each plugin from
it when it builds its own. A ``hub`` command it does not know, or a command of a plugin it does not know, makes it ask
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
``evo_agents.hub.contract``). Standard library only, like the rest of the client.
"""

from __future__ import annotations

import contextvars
import importlib
import importlib.util
import json
import re
import sys
from importlib.metadata import entry_points
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.client import Hub, HubError, hub_dir, load_credentials

PATH = "/v1/cli/plugins"
CACHE_FILE = "cli-plugins.json"
VERSION = 1
HOLE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TYPES = {"string": str, "integer": int, "number": float, "file": str}
ENTRY_POINT_GROUP = "evo_agents.plugins"  # evo_agents.plugins.core
BUNDLED_FILE = "cli.json"
# The clients of the CLI's own a command names with ``client``: (module, function), the function taking the parsed
# arguments and the plugin's id and giving (what --json prints, the line printed without it)
CLIENTS = {
    "browser_post": ("evo_agents.hub.browser_post_client", "post_command"),
    "browser_login": ("evo_agents.hub.browser_post_client", "login_command"),
}


# The list the hub gives


def cache_path() -> Path:
    return hub_dir() / CACHE_FILE


def cached() -> dict:
    """The list as the hub last gave it, ``{"plugins": [...]}``; empty when none was kept or it does not read."""
    try:
        found = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, HubError):
        return {"plugins": []}
    if not isinstance(found, dict) or not isinstance(found.get("plugins"), list):
        return {"plugins": []}
    return found


def refresh() -> dict:
    """Ask the hub for its list and keep it; the one kept when the hub cannot be asked (not signed in, unreachable,
    an older hub without the list), so the CLI works as before."""
    held = cached()
    try:
        credentials = load_credentials()
        body = Hub(credentials.url, credentials.token).call("GET", PATH)
    except (HubError, OSError):
        return held
    if not isinstance(body, dict) or body.get("version") != VERSION or not isinstance(body.get("plugins"), list):
        return held
    found = {"plugins": body["plugins"]}
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return found


def wants_refresh(argv: list[str], known: set[str], plugins: list[dict]) -> bool:
    """Whether ``argv`` (of ``evo-agents``) names a ``hub`` command the CLI has no parser for: a group that is neither
    its own nor a plugin's it knows, or a command of a plugin it knows that the list kept lacks."""
    if len(argv) < 2 or argv[0] != "hub" or argv[1].startswith("-"):
        return False
    name = argv[1]
    plugin = next((item for item in plugins if item.get("id") == name), None)
    if plugin is None:
        return name not in known
    if len(argv) < 3 or argv[2].startswith("-"):
        return False
    return argv[2] not in {command.get("name") for command in plugin.get("commands") or []}


def bundled() -> list[dict]:
    """The commands of the core plugins installed with the CLI, ``[{"id", "version", "commands"}]`` by id: each entry
    point of ``evo_agents.plugins`` whose package holds ``cli.json``, found with ``find_spec`` and read as JSON, so no
    plugin is imported. A file that does not read is left out."""
    found = []
    for point in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda point: point.name):
        try:
            spec = importlib.util.find_spec(point.module)
        except (ImportError, ValueError):
            continue
        if spec is None or not spec.submodule_search_locations:
            continue
        for folder in spec.submodule_search_locations:
            try:
                commands = json.loads((Path(folder) / BUNDLED_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(commands, list) and commands:
                version = point.dist.version if point.dist is not None else ""
                found.append({"id": point.name, "version": version, "commands": commands, "bundled": True})
            break
    return found


# True while the contract (``evo_agents.hub.contract``) builds the parser: the bundled plugins alone, so the contract
# does not depend on the list a machine kept from its hub.
BUNDLED_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("bundled_only", default=False)


def known_plugins() -> list[dict]:
    """The bundled plugins, then those of the list kept that no bundled one has the id of (none under BUNDLED_ONLY)."""
    mine = bundled()
    if BUNDLED_ONLY.get():
        return mine
    ids = {plugin["id"] for plugin in mine}
    return mine + [plugin for plugin in cached()["plugins"] if plugin.get("id") not in ids]


# The parsers


def register_plugin_commands(hsub, plugins: list[dict] | None = None) -> None:
    """A parser for each plugin of ``known_plugins``, under ``hub``, its commands below it."""
    plugins = known_plugins() if plugins is None else plugins
    taken = set(hsub.choices)
    for plugin in plugins:
        name = plugin.get("id")
        if not isinstance(name, str) or name in taken or not plugin.get("commands"):
            continue
        group = hsub.add_parser(name, help=f"commands of plugin {name} {plugin.get('version', '')}".rstrip())
        gsub = group.add_subparsers(dest="plugin_command", required=True)
        for command in plugin["commands"]:
            _command(gsub, name, command)


def _command(gsub, plugin: str, command: dict) -> None:
    parser = gsub.add_parser(command["name"], help=command.get("help"))
    if command.get("project"):
        parser.add_argument(
            "--project", required=True, metavar="PROJECT", help=f"the project that enabled plugin {plugin}"
        )
    groups: dict = {}
    for arg in command.get("args") or []:
        kind = arg["type"]
        options: dict = {"help": arg.get("help")}
        if kind == "boolean":
            options["action"] = "store_true"
        else:
            options["type"] = TYPES[kind]
            if arg.get("choices"):
                options["choices"] = arg["choices"]
            if arg.get("repeatable"):
                options["action"] = "append"
            if arg.get("default") is not None:
                options["default"] = arg["default"]
        metavar = arg.get("metavar") or arg["name"].upper()
        if arg.get("positional"):
            if arg.get("repeatable"):
                options.pop("action", None)
                options["nargs"] = "+" if arg.get("required") else "*"
            elif not arg.get("required"):
                options["nargs"] = "?"
            parser.add_argument(arg["name"], metavar=metavar, **options)
        else:
            flag = arg.get("flag") or "--" + arg["name"].replace("_", "-")
            if kind != "boolean":
                options["metavar"] = metavar
                options["required"] = bool(arg.get("required"))
            if arg.get("values"):
                options["nargs"] = len(arg["values"])
                options["metavar"] = tuple(name.upper() for name in arg["values"])
            target = parser
            if arg.get("group"):
                if arg["group"] not in groups:
                    groups[arg["group"]] = parser.add_mutually_exclusive_group()
                target = groups[arg["group"]]
            target.add_argument(flag, dest=arg["name"], **options)
    confirm = command.get("confirm")
    if confirm:
        parser.add_argument(confirm["flag"], dest="plugin_confirmed", action="store_true", help="act without asking")
    parser.add_argument("--json", action="store_true", help="print what the hub answered")
    parser.set_defaults(func=run_command, plugin_id=plugin, plugin_spec=command)
    output = command.get("output") or {}
    if output.get("keys") is not None:
        from evo_agents.hub.contract import JsonOutput

        kind = output.get("json") or "object"
        parser.set_defaults(json_output=JsonOutput(kind, tuple(output["keys"]), output.get("schema")))


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


def _print(found, output: dict) -> None:
    if output.get("render") == "text" and isinstance(found, dict) and isinstance(found.get("text"), str):
        if found["text"]:
            print(found["text"])
        return
    rows = found
    if isinstance(found, dict) and output.get("json") == "array":
        rows = next((value for value in found.values() if isinstance(value, list)), found)
    columns = output.get("table")
    if columns and isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
        if rows:
            table(tuple(columns), [tuple("" if row.get(c) is None else row.get(c) for c in columns) for row in rows])
        return
    key = output.get("text")
    if key and isinstance(found, dict) and key in found:
        print(found[key])
        return
    print_json(found)


class _Words(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@client_command
def run_command(args) -> int:
    command = args.plugin_spec
    values = vars(args)
    confirm = command.get("confirm")
    if confirm and not values.get("plugin_confirmed"):
        words = _Words({key: "" if value is None else value for key, value in values.items()})
        print(confirm["text"].format_map(words), file=sys.stderr)
        return 2
    output = command.get("output") or {}
    client = CLIENTS.get(command.get("client") or "")
    if client is not None:
        module, name = client
        found, said = getattr(importlib.import_module(module), name)(args, args.plugin_id)
        if args.json:
            print_json(found)
        elif said:
            print(said)
        return 0
    if command.get("client"):
        raise HubError(f"this CLI has no client {command['client']}: update evo-agents")
    render = None if args.json else output.get("render")
    method, path, body = request_of(args.plugin_id, command, values, render)
    hub, _ = signed_in()
    found = hub.call(method, path, body)
    if args.json:
        print_json(found)
    else:
        _print(found, output)
    return 0


def refreshed(argv: list[str]) -> bool:
    """Ask the hub for its list again when ``argv`` names a hub command the CLI has no parser for (``wants_refresh``,
    against ``OWN_COMMANDS`` of ``evo_agents.hub.cli``, which building the parser filled); whether it did."""
    from evo_agents.hub.cli import OWN_COMMANDS

    if not wants_refresh(argv, set(OWN_COMMANDS), known_plugins()):
        return False
    refresh()
    return True
