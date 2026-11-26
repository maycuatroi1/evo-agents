"""``evo-agents hub agent list|show|create|edit|delete``: the agent profiles of a project (``evo_agents.hub.profiles``),
over GET, POST, PATCH and DELETE /v1/projects/{p}/agents. ``agent account`` is in ``cli.agent.account``.

``list`` prints the project's profiles, the default first, with the axes each one limits; ``--deleted`` adds the
deleted ones. ``show`` prints one profile whole, each axis with its allowlist. Every member of the project may read
them. ``create``, ``edit`` and ``delete`` need the admin role on the project.

Each agent has a pool of its own, ``agent-<handle>``, made with it (manage it with ``evo-agents worker pool``): every
run of the agent goes to a node of that pool, and nobody picks the machine.

A profile's permissions are eight axes (credential, tool, skill, worker, project, model, runtime, environment), each
``all`` or an allowlist; an axis a profile does not name, as a hub older than the axis answers, reads as ``all``. ``--allow AXIS:ITEM`` (repeatable) makes AXIS an allowlist of the items given for it, such as ``--allow
tool:read --allow tool:edit --allow credential:github-app``; ``--none AXIS`` makes AXIS an allowlist of nothing, and
``--all AXIS`` (``edit`` only) makes it allow everything again. ``edit`` changes only the axes named, sending the
profile's other axes as they are, with ``if_version`` set to the version it read, so a change made meanwhile is a
refusal rather than lost. ``--instructions-file PATH`` reads the instructions from a file, ``-`` for stdin; ``--avatar
PATH`` sets a PNG, JPEG or WebP image of at most 512 KiB, ``--no-avatar`` drops it. ``--pack PLUGIN/NAME[@VERSION]``
(repeatable) attaches the agent packs given, in that order, each pinned at VERSION of its plugin or, left out, at the
version installed now; on ``edit`` the packs given replace those the profile attached, and ``--no-packs`` drops them.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the
current directory. ``--json`` prints what the hub answered. Standard library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Agents` is the
gateway to the profiles of a project, :func:`permissions_from` and :func:`packs_from` turn the options into what is
sent, and :func:`agent_lines` says a profile to a person. :data:`AGENT` is the node ``evo_agents.hub.cli`` attaches:
``hub agent``, then ``hub account-pool`` beside it.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, returns_array, returns_object
from evo_agents.hub.cli.account_pool import ACCOUNT_POOL, AGENT_ACCOUNT_POOL
from evo_agents.hub.cli.agent.account import AGENT_ACCOUNT
from evo_agents.hub.cli.agent.keys import AGENT_KEYS
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.cli.session import Commands
from evo_agents.hub.profiles import AXES, MAX_INSTRUCTIONS_CHARS

AVATAR_MAX_BYTES = 512 * 1024
ONE_AGENT = returns_object(*AGENT_KEYS, schema="Agent")
NOTHING_TO_CHANGE = (
    "give something to change: a field, an axis (--allow, --none, --all), the packs (--pack, --no-packs) or the avatar"
)


class Agents(ProjectResource):
    """The agent profiles of a project: /v1/projects/{p}/agents and the routes below it (Gateway)."""

    collection = "agents"

    def list(self, deleted: bool) -> list[dict]:
        return self.call("GET", deleted="true" if deleted else None)

    def get(self, handle: str) -> dict:
        return self.call("GET", handle.removeprefix("@"))

    def create(self, body: dict) -> dict:
        return self.call("POST", body=body)

    def change(self, handle: str, body: dict) -> dict:
        return self.call("PATCH", handle.removeprefix("@"), body=body)

    def delete(self, handle: str) -> dict:
        return self.call("DELETE", handle.removeprefix("@"))

    def put_avatar(self, handle: str, avatar: dict) -> dict:
        return self.call("PUT", handle.removeprefix("@"), "avatar", body=avatar)

    def drop_avatar(self, handle: str) -> dict:
        return self.call("DELETE", handle.removeprefix("@"), "avatar")


# Reading


def axis_text(rule: dict) -> str:
    if rule.get("mode") != "allowlist":
        return "all"
    return ", ".join(rule.get("allow") or []) or "none"


def agent_lines(agent: dict) -> list[str]:
    """A profile whole, as ``agent show`` prints it."""
    default = ", the project's default agent" if agent["is_default"] else ""
    lines = [f"@{agent['handle']} ({agent['name']}) of project {agent['project']}, version {agent['version']}{default}"]
    if agent.get("deleted_at"):
        lines.append(f"  deleted at {when(agent['deleted_at'])} UTC: it takes no new run")
    lines.append(f"  colour {agent['color']}, avatar {'yes' if agent.get('avatar_sha256') else 'none (its initial)'}")
    if agent["description"]:
        lines.append(f"  description: {agent['description']}")
    instructions = agent["instructions"]
    lines.append(f"  instructions: {f'{len(instructions)} characters' if instructions else 'none'}")
    for axis in AXES:
        label = f"{axis}:".ljust(12) if len(axis) < 11 else f"{axis}: "
        lines.append(f"  {label}{axis_text(agent['permissions'].get(axis) or {})}")
    packs = [f"{pack['plugin']}/{pack['name']}@{pack['version']}" for pack in agent.get("packs") or ()]
    lines.append(f"  {'packs:'.ljust(12)}{', '.join(packs) or 'none'}")
    lines.append(f"  {'pool:'.ljust(12)}{agent.get('home_pool') or 'none'}")
    lines.append(f"  {'runs on:'.ljust(12)}{agent.get('runs_on') or 'worker'}")
    lines.append(
        f"  created by {agent['created_by']} at {when(agent['created_at'])} UTC, changed {when(agent['updated_at'])}"
    )
    return lines


HANDLE = Arg("handle", metavar="HANDLE", help="the agent's handle, with or without @")


class AgentList(HubCommand):
    name = "list"
    help = "the project's agents, the default first, with the axes each one limits"
    arguments = (Arg("--deleted", action="store_true", help="include the deleted ones"), PROJECT)
    output = returns_array(*AGENT_KEYS, schema="Agent")
    columns = ("HANDLE", "NAME", "VERSION", "DEFAULT", "LIMITS", "POOL", "DELETED (UTC)")

    def run(self, ctx: HubContext) -> Result:
        found = Agents.of(ctx).list(ctx.args.deleted)
        if not found:
            return Result(found, Text(f"Project {ctx.project} has no agent you can see."))
        return Result(found, Table(self.columns, [self.row(agent) for agent in found]))

    @staticmethod
    def row(a: dict) -> tuple:
        return (
            f"@{a['handle']}",
            a["name"],
            a["version"],
            "yes" if a["is_default"] else "",
            ", ".join(a["restricted"]) or "-",
            a["home_pool"] or "-",
            when(a["deleted_at"]) if a.get("deleted_at") else "",
        )


class AgentShow(HubCommand):
    name = "show"
    help = "one agent whole: each axis with what it allows"
    arguments = (HANDLE, PROJECT)
    output = ONE_AGENT

    def run(self, ctx: HubContext) -> Result:
        agent = Agents.of(ctx).get(ctx.args.handle)
        return Result(agent, Text(*agent_lines(agent)))


# Changing


def instructions_of(args: argparse.Namespace) -> str | None:
    if args.instructions_file is None:
        return args.instructions
    text = sys.stdin.read() if args.instructions_file == "-" else Path(args.instructions_file).read_text("utf-8")
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        raise ValueError(f"the instructions are {len(text)} characters, over the {MAX_INSTRUCTIONS_CHARS} they take")
    return text


def avatar_of(path: str) -> dict:
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


def packs_from(args) -> list[dict] | None:
    """The packs of ``--pack`` (``[]`` for ``--no-packs``), each ``{"plugin", "name"}`` with its ``version`` when
    given; None when neither was given. ValueError for one that does not read."""
    if getattr(args, "no_packs", False):
        return []
    if not args.pack:
        return None
    found = []
    for given in args.pack:
        pack, at, version = given.partition("@")
        plugin, slash, name = pack.partition("/")
        if not (plugin and slash and name) or "/" in name or (at and not version):
            raise ValueError(f"--pack {given!r} is not PLUGIN/NAME or PLUGIN/NAME@VERSION")
        found.append({"plugin": plugin, "name": name, **({"version": version} if version else {})})
    return found


def done(agent: dict, verb: str) -> Result:
    return Result(agent, Text(f"{verb} @{agent['handle']} of project {agent['project']}, version {agent['version']}."))


def add_fields(parser: argparse.ArgumentParser, *, creating: bool) -> None:
    """The fields of a profile ``create`` and ``edit`` take, in the order the parser has always had them."""
    parser.add_argument("--name", required=creating, help="the name shown for it, 1 to 80 characters")
    parser.add_argument("--description", help="what it is for, at most 1000 characters")
    text = parser.add_mutually_exclusive_group()
    text.add_argument("--instructions", help="added to the system prompt of its runs, at most 8000 characters")
    text.add_argument("--instructions-file", metavar="PATH", help="read the instructions from a file, - for stdin")
    parser.add_argument("--color", metavar="#RRGGBB", help="its colour, lowercase hex (default: one picked by handle)")
    parser.add_argument(
        "--runs-on",
        choices=("worker", "hub"),
        help="where its runs run: on a worker of its pool or on the hub" + (" (default: worker)" if creating else ""),
    )
    parser.add_argument(
        "--allow",
        action="append",
        metavar="AXIS:ITEM",
        help=f"make AXIS an allowlist holding ITEM; repeat it (axes: {', '.join(AXES)})",
    )
    parser.add_argument("--none", action="append", choices=AXES, metavar="AXIS", help="make AXIS allow nothing")
    parser.add_argument(
        "--pack",
        action="append",
        metavar="PLUGIN/NAME[@VERSION]",
        help="attach an agent pack of a plugin the project enabled, at VERSION or the one installed now; repeat it"
        + ("" if creating else " (they replace the packs it attached)"),
    )


AVATAR_HELP = "a PNG, JPEG or WebP image of at most 512 KiB"


class AgentCreate(HubCommand):
    name = "create"
    help = "create an agent of the project (the admin role on it)"
    output = ONE_AGENT

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        HANDLE.add_to(parser)
        add_fields(parser, creating=True)
        parser.add_argument("--avatar", metavar="PATH", help=AVATAR_HELP)
        PROJECT.add_to(parser)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        try:  # the options and files are read before the hub is asked
            body = {"handle": args.handle.removeprefix("@"), "name": args.name}
            fields = {"description": args.description, "instructions": instructions_of(args), "color": args.color}
            fields |= {"runs_on": args.runs_on, "permissions": permissions_from(args), "packs": packs_from(args)}
            avatar = avatar_of(args.avatar) if args.avatar else None
        except (ValueError, OSError) as exc:
            raise UsageError(str(exc)) from None
        body.update({key: value for key, value in fields.items() if value is not None})
        agents = Agents.of(ctx)
        agent = agents.create(body)
        if avatar is not None:
            agent = agents.put_avatar(agent["handle"], avatar)
        return done(agent, "Created")


class AgentEdit(HubCommand):
    name = "edit"
    help = "change an agent: the fields and axes named, as its next version"
    output = ONE_AGENT

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        HANDLE.add_to(parser)
        add_fields(parser, creating=False)
        parser.add_argument("--all", action="append", choices=AXES, metavar="AXIS", help="make AXIS allow everything")
        parser.add_argument("--no-packs", action="store_true", help="attach no agent pack any more")
        avatar = parser.add_mutually_exclusive_group()
        avatar.add_argument("--avatar", metavar="PATH", help=AVATAR_HELP)
        avatar.add_argument("--no-avatar", action="store_true", help="drop the avatar: its initial on its colour")
        parser.add_argument(
            "--if-version", type=int, metavar="N", help="refuse unless it is at version N (default: as read)"
        )
        PROJECT.add_to(parser)

    @staticmethod
    def fields(args: argparse.Namespace, current: dict) -> dict:
        """What to change of ``current``; UsageError for an option or a file that does not read."""
        try:
            fields = {
                "name": args.name,
                "description": args.description,
                "instructions": instructions_of(args),
                "color": args.color,
                "runs_on": args.runs_on,
                "permissions": permissions_from(args, current["permissions"]),
                "packs": packs_from(args),
            }
            if args.avatar:
                avatar_of(args.avatar)  # refused before anything changes
        except (ValueError, OSError) as exc:
            raise UsageError(str(exc)) from None
        return {key: value for key, value in fields.items() if value is not None}

    def run(self, ctx: HubContext) -> Result:
        args, agents = ctx.args, Agents.of(ctx)
        current = agents.get(args.handle)
        body = self.fields(args, current)
        agent = current
        if body:
            body["if_version"] = args.if_version or current["version"]
            agent = agents.change(args.handle, body)
        if args.no_avatar:
            agent = agents.drop_avatar(current["handle"]) or agent
        elif args.avatar:
            agent = agents.put_avatar(current["handle"], avatar_of(args.avatar)) or agent
        if not body and agent is current:
            raise UsageError(NOTHING_TO_CHANGE)
        return done(agent, "Changed")


class AgentDelete(HubCommand):
    name = "delete"
    help = "delete an agent: it takes no new run, and reads as before"
    arguments = (HANDLE, PROJECT)
    output = ONE_AGENT

    def run(self, ctx: HubContext) -> Result:
        return done(Agents.of(ctx).delete(ctx.args.handle), "Deleted")


AGENT_GROUP = Group(  # hub agent
    "agent",
    "the agent profiles of a project: who its agents are and what they may use",
    [AgentList(), AgentShow(), AgentCreate(), AgentEdit(), AgentDelete(), AGENT_ACCOUNT, AGENT_ACCOUNT_POOL],
)
# The node ``evo_agents.hub.cli`` attaches: hub agent, then hub account-pool beside it, as they have always come.
AGENT = Commands((AGENT_GROUP, ACCOUNT_POOL))
