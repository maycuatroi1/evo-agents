"""``evo-agents hub loop status|resume|baseline``: the Loop of a project (plan loop-engine), over GET /v1/projects/{p}/loop and
POST .../loop/resume; ``baseline`` is the baseline of a goal that aims at an SRS document (plan srs-backlog-loop).

``status`` prints whether the Loop is on, its breaker, its cursor and last tick, and its work items by state; every
member of the project reads it. ``resume`` resets a breaker that tripped on too many work items in an hour, which takes
the admin role on the project: the Loop reads its events again and hands out runs from its next tick. The Loop is
turned on and off with the policy's ``loop.enabled`` (``evo-agents hub policy``).

``baseline --goal G-n`` previews what the hub proposes of each approved use case of the goal's document: delivered,
undelivered or unknown, with the evidence the knowledge graph gives. ``--set CODE=STATE`` and ``--heading HEADING=STATE``
(both repeatable, STATE delivered or undelivered) fix use cases by code and by heading (every use case under it),
``--unknown STATE`` sets all that are still unknown, and ``--confirm`` confirms the baseline once; the last three are
the admin role on the project. Until it is confirmed the Loop makes no build for the goal, and a use case left unknown
never enters its backlog.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the
current directory. ``--json`` prints what the hub answered. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in, when
from evo_agents.hub.cli_policy import PROJECT_HELP
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.mirror import harness_project, harness_root
from evo_agents.hub.srs_baseline import FIX_STATES

# The keys of the hub's answer these commands print with --json (the contract, `evo-agents hub contract print`).
LOOP_KEYS = (
    "project",
    "enabled",
    "breaker",
    "breaker_reason",
    "tripped_at",
    "resumed_by",
    "resumed_at",
    "cursor",
    "ticked_at",
    "items",
)


BASELINE_KEYS = (
    "project",
    "goal",
    "document",
    "heading",
    "confirmed",
    "confirmed_at",
    "confirmed_by",
    "graph",
    "why",
    "counts",
    "items",
)
SHOWN_EVIDENCE = 3  # lines of evidence printed under a use case


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


def _path(project: str, rest: str = "") -> str:
    return f"/v1/projects/{quote(project, safe='')}/loop{rest}"


def _lines(found: dict) -> list[str]:
    project = found["project"]
    lines = [f"The Loop of project {project} is {'on' if found['enabled'] else 'off'}."]
    if found["breaker"] == "tripped":
        lines += [
            f"Its breaker tripped at {when(found['tripped_at'])} UTC: {found['breaker_reason']}",
            f"  an admin resets it: evo-agents hub loop resume --project {project}",
        ]
    else:
        reset = f", reset by {found['resumed_by']} at {when(found['resumed_at'])} UTC" if found["resumed_by"] else ""
        lines.append(f"Its breaker is armed{reset}.")
    ticked = f"{when(found['ticked_at'])} UTC" if found["ticked_at"] else "never"
    lines.append(f"Cursor at event {found['cursor']}; last tick {ticked}.")
    counts = ", ".join(f"{state} {count}" for state, count in sorted(found["items"].items()))
    lines.append(f"Work items: {counts or 'none'}.")
    return lines


@client_command
def cmd_status(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(_project(args)))
    if args.json:
        print_json(found)
    else:
        print("\n".join(_lines(found)))
    return 0


@client_command
def cmd_resume(args) -> int:
    hub, _ = signed_in()
    found = hub.call("POST", _path(_project(args), "/resume"))
    if args.json:
        print_json(found)
    else:
        print(f"Reset the breaker of the Loop of project {found['project']}: it goes on from its next tick.")
    return 0


def _fixes(pairs: list[str] | None, what: str) -> dict[str, str]:
    """``NAME=STATE`` options as {name: state}; HubError for one that is not."""
    found = {}
    for pair in pairs or []:
        name, _, state = pair.rpartition("=")
        if not name.strip() or state not in FIX_STATES:
            raise HubError(f"{what} {pair!r}: write NAME=STATE with STATE one of {', '.join(FIX_STATES)}")
        found[name.strip()] = state
    return found


def _baseline_lines(found: dict) -> list[str]:
    scope = f"{found['document']}" + (f", under {found['heading']}" if found["heading"] else "")
    counts = ", ".join(f"{state} {count}" for state, count in sorted(found["counts"].items()))
    lines = [f"Baseline of goal {found['goal']} on SRS document {scope}: {counts or 'no approved use case'}."]
    if not found["graph"]:
        lines.append(f"The knowledge graph could not be read: {found['why']}. Every use case without a row is unknown.")
    for state in ("delivered", "undelivered", "unknown", "delivering"):
        held = [item for item in found["items"] if item["state"] == state]
        if not held:
            continue
        lines.append(f"{state} ({len(held)}):")
        for item in held:
            lines.append(f"  {item['code']}  {item['title']}  [{item['source']}] {item['reason']}".rstrip())
            for line in item["evidence"][:SHOWN_EVIDENCE]:
                lines.append(f"      {line['kind']}: {line['title'] or line['ref']}")
    if found["confirmed"]:
        lines.append(f"Confirmed by {found['confirmed_by']} at {when(found['confirmed_at'])} UTC.")
    else:
        lines.append(
            f"Not confirmed: the Loop makes no build for goal {found['goal']} yet, and unknown use cases never enter "
            f"its backlog. Fix with --set CODE=STATE, --heading HEADING=STATE or --unknown STATE; then "
            f"evo-agents hub loop baseline --goal {found['goal']} --confirm"
        )
    return lines


@client_command
def cmd_baseline(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    path = f"/v1/projects/{quote(project, safe='')}/goals/{quote(args.goal, safe='')}/baseline"
    body = {
        "codes": _fixes(args.set, "--set"),
        "headings": _fixes(args.heading, "--heading"),
        "unknown": args.unknown,
    }
    if body["codes"] or body["headings"] or body["unknown"]:
        found = hub.call("POST", path + "/fix", body)
    else:
        found = hub.call("GET", path)
    if args.confirm:
        found = hub.call("POST", path + "/confirm")
    if args.json:
        print_json(found)
    else:
        print("\n".join(_baseline_lines(found)))
    return 0


def register_loop(hsub) -> None:
    loop = hsub.add_parser(
        "loop", help="the Loop of a project: its state, the reset of its breaker and the baseline of a goal"
    )
    lsub = loop.add_subparsers(dest="loop_command", required=True)

    status = lsub.add_parser("status", help="whether the Loop is on, its breaker, cursor and work items by state")
    status.add_argument("--project", help=PROJECT_HELP)
    json_option(status, returns_object(*LOOP_KEYS, schema="ProjectLoop"))
    status.set_defaults(func=cmd_status)

    resume = lsub.add_parser(
        "resume", help="reset the breaker of the Loop that tripped (the admin role on the project)"
    )
    resume.add_argument("--project", help=PROJECT_HELP)
    json_option(resume, returns_object(*LOOP_KEYS, schema="ProjectLoop"))
    resume.set_defaults(func=cmd_resume)

    base = lsub.add_parser(
        "baseline",
        help="the baseline of a goal that aims at an SRS document: preview, fix by code or heading, confirm once",
    )
    base.add_argument("--goal", required=True, metavar="G-N", help="the goal's code (G-<number>) or alias")
    base.add_argument("--confirm", action="store_true", help="confirm the baseline, once (the admin role)")
    base.add_argument(
        "--set", action="append", metavar="CODE=STATE", help="set one use case to delivered or undelivered"
    )
    base.add_argument("--heading", action="append", metavar="HEADING=STATE", help="set every use case under a heading")
    base.add_argument("--unknown", choices=FIX_STATES, help="set every use case still unknown to this")
    base.add_argument("--project", help=PROJECT_HELP)
    json_option(base, returns_object(*BASELINE_KEYS, schema="SrsBaseline"))
    base.set_defaults(func=cmd_baseline)
