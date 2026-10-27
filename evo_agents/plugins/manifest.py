"""The manifest of a plugin, ``evo-plugin.json`` at the top of its bundle, read against
``evo_agents/harness/schemas/plugin.schema.json`` (the seam plugin-v1).

``read_manifest`` takes the manifest as JSON bytes or as an object already parsed and gives a ``Manifest``, or raises
ManifestError listing every problem: the schema's, then what a schema cannot say. Contributions to one extension
point have distinct names; a contribution's ``python`` (``module:attribute`` of the code that implements it, run in
the hub's or the worker's process) belongs to a core plugin only, and so does a ``backend`` to an isolated one; the
paths a backend names stay inside the bundle; the ``engines`` range must read (``evo_agents.plugins.compat``); the
config's ``required`` names its own properties.

A core plugin implements its contributions to the points of the hub (channels, routes, MCP tools, jobs, events) in
Python, which each names with ``python`` (one that names none does nothing there); a channel of a core plugin is a kind
of members' channel, named as the hub names those (``CHANNEL_KIND``). Only a core plugin owns tables (``tables``), and
each one's name starts with the plugin's table prefix (``table_prefix``): its id with ``_`` for ``-``, then ``_``. The
tables come from a chain of migrations, never from the plugin at run time: the plugin's own (root key ``schema``, its
``metadata``, its ``migrations`` package and the tables it ``adopts``, which ``evo-agents hub migrate`` runs after the
hub's chain), or, for the tables of crm, pm and marketing, the hub's older chain. A core plugin's kinds of notification
(``notice_kinds``) start with its table prefix too. Only a core plugin contributes a
source of a project's knowledge graph (``kg_sources``), at most one, which the hub reads in its own process when it
builds the graph of a project that enables the plugin (``evo_agents.hub.kg_sources``).

A page (``pages``) of an isolated plugin names its HTML file in the bundle under ``pages/`` (``entry``), which the web
shows in a sandboxed iframe. A core plugin's page names one too when the plugin has ``ui`` (``module:directory``, the
directory of its package that holds ``pages/``), and is framed the same way; otherwise it is native, a page of the hub's
web itself, and names no entry.

A command (``cli``) is one of ``evo-agents hub <id> <name>``: its arguments, the route of the plugin it calls and how
its answer prints (``evo_agents.hub.cli_plugins``); its path arguments appear in the route's path, a positional has no
flag, and each argument has one name.

A runtime (``runtimes``) names one of ``acp``, the command of an agent that speaks the Agent Client Protocol, which
any plugin may declare (``evo_agents.plugins.runtimes``), and ``python``, a core plugin's adapter of a runtime of the
worker; within the bounds of MAX_COMMAND_ITEMS, MAX_RUNTIME_MODELS and MAX_RUNTIME_SECRETS, each model and secret once.

An environment (``environments``) is a container image pinned by its digest, its network and at most
MAX_ENVIRONMENT_MOUNTS mounts (``evo_agents.plugins.run_environments``): a volume names itself and a tmpfs does not,
only a tmpfs has a size, each target is a normalized absolute path, once, and none is ``/`` or under the kernel's
file systems (FORBIDDEN_TARGETS).

An agent pack (``agent_packs``) is a directory ``packs/<name>/`` of the bundle laid out as a Claude Code plugin, which
an agent profile attaches; ``evo_agents.plugins.agent_packs`` reads it and ``bundle.read_bundle`` checks it.

An agent type (``agent_types``) is the template of an agent profile with the packs, the environment, the jobs, the
events and the pages it uses, each of them a contribution of the plugin itself, named once; no two types of a plugin
share a handle (``evo_agents.hub.task_runs``).

The schema file is read where the package installs it, next to the harness schemas, without importing
``evo_agents.harness``. Standard library only, besides ``evo_agents.schema``.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from evo_agents.plugins.compat import POINT_VERSIONS, SpecError, parse_spec
from evo_agents.plugins.protocol import BACKEND_POINTS
from evo_agents.schema import errors, validate

MANIFEST_FILE = "evo-plugin.json"
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "harness" / "schemas" / "plugin.schema.json"
MAX_MANIFEST = 256 * 1024  # bytes of evo-plugin.json
PLUGIN_ID = r"^[a-z][a-z0-9-]{0,62}$"  # a manifest's id, as plugin.schema.json has it
TIERS = ("isolated", "core")
EXTENSION_POINTS = tuple(POINT_VERSIONS)  # each with the versions of its contract this release serves
CORE_ONLY_POINTS = ("tables", "kg_sources", "loop_rules", "notice_kinds")  # what only a core plugin contributes
# what a project enables a plugin for, besides its permissions, its config and its routes and runtimes of a project
PROJECT_POINTS = (
    "mcp_tools",
    "jobs",
    "events",
    "pages",
    "agent_packs",
    "agent_types",
    "environments",
    "kg_sources",
    "loop_rules",
)
# the keys of an agent type that name contributions of the plugin's own, and the point of each
AGENT_TYPE_POINTS = {"packs": "agent_packs", "jobs": "jobs", "events": "events", "pages": "pages"}
CHANNEL_KIND = r"^[a-z][a-z0-9_-]{0,31}$"  # a kind of members' channel (notification_channels.kind), never "web"
MAX_LOOP_RULE_LISTS = {"on": 16, "emits": 16, "kinds": 8}  # the lists of a Loop rule
MAX_COMMAND_ITEMS = 32  # the program and arguments of an ACP runtime's command
MAX_RUNTIME_MODELS = 200  # the models an ACP runtime lists, as a worker's heartbeat bounds them
MAX_RUNTIME_SECRETS = 20  # the variables an ACP runtime's agent needs
MAX_ENVIRONMENT_MOUNTS = 8  # the mounts an environment declares, besides those the worker always makes
FORBIDDEN_TARGETS = ("/proc", "/sys", "/dev")  # no mount of an environment goes there, nor under them, nor on /
PERMISSIONS = (
    "runs:read",
    "runs:dispatch",
    "chats:read",
    "chats:write",
    "plans:read",
    "memories:read",
    "notifications:send",
    "canvas:read",
    "signals:read",
    "srs:read",
    "tasks:read",
)
# What each permission lets a plugin do on a project that enables it, as an admin of the project reads it before
# granting it.
PERMISSION_TEXT = {
    "runs:read": "read the project's runs, their events and their outcomes",
    "runs:dispatch": "dispatch runs in the project",
    "chats:read": "read the project's group chats",
    "chats:write": "post messages in the project's group chats",
    "plans:read": "read the project's plans",
    "memories:read": "read the project's memories",
    "notifications:send": "send notifications to the project's members",
    "canvas:read": "read the project's Lean Canvases: each claim's revs and each approved baseline",
    "signals:read": "read the project's signals: each batch the hub takes",
    "srs:read": "read the project's SRS documents: each version approved, with the codes of the items it changed",
    "tasks:read": "read the project's tasks: each status a task takes, with what the task links to",
}


CORE_ONLY_TEXT = {
    "tables": "only a core plugin owns tables, which the hub's migrations create",
    "kg_sources": "only a core plugin contributes a source of the knowledge graph, which the hub reads in its process",
    "loop_rules": "only a core plugin adds rules to the Loop, which the hub calls in its process",
    "notice_kinds": "only a core plugin adds kinds of notification, which it sends through the core plugin API",
}
CORE_ONLY_KEYS = {
    "schema": "only a core plugin carries tables and a chain of migrations of its own",
    "ui": "only a core plugin names a directory of its package for its pages; an isolated plugin's are in its bundle",
    "egress": "only a core plugin declares the sinks it sends a project's text out through, in the hub's process",
}


def table_prefix(plugin_id: str) -> str:
    """What the name of every table plugin ``plugin_id`` owns starts with: ``telegram_`` for telegram, ``srs_docs_``
    for srs-docs."""
    return plugin_id.replace("-", "_") + "_"


class ManifestError(ValueError):
    """A manifest that is not one: ``problems`` says each thing that is wrong, with where."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@cache
def schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Manifest:
    """A manifest that passed every check. ``data`` is the object as it was read."""

    id: str
    version: str
    publisher: str
    tier: str
    engines: dict
    plugin_api: int
    name: str
    description: str
    permissions: tuple[str, ...]
    config: dict | None
    contributes: dict[str, tuple[dict, ...]]
    backend: dict | None
    data: dict = field(repr=False, compare=False)

    def contributions(self, point: str) -> tuple[dict, ...]:
        """What the plugin contributes to extension point ``point``, in the manifest's order."""
        return self.contributes.get(point, ())

    def serves(self) -> tuple[str, ...]:
        """The extension points an isolated plugin serves from its backend, in BACKEND_POINTS' order."""
        return tuple(point for point in BACKEND_POINTS if self.contributions(point)) if self.tier == "isolated" else ()

    def python_refs(self, point: str) -> dict[str, str]:
        """name -> ``module:attribute`` of the contributions to ``point`` that name their Python."""
        return {item["name"]: item["python"] for item in self.contributions(point) if "python" in item}

    def contribution(self, point: str, name: str) -> dict | None:
        """The contribution named ``name`` to extension point ``point``, or None."""
        return next((item for item in self.contributions(point) if item["name"] == name), None)

    def pages(self) -> tuple[dict, ...]:
        """The pages the plugin contributes to the hub's web, in the manifest's order."""
        return self.contributions("pages")

    def tables(self) -> tuple[str, ...]:
        """The tables a core plugin owns, which the hub's migrations create."""
        return tuple(item["name"] for item in self.contributions("tables"))

    @property
    def ui(self) -> str | None:
        """``module:directory`` of a core plugin's page files, or None."""
        return self.data.get("ui")

    @property
    def egress(self) -> tuple[dict, ...]:
        """The sinks a core plugin sends project text out through (root key ``egress``): id, kind and host each."""
        return tuple(self.data.get("egress") or ())

    @property
    def plugin_schema(self) -> dict | None:
        """A core plugin's own tables and chain of migrations (root key ``schema``), or None."""
        return self.data.get("schema")

    def per_project(self) -> bool:
        """Whether a project enables the plugin for anything (``is_per_project``)."""
        return is_per_project(self.data)


def is_per_project(data: dict) -> bool:
    """Whether a project enables the plugin of manifest ``data`` for anything: it is isolated, asks for a permission,
    takes a config, declares an egress sink (it reads a project's text only where the project enabled it), or contributes an MCP tool, a job, an event, a page, an agent pack, an agent type, an ACP runtime,
    an environment, a source of the knowledge graph or a route under /v1/x/<id>/<project>. A core plugin that does none of that (Telegram: a channel of
    members and routes of its own) runs hub-wide wherever it is installed: the hub's store lists it, installed with
    the image, and no project enables it. ``data`` is a manifest that was read once (the store keeps it so), not
    checked again here."""
    contributes = data.get("contributes") or {}
    if (
        data.get("tier", "isolated") == "isolated"
        or data.get("permissions")
        or data.get("config")
        or data.get("egress")
    ):
        return True
    if any(contributes.get(point) for point in PROJECT_POINTS):
        return True
    if any("acp" in item for item in contributes.get("runtimes") or ()):
        return True
    return any("prefix" not in item for item in contributes.get("routes") or ())


def _semantics(data: dict) -> list[str]:
    """What the schema cannot check: unique names per point, Python in core plugins only, a range that reads, and a
    config whose required properties exist."""
    found = []
    core = data.get("tier", "isolated") == "core"
    for point, items in (data.get("contributes") or {}).items():
        names = [item["name"] for item in items]
        for name in sorted({name for name in names if names.count(name) > 1}):
            found.append(f"contributes.{point}: {name!r} is contributed twice")
        for index, item in enumerate(items):
            found.extend(_contribution_problems(data["id"], point, index, item, core))
    found.extend(_backend_problems(data, core))
    for key, text in CORE_ONLY_KEYS.items():
        if key in data and not core:
            found.append(f"{key}: {text}")
    sinks = [item["id"] for item in data.get("egress") or ()]
    for sink in sorted({sink for sink in sinks if sinks.count(sink) > 1}):
        found.append(f"egress: sink {sink!r} is listed twice")
    for index, item in enumerate((data.get("contributes") or {}).get("pages") or ()):
        if core and "entry" in item and "ui" not in data:
            found.append(
                f"contributes.pages[{index}].entry: a core plugin's page is native, a page of the hub's web, unless "
                "the plugin names ui, the directory of its package that holds pages/, for a framed page"
            )
    found.extend(_agent_type_problems(data.get("contributes") or {}))
    try:
        parse_spec(data["engines"]["evo-agents"])
    except SpecError as exc:
        found.append(f"engines.evo-agents: {exc}")
    config = data.get("config") or {}
    declared = set(config.get("properties") or {})
    for name in config.get("required") or []:
        if name not in declared:
            found.append(f"config.required: {name!r} is not one of the config's properties")
    permissions = data.get("permissions") or []
    for permission in sorted({p for p in permissions if permissions.count(p) > 1}):
        found.append(f"permissions: {permission!r} is listed twice")
    return found


def _contribution_problems(plugin_id: str, point: str, index: int, item: dict, core: bool) -> list[str]:
    """What is wrong with contribution ``item`` to ``point`` of a core (``core``) or an isolated plugin."""
    where = f"contributes.{point}[{index}]"
    if "python" in item and not core:
        return [
            f"{where}.python: only a core plugin runs Python in the hub's or the worker's process; an isolated "
            "plugin implements it in its own process"
        ]
    if point in CORE_ONLY_POINTS and not core:
        return [f"{where}: {CORE_ONLY_TEXT[point]}"]
    if point == "kg_sources" and index > 0:
        return [f"{where}: a plugin contributes at most one source of the knowledge graph, plugin.<its id>"]
    if "prefix" in item and not (core and point == "routes"):
        return [f"{where}.prefix: only a core plugin's route names the path its router is included under"]
    if point == "tables" and not item["name"].startswith(table_prefix(plugin_id)):
        return [f"{where}: table {item['name']!r} must start with {table_prefix(plugin_id)}, the plugin's prefix"]
    if core and point == "channels" and (not re.fullmatch(CHANNEL_KIND, item["name"]) or item["name"] == "web"):
        return [f"{where}: a core plugin's channel is a kind of members' channel: {CHANNEL_KIND}, not web"]
    if point == "loop_rules":
        return _loop_rule_problems(where, item)
    if point == "pages":
        return _page_problems(where, item, core)
    if point == "notice_kinds" and not item["name"].startswith(table_prefix(plugin_id)):
        return [f"{where}: notice kind {item['name']!r} must start with {table_prefix(plugin_id)}, the plugin's prefix"]
    if point == "cli":
        return _command_problems(where, item)
    if point == "runtimes":
        return _runtime_problems(where, item)
    if point == "environments":
        return _environment_problems(where, item)
    return []


def _loop_rule_problems(where: str, item: dict) -> list[str]:
    """What is wrong with Loop rule ``item``: it names a handler, reads an event or the clock, and each kind of work
    item it makes once, a task with the agent type that takes it."""
    found = []
    if "python" not in item:
        found.append(f"{where}.python: a Loop rule names the handler the hub calls, module:attribute")
    if not item.get("on") and not item.get("schedule"):
        found.append(f"{where}: a Loop rule reads at least one event (on) or the clock (schedule)")
    for key, bound in MAX_LOOP_RULE_LISTS.items():
        if len(item.get(key) or ()) > bound:
            found.append(f"{where}.{key}: at most {bound} items")
    kinds = [entry["kind"] for entry in item.get("kinds") or ()]
    found += [f"{where}.kinds: {kind!r} is listed twice" for kind in sorted({k for k in kinds if kinds.count(k) > 1})]
    for index, entry in enumerate(item.get("kinds") or ()):
        if entry["run"] == "task" and "agent_type" not in entry:
            found.append(f"{where}.kinds[{index}]: run task names the agent_type that takes the task")
        if entry["run"] == "job" and "agent_type" in entry:
            found.append(f"{where}.kinds[{index}]: run job has no agent_type")
    return found


def _agent_type_problems(contributes: dict) -> list[str]:
    """What is wrong with the agent types of ``contributes``: each names packs, jobs, events, pages and an environment
    of the plugin's own, each once, and no two share a handle."""
    found, handles = [], []
    for index, item in enumerate(contributes.get("agent_types") or ()):
        where = f"contributes.agent_types[{index}]"
        handles.append(item["profile"]["handle"])
        for key, point in AGENT_TYPE_POINTS.items():
            own = {entry["name"] for entry in contributes.get(point) or ()}
            named = item.get(key) or []
            alien = [name for name in dict.fromkeys(named) if name not in own]
            found += [f"{where}.{key}: {name!r} is not one of the plugin's {point}" for name in alien]
            twice = sorted({name for name in named if named.count(name) > 1})
            found += [f"{where}.{key}: {name!r} is named twice" for name in twice]
        environment = item.get("environment")
        if environment is not None and environment not in {e["name"] for e in contributes.get("environments") or ()}:
            found.append(f"{where}.environment: {environment!r} is not one of the plugin's environments")
    for handle in sorted({handle for handle in handles if handles.count(handle) > 1}):
        found.append(f"contributes.agent_types: two agent types have the handle {handle!r}")
    return found


def _runtime_problems(where: str, item: dict) -> list[str]:
    """What is wrong with runtime ``item``: it names one of acp and python, within the bounds, each model and secret
    once."""
    if ("acp" in item) == ("python" in item):
        return [f"{where}: a runtime names one of acp (an ACP agent's command) and python (a core plugin's adapter)"]
    found = []
    if len(item.get("acp", {}).get("command", ())) > MAX_COMMAND_ITEMS:
        found.append(f"{where}.acp.command: at most {MAX_COMMAND_ITEMS} items")
    for key, bound in (("models", MAX_RUNTIME_MODELS), ("secrets", MAX_RUNTIME_SECRETS)):
        values = item.get(key) or []
        if len(values) > bound:
            found.append(f"{where}.{key}: at most {bound} items")
        found += [
            f"{where}.{key}: {value!r} is listed twice" for value in sorted(set(values)) if values.count(value) > 1
        ]
    return found


def _environment_problems(where: str, item: dict) -> list[str]:
    """What is wrong with environment ``item``: at most MAX_ENVIRONMENT_MOUNTS mounts, each target once, normalized and
    clear of FORBIDDEN_TARGETS; a volume with a name and no size, a tmpfs without a name."""
    mounts = item.get("mounts") or []
    found = [f"{where}.mounts: at most {MAX_ENVIRONMENT_MOUNTS} items"] if len(mounts) > MAX_ENVIRONMENT_MOUNTS else []
    targets = [mount["target"] for mount in mounts]
    found += [
        f"{where}.mounts: target {target!r} is listed twice"
        for target in sorted(set(targets))
        if targets.count(target) > 1
    ]
    for index, mount in enumerate(mounts):
        found += _mount_problems(f"{where}.mounts[{index}]", mount)
    return found


def _mount_problems(where: str, mount: dict) -> list[str]:
    target = mount["target"]
    found = []
    if posixpath.normpath(target) != target or target.startswith("//"):
        found.append(f"{where}.target: {target!r} is not a normalized absolute path")
    if target == "/" or any(target == top or target.startswith(top + "/") for top in FORBIDDEN_TARGETS):
        found.append(f"{where}.target: {target!r} is /, or one of {', '.join(FORBIDDEN_TARGETS)} or under it")
    if mount["type"] == "volume":
        if "name" not in mount:
            found.append(f"{where}: a volume names itself (name)")
        if "size_mb" in mount:
            found.append(f"{where}.size_mb: only a tmpfs has a size")
    elif "name" in mount:
        found.append(f"{where}.name: only a volume has a name")
    return found


def _command_problems(where: str, item: dict) -> list[str]:
    """What is wrong with command ``item``: an argument named twice, a positional with a flag or a flag twice, a path
    argument no route's path has, a ``{name}`` of a path no argument fills, a ``when`` or ``confirm`` that names no
    argument or a flag taken, or ``values`` and ``group`` on a positional."""
    found = []
    args = item.get("args") or ()
    names = [arg["name"] for arg in args]
    flags = [arg["flag"] for arg in args if "flag" in arg]
    confirm = (item.get("confirm") or {}).get("flag")
    for name in sorted({name for name in names if names.count(name) > 1}):
        found.append(f"{where}.args: {name!r} is named twice")
    for flag in sorted({flag for flag in flags if flags.count(flag) > 1}):
        found.append(f"{where}.args: {flag} is given twice")
    if confirm is not None and confirm in {arg.get("flag") or "--" + arg["name"].replace("_", "-") for arg in args}:
        found.append(f"{where}.confirm.flag: {confirm} is the flag of an argument")
    routes = [("http.path", item["http"]["path"])]
    routes += [(f"when[{i}].http.path", entry["http"]["path"]) for i, entry in enumerate(item.get("when") or ())]
    holes = {key: set(re.findall(r"{([a-z][a-z0-9_]*)}", path)) for key, path in routes}
    every_hole = set().union(*holes.values())
    paths = " or ".join(repr(path) for _, path in routes)
    for index, arg in enumerate(args):
        if arg.get("positional") and "flag" in arg:
            found.append(f"{where}.args[{index}]: a positional argument has no flag")
        if arg.get("positional") and ("values" in arg or "group" in arg):
            found.append(f"{where}.args[{index}]: values and group are for a flag, not a positional argument")
        if "values" in arg and (arg["in"] != "body" or arg.get("repeatable") or arg["type"] != "string"):
            found.append(f"{where}.args[{index}]: an argument with values is one string per name, sent in the body")
        if arg["in"] == "path" and arg["name"] not in every_hole:
            found.append(f"{where}.args[{index}]: path argument {arg['name']!r} is not {{{arg['name']}}} of {paths}")
        if arg["in"] == "path" and (arg.get("repeatable") or arg["type"] in ("boolean", "file")):
            found.append(f"{where}.args[{index}]: a path argument is one string or number")
    filled = {arg["name"] for arg in args if arg["in"] == "path"}
    for key, _ in routes:
        for hole in sorted(holes[key] - filled):
            found.append(f"{where}.{key}: {{{hole}}} is filled by no path argument")
    for index, entry in enumerate(item.get("when") or ()):
        if entry["arg"] not in names:
            found.append(f"{where}.when[{index}].arg: {entry['arg']!r} is not one of the command's arguments")
    return found


def _page_problems(where: str, item: dict, core: bool) -> list[str]:
    """What is wrong with page ``item``: a core plugin's page is native and names no entry unless the plugin has
    ``ui`` (checked with the manifest's root, ``_semantics``); a page that names one, of either tier, names an HTML
    file inside pages/."""
    entry = item.get("entry")
    if core and entry is None:
        return []
    if entry is None:
        return [f"{where}: an isolated plugin's page names its entry, an HTML file of its bundle under pages/"]
    if any(part in ("", ".", "..") for part in entry.split("/")):
        return [f"{where}.entry: {entry!r} must be a path inside the bundle's pages/, without empty, . or .. parts"]
    return []


def _backend_problems(data: dict, core: bool) -> list[str]:
    backend = data.get("backend")
    if backend is None:
        return []
    if core:
        return [
            "backend: a core plugin runs Python in the hub's and the worker's processes; only an isolated one "
            "has a backend"
        ]
    found = []
    for key in ("entry", "requirements"):
        path = backend.get(key)
        if path is not None and any(part in ("", ".", "..") for part in path.split("/")):
            found.append(f"backend.{key}: {path!r} must be a path inside the bundle, without empty, . or .. parts")
    return found


def _parsed(source: bytes | str | dict) -> dict:
    if isinstance(source, dict):
        return source
    if len(source) > MAX_MANIFEST:
        raise ManifestError([f"{MANIFEST_FILE} is over {MAX_MANIFEST // 1024} KiB"])
    try:
        data = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError([f"{MANIFEST_FILE} is not JSON: {exc}"]) from None
    if not isinstance(data, dict):
        raise ManifestError([f"{MANIFEST_FILE} holds {type(data).__name__}, not an object"])
    return data


def read_manifest(source: bytes | str | dict) -> Manifest:
    """The manifest ``source`` holds, checked whole; ManifestError listing every problem otherwise."""
    data = _parsed(source)
    problems = [f"{issue.path or '<root>'}: {issue.message}" for issue in errors(validate(data, schema()))]
    if problems:
        raise ManifestError(problems)
    problems = _semantics(data)
    if problems:
        raise ManifestError(problems)
    contributes = {point: tuple(items) for point, items in (data.get("contributes") or {}).items()}
    return Manifest(
        id=data["id"],
        version=data["version"],
        publisher=data["publisher"],
        tier=data.get("tier", "isolated"),
        engines=dict(data["engines"]),
        plugin_api=data["plugin_api"],
        name=data.get("name") or data["id"],
        description=data.get("description", ""),
        permissions=tuple(data.get("permissions") or ()),
        config=data.get("config"),
        contributes=contributes,
        backend=data.get("backend"),
        data=data,
    )
