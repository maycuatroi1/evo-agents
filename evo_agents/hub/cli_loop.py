"""``evo-agents hub loop status|resume``: the Loop of a project (plan loop-engine), over GET /v1/projects/{p}/loop and
POST .../loop/resume.

``status`` prints whether the Loop is on, its breaker, its cursor and last tick, and its work items by state; every
member of the project reads it. ``resume`` resets a breaker that tripped on too many work items in an hour, which takes
the admin role on the project: the Loop reads its events again and hands out runs from its next tick. The Loop is
turned on and off with the policy's ``loop.enabled`` (``evo-agents hub policy``).

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


def register_loop(hsub) -> None:
    loop = hsub.add_parser("loop", help="the Loop of a project: its state and the reset of its breaker")
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
