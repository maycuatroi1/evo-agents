"""``evo-agents hub stop``: the stop of autonomy S4 (plan autonomy-levels, criterion a11), over POST /v1/autonomy/stops
and POST /v1/autonomy/stops/lift.

``hub stop --login <login> | --unit <slug> | --all [--reason "..."]`` cancels every run of the principals it names
that has not ended (a held run stops at its worker's next heartbeat), takes back their leases, and holds every new run
of theirs, those the hub makes itself included, until ``hub stop ... --lift``. A person stops their own runs, a lead
those of the units they lead and below, a hub admin anyone and the whole hub. ``--json`` prints the hub's answer: id,
scope, target, reason, stopped_by, stopped_at, lifted_by, lifted_at, runs, leases, told. A refusal (403, 404, 409,
422) prints the hub's reason and exits 1.

Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.contract import json_option, returns_object

# The keys of the hub's answer (``evo_agents.hub.server.autonomy_stops.StopView``).
STOP_KEYS = (
    "id",
    "scope",
    "target",
    "reason",
    "stopped_by",
    "stopped_at",
    "lifted_by",
    "lifted_at",
    "runs",
    "leases",
    "told",
)


def stop_body(args) -> dict:
    """The body of POST /v1/autonomy/stops or .../lift for the command line ``args``."""
    if args.login is not None:
        body: dict = {"scope": "login", "login": args.login}
    elif args.unit is not None:
        body = {"scope": "unit", "unit": args.unit}
    else:
        body = {"scope": "all"}
    if args.reason and not args.lift:
        body["reason"] = args.reason
    return body


def stop_lines(view: dict) -> list[str]:
    """What the terminal prints of a stop or a lift."""
    if view["lifted_at"]:
        lines = [f"Stop #{view['id']} of {view['target']} lifted by {view['lifted_by']}: runs are queued again."]
    else:
        why = f" ({view['reason']})" if view["reason"] else ""
        lines = [f"Stop #{view['id']} of {view['target']}{why} holds: no new run of it is queued until it is lifted."]
        listed = ", ".join(f"#{run_id}" for run_id in view["runs"]) or "none"
        lines.append(f"Runs cancelled or asked to stop: {listed}; leases ended: {view['leases']}.")
    if view["told"]:
        lines.append(f"Told: {', '.join(view['told'])}.")
    return lines


@client_command
def cmd_stop(args) -> int:
    hub, _ = signed_in()
    path = "/v1/autonomy/stops/lift" if args.lift else "/v1/autonomy/stops"
    view = hub.call("POST", path, stop_body(args))
    if args.json:
        print_json(view)
    else:
        print("\n".join(stop_lines(view)))
    return 0


def register_stop(hsub) -> None:
    stop = hsub.add_parser(
        "stop", help="stop autonomy S4: cancel the runs of a person, a unit or the hub and hold new ones until --lift"
    )
    scope = stop.add_mutually_exclusive_group(required=True)
    scope.add_argument("--login", help="a person's runs (yours, or of someone in a unit you lead)")
    scope.add_argument("--unit", metavar="SLUG", help="the runs of everyone in this unit and the units below it")
    scope.add_argument("--all", action="store_true", help="every run of the hub (a hub admin)")
    stop.add_argument("--reason", help="why, for the people stopped and the audit (at most 1000 characters)")
    stop.add_argument("--lift", action="store_true", help="lift the stop that holds for this scope")
    json_option(stop, returns_object(*STOP_KEYS, schema="StopView"))
    stop.set_defaults(func=cmd_stop)
