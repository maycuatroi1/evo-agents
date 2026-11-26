"""``evo-agents hub run-cause restore RUN --project P``: take back the cause a core plugin gave a failed run
(the writer role), over POST /v1/projects/{project}/runs/{run}/cause/restore. The run has the cause the hub had guessed
again, and its log says who restored it. ``--json`` prints the hub's CauseRestored: run_id, failure_cause, was.
A refusal (403, 404, 409) prints the hub's reason and exits 1. Standard library only.
"""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.contract import json_option, returns_object

CAUSE_RESTORED = ("run_id", "failure_cause", "was")


@client_command
def cmd_run_cause(args) -> int:
    hub, _ = signed_in()
    found = hub.call("POST", f"/v1/projects/{quote(args.project, safe='')}/runs/{args.run}/cause/restore", {})
    if args.json:
        print_json(found)
        return 0
    print(f"run {found['run_id']} has the cause {found['failure_cause']} again; a plugin had set {found['was']}")
    return 0


def register_run_cause(hsub) -> None:
    cause = hsub.add_parser("run-cause", help="take back the cause a core plugin gave a failed run (the writer role)")
    cause.add_argument("action", choices=["restore"], help="restore the cause the hub had guessed")
    cause.add_argument("run", type=int, metavar="RUN", help="the run's id")
    cause.add_argument("--project", required=True, help="project name on the hub")
    json_option(cause, returns_object(*CAUSE_RESTORED, schema="CauseRestored"))
    cause.set_defaults(func=cmd_run_cause)
