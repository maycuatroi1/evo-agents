"""``evo-agents hub agent list|show|create|edit|delete``: the agent profiles of a project (``evo_agents.hub.profiles``),
over GET, POST, PATCH and DELETE /v1/projects/{p}/agents.

``list`` prints the project's profiles, the default first, with the axes each one limits; ``--deleted`` adds the
deleted ones. ``show`` prints one profile whole, each axis with its allowlist. Every member of the project may read
them. ``create``, ``edit`` and ``delete`` need the admin role on the project.

A profile's permissions are eight axes (credential, tool, skill, worker, project, model, runtime, environment), each
``all`` or an allowlist; an axis a profile does not name, as a hub older than the axis answers, reads as ``all``. ``--allow AXIS:ITEM`` (repeatable) makes AXIS an allowlist of the items given for it, such as ``--allow
tool:read --allow tool:edit --allow credential:github-app``; ``--none AXIS`` makes AXIS an allowlist of nothing, and
``--all AXIS`` (``edit`` only) makes it allow everything again. ``edit`` changes only the axes named, sending the
profile's other axes as they are, with ``if_version`` set to the version it read, so a change made meanwhile is a
refusal rather than lost. ``--instructions-file PATH`` reads the instructions from a file, ``-`` for stdin; ``--avatar
PATH`` sets a PNG, JPEG or WebP image of at most 512 KiB, ``--no-avatar`` drops it.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the
current directory. ``--json`` prints what the hub answered. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.mirror import harness_project, harness_root
from evo_agents.hub.profiles import AXES, MAX_INSTRUCTIONS_CHARS

EXIT_USAGE = 2
AVATAR_MAX_BYTES = 512 * 1024
PROJECT_HELP = "hub project (default: hub.project in the harness.yaml around the current directory)"
# The keys of an agent profile in --json (the contract, `evo-agents hub contract print`).
AGENT_KEYS = (
    "handle",
    "project",
    "name",
    "description",
    "instructions",
    "color",
    "icon_url",
    "avatar_sha256",
    "is_default",
    "permissions",
    "restricted",
    "version",
    "created_by",
    "created_at",
    "updated_at",
    "deleted_at",
)


def _project(args) -> str:
    if args.project:
        return args.project
    try:
        root = harness_root(None)
    except HubError:
        raise HubError(
            "which project? pass --project, or run this inside a harness whose harness.yaml has hub.project"
        ) from None
    return harness_project(root)


def _path(project: str, handle: str | None = None) -> str:
    path = f"/v1/projects/{quote(project, safe='')}/agents"
    return path if handle is None else f"{path}/{quote(handle.removeprefix('@'), safe='')}"


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


# Reading


def _axis_text(rule: dict) -> str:
    if rule.get("mode") != "allowlist":
        return "all"
    return ", ".join(rule.get("allow") or []) or "none"


def _show(agent: dict) -> None:
    default = ", the project's default agent" if agent["is_default"] else ""
    print(f"@{agent['handle']} ({agent['name']}) of project {agent['project']}, version {agent['version']}{default}")
    if agent.get("deleted_at"):
        print(f"  deleted at {when(agent['deleted_at'])} UTC: it takes no new run")
    print(f"  colour {agent['color']}, avatar {'yes' if agent.get('avatar_sha256') else 'none (its initial)'}")
    if agent["description"]:
        print(f"  description: {agent['description']}")
    instructions = agent["instructions"]
    print(f"  instructions: {f'{len(instructions)} characters' if instructions else 'none'}")
    for axis in AXES:
        label = f"{axis}:".ljust(12) if len(axis) < 11 else f"{axis}: "
        print(f"  {label}{_axis_text(agent['permissions'].get(axis) or {})}")
    print(f"  created by {agent['created_by']} at {when(agent['created_at'])} UTC, changed {when(agent['updated_at'])}")


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    found = hub.call("GET", _path(project) + ("?deleted=true" if args.deleted else ""))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no agent you can see.")
        return 0
    rows = [
        (
            f"@{a['handle']}",
            a["name"],
            a["version"],
            "yes" if a["is_default"] else "",
            ", ".join(a["restricted"]) or "-",
            when(a["deleted_at"]) if a.get("deleted_at") else "",
        )
        for a in found
    ]
    table(("HANDLE", "NAME", "VERSION", "DEFAULT", "LIMITS", "DELETED (UTC)"), rows)
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    agent = hub.call("GET", _path(_project(args), args.handle))
    if args.json:
        print_json(agent)
    else:
        _show(agent)
    return 0


# Changing


def _instructions(args) -> str | None:
    if args.instructions_file is None:
        return args.instructions
    text = sys.stdin.read() if args.instructions_file == "-" else Path(args.instructions_file).read_text("utf-8")
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        raise ValueError(f"the instructions are {len(text)} characters, over the {MAX_INSTRUCTIONS_CHARS} they take")
    return text


def _avatar(path: str) -> dict:
    data = Path(path).read_bytes()
    if len(data) > AVATAR_MAX_BYTES:
        raise ValueError(f"{path} is {len(data)} bytes, over the {AVATAR_MAX_BYTES} an avatar may have")
    return {"data": base64.b64encode(data).decode()}


def permissions_from(args, current: dict | None = None) -> dict | None:
    """The permissions to send: ``current`` (none when None) with the axes ``--allow``, ``--none`` and ``--all`` name
    changed; None when none is named and there is nothing to send. An axis left out allows all, so a hub that does not
    know one of AXES is never sent it unless the command names it."""
    named: dict[str, list[str]] = {}
    for entry in args.allow or ():
        axis, sep, item = entry.partition(":")
        if not sep or axis not in AXES or not item:
            raise ValueError(f"--allow takes AXIS:ITEM, AXIS one of {', '.join(AXES)}, not {entry!r}")
        named.setdefault(axis, []).append(item)
    for axis in args.none or ():
        named.setdefault(axis, [])
    reset = set(getattr(args, "all", None) or ())
    clash = reset & set(named)
    if clash:
        raise ValueError(f"{', '.join(sorted(clash))}: --all with --allow or --none for the same axis")
    if not named and not reset:
        return None
    rules = {axis: dict(rule) for axis, rule in (current or {}).items()}
    for axis in AXES:
        if axis in named:
            rules[axis] = {"mode": "allowlist", "allow": named[axis]}
        elif axis in reset:
            rules[axis] = {"mode": "all"}
    return rules


def _send_avatar(hub, project: str, handle: str, args) -> dict | None:
    if getattr(args, "no_avatar", False):
        return hub.call("DELETE", _path(project, handle) + "/avatar")
    if args.avatar:
        return hub.call("PUT", _path(project, handle) + "/avatar", _avatar(args.avatar))
    return None


def _done(args, agent: dict, verb: str) -> int:
    if args.json:
        print_json(agent)
    else:
        print(f"{verb} @{agent['handle']} of project {agent['project']}, version {agent['version']}.")
    return 0


@client_command
def cmd_create(args) -> int:
    try:
        body = {"handle": args.handle.removeprefix("@"), "name": args.name}
        fields = {"description": args.description, "instructions": _instructions(args), "color": args.color}
        body.update({key: value for key, value in fields.items() if value is not None})
        permissions = permissions_from(args)
        avatar = _avatar(args.avatar) if args.avatar else None
    except (ValueError, OSError) as exc:
        return _usage(str(exc))
    if permissions is not None:
        body["permissions"] = permissions
    hub, _ = signed_in()
    project = _project(args)
    agent = hub.call("POST", _path(project), body)
    if avatar is not None:
        agent = hub.call("PUT", _path(project, agent["handle"]) + "/avatar", avatar)
    return _done(args, agent, "Created")


@client_command
def cmd_edit(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    current = hub.call("GET", _path(project, args.handle))
    try:
        fields = {
            "name": args.name,
            "description": args.description,
            "instructions": _instructions(args),
            "color": args.color,
            "permissions": permissions_from(args, current["permissions"]),
        }
        if args.avatar:
            _avatar(args.avatar)  # refused before anything changes
    except (ValueError, OSError) as exc:
        return _usage(str(exc))
    body = {key: value for key, value in fields.items() if value is not None}
    agent = current
    if body:
        body["if_version"] = args.if_version or current["version"]
        agent = hub.call("PATCH", _path(project, args.handle), body)
    agent = _send_avatar(hub, project, current["handle"], args) or agent
    if not body and agent is current:
        return _usage("give something to change: a field, an axis (--allow, --none, --all) or the avatar")
    return _done(args, agent, "Changed")


@client_command
def cmd_delete(args) -> int:
    hub, _ = signed_in()
    agent = hub.call("DELETE", _path(_project(args), args.handle))
    return _done(args, agent, "Deleted")


# Registering


def _fields(parser, *, creating: bool) -> None:
    parser.add_argument("--name", required=creating, help="the name shown for it, 1 to 80 characters")
    parser.add_argument("--description", help="what it is for, at most 1000 characters")
    text = parser.add_mutually_exclusive_group()
    text.add_argument("--instructions", help="added to the system prompt of its runs, at most 8000 characters")
    text.add_argument("--instructions-file", metavar="PATH", help="read the instructions from a file, - for stdin")
    parser.add_argument("--color", metavar="#RRGGBB", help="its colour, lowercase hex (default: one picked by handle)")
    parser.add_argument(
        "--allow",
        action="append",
        metavar="AXIS:ITEM",
        help=f"make AXIS an allowlist holding ITEM; repeat it (axes: {', '.join(AXES)})",
    )
    parser.add_argument("--none", action="append", choices=AXES, metavar="AXIS", help="make AXIS allow nothing")


def _handle(parser) -> None:
    parser.add_argument("handle", metavar="HANDLE", help="the agent's handle, with or without @")


def register_agents(hsub) -> None:
    agent = hsub.add_parser("agent", help="the agent profiles of a project: who its agents are and what they may use")
    asub = agent.add_subparsers(dest="agent_command", required=True)

    listed = asub.add_parser("list", help="the project's agents, the default first, with the axes each one limits")
    listed.add_argument("--deleted", action="store_true", help="include the deleted ones")
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_array(*AGENT_KEYS, schema="Agent"))
    listed.set_defaults(func=cmd_list)

    show = asub.add_parser("show", help="one agent whole: each axis with what it allows")
    _handle(show)
    show.add_argument("--project", help=PROJECT_HELP)
    json_option(show, returns_object(*AGENT_KEYS, schema="Agent"))
    show.set_defaults(func=cmd_show)

    create = asub.add_parser("create", help="create an agent of the project (the admin role on it)")
    _handle(create)
    _fields(create, creating=True)
    create.add_argument("--avatar", metavar="PATH", help="a PNG, JPEG or WebP image of at most 512 KiB")
    create.add_argument("--project", help=PROJECT_HELP)
    json_option(create, returns_object(*AGENT_KEYS, schema="Agent"))
    create.set_defaults(func=cmd_create)

    edit = asub.add_parser("edit", help="change an agent: the fields and axes named, as its next version")
    _handle(edit)
    _fields(edit, creating=False)
    edit.add_argument("--all", action="append", choices=AXES, metavar="AXIS", help="make AXIS allow everything")
    avatar = edit.add_mutually_exclusive_group()
    avatar.add_argument("--avatar", metavar="PATH", help="a PNG, JPEG or WebP image of at most 512 KiB")
    avatar.add_argument("--no-avatar", action="store_true", help="drop the avatar: its initial on its colour")
    edit.add_argument("--if-version", type=int, metavar="N", help="refuse unless it is at version N (default: as read)")
    edit.add_argument("--project", help=PROJECT_HELP)
    json_option(edit, returns_object(*AGENT_KEYS, schema="Agent"))
    edit.set_defaults(func=cmd_edit)

    delete = asub.add_parser("delete", help="delete an agent: it takes no new run, and reads as before")
    _handle(delete)
    delete.add_argument("--project", help=PROJECT_HELP)
    json_option(delete, returns_object(*AGENT_KEYS, schema="Agent"))
    delete.set_defaults(func=cmd_delete)
