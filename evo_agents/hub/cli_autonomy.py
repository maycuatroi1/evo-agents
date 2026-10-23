"""``evo-agents hub autonomy show|set|ceiling set``: the autonomy levels A0 to A5 of people and agents (plan
autonomy-levels, criteria a2 and a3), over GET /v1/autonomy, PUT /v1/autonomy/prefs and PUT /v1/org/ceilings.

``autonomy show`` prints the effective level of a person (the caller without ``--login``), alone or running an agent
profile (``--agent``) on a repo (``--repo``) of a project (``--project``): each class K1 to K12 with its mode and the
source that sets it, and the environments and workers allowed. ``--json`` prints the hub's answer: principal, project,
agent, level, classes (each {mode, source, detail}), environments, workers.

``autonomy set`` writes the caller's own preference: a level, class overrides (``--class K6=deny``, merged onto those
before), or ``--reset`` to drop them. A preference above the ceilings is stored; the ceilings still decide.
``autonomy ceiling set`` writes the next ceiling revision of a unit, of a person's primary seat or of a seat, with its
class overrides and the environments and workers it allows; each revision is whole, so what it leaves out it drops.
A refusal (403, 404, 409, 422) prints the hub's reason and exits 1.

Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from urllib.parse import urlencode

from evo_agents.hub import autonomy_levels
from evo_agents.hub.autonomy_levels import CLASSES, LEVELS
from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.contract import json_option, returns_object

EXIT_USAGE = 2
# The keys of the hub's answers these commands print with --json (``evo_agents.hub.server.autonomy_models`` and
# ``.org_models``).
SHOW_KEYS = ("principal", "project", "agent", "level", "classes", "environments", "workers")
PREF_KEYS = ("login", "revision", "level", "previous", "classes")
CEILING_KEYS = (
    "scope",
    "target",
    "revision",
    "level",
    "previous",
    "note",
    "classes",
    "environments",
    "workers",
)
CLASS_HELP = (
    "a class and its mode, K1 to K12 = deny, ask, notify or auto (e.g. K6=deny, K5=notify:ask_on_risk_paths); repeat it"
)


class Usage(Exception):
    """A command line the hub is not asked about; exit 2 with this sentence."""


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def overrides(given: list[str] | None) -> dict[str, str]:
    """``Kn=mode`` flags as a map in class order; Usage naming the first wrong one or a class given twice."""
    found: dict[str, str] = {}
    for text in given or ():
        try:
            klass, mode = autonomy_levels.parse_override(text)
        except ValueError as exc:
            raise Usage(f"--class {exc}") from None
        if klass in found:
            raise Usage(f"--class names {klass} twice")
        found[klass] = mode
    return {klass: found[klass] for klass in CLASSES if klass in found}


def _places(given: list[str] | None) -> list[str]:
    return sorted(set(given or ()))


def show_lines(view: dict) -> list[str]:
    """The effective autonomy as the terminal prints it."""
    who = view["principal"] + (f" running {view['agent']}" if view["agent"] else "")
    lines = [f"{who}{' in ' + view['project'] if view['project'] else ''}: level {view['level']}", ""]
    for klass in CLASSES:
        held = view["classes"][klass]
        lines.append(f"  {klass:<4} {held['mode']:<7} {held['source']:<24} {held['detail']}")
    for name in ("environments", "workers"):
        listed = view[name]
        lines.append(f"{name.capitalize()}: {'any' if listed is None else ', '.join(listed) or 'none'}")
    return lines


@client_command
def cmd_show(args) -> int:
    if (args.agent or args.repo) and not args.project:
        return _usage("--agent and --repo name a profile or a repo within a project: add --project")
    named = {"login": args.login, "project": args.project, "agent": args.agent, "repo": args.repo}
    query = urlencode({key: value for key, value in named.items() if value is not None})
    hub, _ = signed_in()
    view = hub.call("GET", "/v1/autonomy" + (f"?{query}" if query else ""))
    if args.json:
        print_json(view)
    else:
        print("\n".join(show_lines(view)))
    return 0


@client_command
def cmd_set(args) -> int:
    try:
        classes = overrides(args.klass)
    except Usage as exc:
        return _usage(str(exc))
    if args.level is None and not classes and not args.reset:
        return _usage("nothing to set: pass --level, --class or --reset")
    body: dict = {"classes": classes, "reset": args.reset}
    if args.level is not None:
        body["level"] = args.level
    hub, _ = signed_in()
    written = hub.call("PUT", "/v1/autonomy/prefs", body)
    if args.json:
        print_json(written)
        return 0
    shown = "".join(f" {klass}={mode}" for klass, mode in written["classes"].items())
    print(
        f"Your preference is {written['level']}{shown} (was {written['previous'] or 'none'}), "
        f"revision {written['revision']}."
    )
    print("Effective now: evo-agents hub autonomy show")
    return 0


@client_command
def cmd_ceiling_set(args) -> int:
    try:
        classes = overrides(args.klass)
    except Usage as exc:
        return _usage(str(exc))
    body: dict = {"level": args.level, "note": args.note, "classes": classes}
    body |= {"environments": _places(args.environment), "workers": _places(args.worker)}
    if args.unit is not None:
        body["unit"] = args.unit
    elif args.login is not None:
        body["login"] = args.login
    else:
        body["seat"] = args.seat
    hub, _ = signed_in()
    written = hub.call("PUT", "/v1/org/ceilings", body)
    if args.json:
        print_json(written)
        return 0
    before = written["previous"] or "none"
    limits = "".join(f" {klass}={mode}" for klass, mode in written["classes"].items())
    for name in ("environments", "workers"):
        limits += f"; {name} {', '.join(written[name])}" if written[name] else ""
    print(
        f"Ceiling of {written['target']} is {written['level']}{limits} (was {before}), revision {written['revision']}."
    )
    return 0


# Registering


def _register_ceiling(asub) -> None:
    ceiling = asub.add_parser("ceiling", help="the ceilings the org chart sets on autonomy levels")
    csub = ceiling.add_subparsers(dest="autonomy_ceiling_command", required=True)

    written = csub.add_parser("set", help="the next ceiling revision of a unit, a person's primary seat or a seat")
    scope = written.add_mutually_exclusive_group(required=True)
    scope.add_argument("--unit", metavar="SLUG", help="the ceiling of a unit, which every unit below it keeps to")
    scope.add_argument("--login", help="the ceiling of a person's primary seat")
    scope.add_argument("--seat", type=int, metavar="ID", help="the ceiling of one seat, such as an agent's")
    written.add_argument("--level", required=True, choices=LEVELS, help="the highest level allowed")
    written.add_argument("--note", required=True, help="why, for the history (at most 1000 characters)")
    written.add_argument("--class", dest="klass", action="append", metavar="Kn=MODE", help=CLASS_HELP)
    written.add_argument(
        "--environment",
        action="append",
        metavar="NAME",
        help="an environment it allows (host, <plugin>/<name>); repeat it",
    )
    written.add_argument("--worker", action="append", metavar="NAME", help="a worker it allows; repeat it")
    json_option(written, returns_object(*CEILING_KEYS, schema="CeilingWritten"))
    written.set_defaults(func=cmd_ceiling_set)


def register_autonomy(hsub) -> None:
    group = hsub.add_parser("autonomy", help="the autonomy levels A0 to A5 of people and agents")
    asub = group.add_subparsers(dest="autonomy_command", required=True)

    show = asub.add_parser("show", help="the effective level and the mode of each class, with the source of each")
    show.add_argument("--login", help="the person (default: you); a lead sees those below them, an admin everyone")
    show.add_argument("--agent", metavar="HANDLE", help="running this agent profile of --project")
    show.add_argument("--project", help="the project of --agent and --repo")
    show.add_argument("--repo", help="acting on this repo of --project, under its policy")
    json_option(show, returns_object(*SHOW_KEYS, schema="AutonomyView"))
    show.set_defaults(func=cmd_show)

    pref = asub.add_parser("set", help="your own preference: a level, class overrides, or a reset")
    pref.add_argument("--level", choices=LEVELS, help="the level you prefer (the ceilings above you still decide)")
    pref.add_argument("--class", dest="klass", action="append", metavar="Kn=MODE", help=CLASS_HELP)
    pref.add_argument("--reset", action="store_true", help="drop your class overrides; A5 unless --level")
    json_option(pref, returns_object(*PREF_KEYS, schema="PrefWritten"))
    pref.set_defaults(func=cmd_set)

    _register_ceiling(asub)
