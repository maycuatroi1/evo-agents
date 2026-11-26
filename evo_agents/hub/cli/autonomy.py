"""``evo-agents hub autonomy show|set|ceiling set|run-reach``: the autonomy levels A0 to A5 of people and agents (plan
autonomy-levels, criteria a2 and a3), over GET /v1/autonomy, PUT /v1/autonomy/prefs and PUT /v1/org/ceilings, and what
the caller's runs that run as them reach, over GET and PUT /v1/autonomy/run-reach.

``autonomy show`` prints the effective level of a person (the caller without ``--login``), alone or running an agent
profile (``--agent``) on a repo (``--repo``) of a project (``--project``): each class K1 to K12 with its mode and the
source that sets it, and the environments and workers allowed. ``--json`` prints the hub's answer: principal, project,
agent, level, classes (each {mode, source, detail}), environments, workers.

``autonomy set`` writes the caller's own preference: a level, class overrides (``--class K6=deny``, merged onto those
before), or ``--reset`` to drop them. A preference above the ceilings is stored; the ceilings still decide.
``autonomy ceiling set`` writes the next ceiling revision of a unit, of a person's primary seat or of a seat, with its
class overrides and the environments and workers it allows; each revision is whole, so what it leaves out it drops.

``autonomy run-reach`` prints the groups a run's token (``evr_...``) of the caller reaches besides what every run's
token reaches, of agents (a project's agent profiles and agent types), workers (workers, worker pools, join tokens) and
curator (the Curator's writes); ``--allow agents,workers`` replaces them with those named, ``--none`` clears them.
``--json`` prints login and groups, and previous too after ``--allow`` or ``--none``. No run's token reads or sets it.
A refusal (403, 404, 409, 422) prints the hub's reason and exits 1.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Autonomy` is
the gateway to their routes, which belong to the hub and not to a project. Standard library only, like the rest of
the client.
"""

from __future__ import annotations

import argparse
from urllib.parse import urlencode

from evo_agents.clikit import Arg, Group, Result, Text, UsageError
from evo_agents.hub import autonomy_levels
from evo_agents.hub.autonomy_levels import CLASSES, LEVELS
from evo_agents.hub.cli.base import HubCommand, HubContext, call_object
from evo_agents.hub.client import Hub
from evo_agents.hub.contract import returns_object
from evo_agents.hub.credentials import RUN_REACH_GROUPS

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
REACH_KEYS = ("login", "groups")
REACH_WRITTEN_KEYS = ("login", "groups", "previous")
CLASS_HELP = (
    "a class and its mode, K1 to K12 = deny, ask, notify or auto (e.g. K6=deny, K5=notify:ask_on_risk_paths); repeat it"
)


def overrides(given: list[str] | None) -> dict[str, str]:
    """``Kn=mode`` flags as a map in class order; UsageError naming the first wrong one or a class given twice."""
    found: dict[str, str] = {}
    for text in given or ():
        try:
            klass, mode = autonomy_levels.parse_override(text)
        except ValueError as exc:
            raise UsageError(f"--class {exc}") from None
        if klass in found:
            raise UsageError(f"--class names {klass} twice")
        found[klass] = mode
    return {klass: found[klass] for klass in CLASSES if klass in found}


def reach_groups(text: str) -> list[str]:
    """``--allow agents,workers`` as the groups it names, once each in the hub's order; UsageError naming a wrong one."""
    names = [name.strip() for name in text.split(",") if name.strip()]
    choices = ", ".join(RUN_REACH_GROUPS)
    if not names:
        raise UsageError(f"--allow names one or more of {choices}; --none clears them")
    for name in names:
        if name not in RUN_REACH_GROUPS:
            raise UsageError(f"--allow: no group {name!r}; the groups are {choices}")
    return [group for group in RUN_REACH_GROUPS if group in names]


def _groups(groups: list[str]) -> str:
    return ", ".join(groups) or "none"


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


class Autonomy:
    """The autonomy routes of the hub: GET /v1/autonomy, PUT /v1/autonomy/prefs, GET and PUT
    /v1/autonomy/run-reach, and PUT /v1/org/ceilings (Gateway)."""

    path = "/v1/autonomy"

    def __init__(self, hub: Hub):
        self.hub = hub

    def view(self, **named: str | None) -> dict:
        query = urlencode({key: value for key, value in named.items() if value is not None})
        return call_object(self.hub, "GET", self.path + (f"?{query}" if query else ""))

    def prefer(self, body: dict) -> dict:
        return call_object(self.hub, "PUT", f"{self.path}/prefs", body)

    def set_ceiling(self, body: dict) -> dict:
        return call_object(self.hub, "PUT", "/v1/org/ceilings", body)

    def run_reach(self) -> dict:
        return call_object(self.hub, "GET", f"{self.path}/run-reach")

    def set_run_reach(self, groups: list[str]) -> dict:
        return call_object(self.hub, "PUT", f"{self.path}/run-reach", {"groups": groups})


CLASS = Arg("--class", dest="klass", action="append", metavar="Kn=MODE", help=CLASS_HELP)


class AutonomyShow(HubCommand):
    name = "show"
    help = "the effective level and the mode of each class, with the source of each"
    arguments = (
        Arg("--login", help="the person (default: you); a lead sees those below them, an admin everyone"),
        Arg("--agent", metavar="HANDLE", help="running this agent profile of --project"),
        Arg("--project", help="the project of --agent and --repo"),
        Arg("--repo", help="acting on this repo of --project, under its policy"),
    )
    output = returns_object(*SHOW_KEYS, schema="AutonomyView")

    def check(self, args: argparse.Namespace) -> None:
        if (args.agent or args.repo) and not args.project:
            raise UsageError("--agent and --repo name a profile or a repo within a project: add --project")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        view = Autonomy(ctx.hub).view(login=args.login, project=args.project, agent=args.agent, repo=args.repo)
        return Result(view, Text("\n".join(show_lines(view))))


def pref_body(args) -> dict:
    """The body of PUT /v1/autonomy/prefs for the command line ``args``; UsageError when it sets nothing."""
    classes = overrides(args.klass)
    if args.level is None and not classes and not args.reset:
        raise UsageError("nothing to set: pass --level, --class or --reset")
    body: dict = {"classes": classes, "reset": args.reset}
    if args.level is not None:
        body["level"] = args.level
    return body


def _modes(classes: dict[str, str]) -> str:
    return "".join(f" {klass}={mode}" for klass, mode in classes.items())


class AutonomySet(HubCommand):
    name = "set"
    help = "your own preference: a level, class overrides, or a reset"
    arguments = (
        Arg("--level", choices=LEVELS, help="the level you prefer (the ceilings above you still decide)"),
        CLASS,
        Arg("--reset", action="store_true", help="drop your class overrides; A5 unless --level"),
    )
    output = returns_object(*PREF_KEYS, schema="PrefWritten")

    def check(self, args: argparse.Namespace) -> None:
        pref_body(args)

    def run(self, ctx: HubContext) -> Result:
        written = Autonomy(ctx.hub).prefer(pref_body(ctx.args))
        said = (
            f"Your preference is {written['level']}{_modes(written['classes'])} "
            f"(was {written['previous'] or 'none'}), revision {written['revision']}."
        )
        return Result(written, Text(said, "Effective now: evo-agents hub autonomy show"))


def ceiling_body(args) -> dict:
    """The body of PUT /v1/org/ceilings for the command line ``args``; UsageError naming a wrong --class."""
    body: dict = {"level": args.level, "note": args.note, "classes": overrides(args.klass)}
    body |= {"environments": _places(args.environment), "workers": _places(args.worker)}
    if args.unit is not None:
        body["unit"] = args.unit
    elif args.login is not None:
        body["login"] = args.login
    else:
        body["seat"] = args.seat
    return body


def ceiling_line(written: dict) -> str:
    """What the terminal prints of a ceiling written."""
    limits = _modes(written["classes"])
    for name in ("environments", "workers"):
        limits += f"; {name} {', '.join(written[name])}" if written[name] else ""
    before = written["previous"] or "none"
    return (
        f"Ceiling of {written['target']} is {written['level']}{limits} (was {before}), revision {written['revision']}."
    )


CEILING_SCOPES = (
    Arg("--unit", metavar="SLUG", help="the ceiling of a unit, which every unit below it keeps to"),
    Arg("--login", help="the ceiling of a person's primary seat"),
    Arg("--seat", type=int, metavar="ID", help="the ceiling of one seat, such as an agent's"),
)


class CeilingSet(HubCommand):
    name = "set"
    help = "the next ceiling revision of a unit, a person's primary seat or a seat"
    arguments = (
        Arg("--level", required=True, choices=LEVELS, help="the highest level allowed"),
        Arg("--note", required=True, help="why, for the history (at most 1000 characters)"),
        CLASS,
        Arg(
            "--environment",
            action="append",
            metavar="NAME",
            help="an environment it allows (host, <plugin>/<name>); repeat it",
        ),
        Arg("--worker", action="append", metavar="NAME", help="a worker it allows; repeat it"),
    )
    output = returns_object(*CEILING_KEYS, schema="CeilingWritten")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        scope = parser.add_mutually_exclusive_group(required=True)  # first, before --level
        for arg in CEILING_SCOPES:
            scope.add_argument(*arg.flags, **arg.options)
        super().configure(parser)

    def check(self, args: argparse.Namespace) -> None:
        overrides(args.klass)

    def run(self, ctx: HubContext) -> Result:
        written = Autonomy(ctx.hub).set_ceiling(ceiling_body(ctx.args))
        return Result(written, Text(ceiling_line(written)))


def chosen_groups(args) -> list[str] | None:
    """The groups ``--allow`` or ``--none`` sets; None when the command only reads them."""
    return [] if args.none else None if args.allow is None else reach_groups(args.allow)


def reached(held: dict) -> str:
    return f"Your runs that run as you also reach: {_groups(held['groups'])}"


REACH_WRITTEN = returns_object(*REACH_WRITTEN_KEYS, schema="RunReachWritten")


class RunReach(HubCommand):
    name = "run-reach"
    help = "what your runs that run as you reach besides what every run's token reaches: " + ", ".join(RUN_REACH_GROUPS)
    output = returns_object(
        *REACH_KEYS, schema="RunReach", variants=(("--allow", REACH_WRITTEN), ("--none", REACH_WRITTEN))
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        chosen = parser.add_mutually_exclusive_group()
        chosen.add_argument(
            "--allow",
            metavar="GROUP[,GROUP...]",
            help=f"replace it with these groups, comma-separated, of {', '.join(RUN_REACH_GROUPS)}",
        )
        chosen.add_argument("--none", action="store_true", help="clear it: your runs reach none of the groups")

    def check(self, args: argparse.Namespace) -> None:
        chosen_groups(args)

    def run(self, ctx: HubContext) -> Result:
        groups = chosen_groups(ctx.args)
        autonomy = Autonomy(ctx.hub)
        held = autonomy.run_reach() if groups is None else autonomy.set_run_reach(groups)
        if groups is not None:
            return Result(held, Text(f"{reached(held)} (was {_groups(held['previous'])})."))
        said = f"The groups: {', '.join(RUN_REACH_GROUPS)}. Change it with --allow GROUP[,GROUP...] or --none."
        return Result(held, Text(f"{reached(held)}.", said))


CEILING = Group(
    "ceiling",
    "the ceilings the org chart sets on autonomy levels",
    [CeilingSet()],
    dest="autonomy_ceiling_command",
)
AUTONOMY = Group(
    "autonomy",
    "the autonomy levels A0 to A5 of people and agents",
    [AutonomyShow(), AutonomySet(), CEILING, RunReach()],
)
