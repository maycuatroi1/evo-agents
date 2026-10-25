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
that key), else as ``output.text`` (a key of an object answer), else as JSON. Standard library only, like the rest of
the client.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.client import Hub, HubError, hub_dir, load_credentials

PATH = "/v1/cli/plugins"
CACHE_FILE = "cli-plugins.json"
VERSION = 1
HOLE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TYPES = {"string": str, "integer": int, "number": float, "file": str}


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


# The parsers


def register_plugin_commands(hsub, plugins: list[dict] | None = None) -> None:
    """A parser for each plugin of the list kept, under ``hub``, its commands below it."""
    plugins = cached()["plugins"] if plugins is None else plugins
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
        parser.add_argument("--project", required=True, metavar="PROJECT", help=f"a project that enabled {plugin}")
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
        if arg.get("positional"):
            if arg.get("repeatable"):
                options.pop("action", None)
                options["nargs"] = "+" if arg.get("required") else "*"
            elif not arg.get("required"):
                options["nargs"] = "?"
            parser.add_argument(arg["name"], metavar=arg["name"].upper(), **options)
        else:
            flag = arg.get("flag") or "--" + arg["name"].replace("_", "-")
            if kind != "boolean":
                options["metavar"] = arg["name"].upper()
                options["required"] = bool(arg.get("required"))
            parser.add_argument(flag, dest=arg["name"], **options)
    parser.add_argument("--json", action="store_true", help="print what the hub answered")
    parser.set_defaults(func=run_command, plugin_id=plugin, plugin_spec=command)


# Running a command


def request_of(plugin: str, command: dict, values: dict) -> tuple[str, str, dict | None]:
    """(method, path with its query, JSON body or None) of ``command`` of ``plugin`` with argument ``values``."""
    query, body, holes = [], {}, {}
    for arg in command.get("args") or []:
        value = values.get(arg["name"])
        if value is None or (arg["type"] == "boolean" and value is False and arg["in"] != "body"):
            continue
        if arg["type"] == "file":
            value = Path(value).read_text(encoding="utf-8")
        if arg["in"] == "path":
            holes[arg["name"]] = quote(str(value), safe="")
        elif arg["in"] == "query":
            for item in value if isinstance(value, list) else [value]:
                query.append((arg["name"], str(item).lower() if isinstance(item, bool) else str(item)))
        else:
            body[arg["name"]] = value
    path = HOLE.sub(lambda found: holes[found[1]], command["http"]["path"]).lstrip("/")
    base = f"/v1/x/{quote(plugin, safe='')}"
    if command.get("project"):
        base += f"/{quote(values['project'], safe='')}"
    full = f"{base}/{path}" if path else base
    if query:
        full += "?" + urlencode(query)
    method = command["http"]["method"]
    return method, full, (body if method in ("POST", "PUT", "PATCH") else None)


def _print(found, output: dict) -> None:
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


@client_command
def run_command(args) -> int:
    command = args.plugin_spec
    method, path, body = request_of(args.plugin_id, command, vars(args))
    hub, _ = signed_in()
    found = hub.call(method, path, body)
    if args.json:
        print_json(found)
    else:
        _print(found, command.get("output") or {})
    return 0


def refreshed(argv: list[str]) -> bool:
    """Ask the hub for its list again when ``argv`` names a hub command the CLI has no parser for (``wants_refresh``,
    against ``OWN_COMMANDS`` of ``evo_agents.hub.cli``, which building the parser filled); whether it did."""
    from evo_agents.hub.cli import OWN_COMMANDS

    if not wants_refresh(argv, set(OWN_COMMANDS), cached()["plugins"]):
        return False
    refresh()
    return True
