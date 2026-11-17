"""``evo-agents hub run priority RUN LEVEL``, for ``evo_agents.hub.run_cli``: change the priority of a queued run of the
project, a writer of it (POST /v1/projects/{p}/runs/{id}/priority). A worker claims its queued runs by priority, urgent
first and low last, then the oldest; a run already held keeps its slot. The run's log says who changed it. A refusal
(403 below writer, 404, 409 for a run no longer queued) prints the hub's reason and exits 1. Standard library only,
like the rest of the client.
"""

from __future__ import annotations

from evo_agents.hub.cli_client import client_command, print_json, project_path, signed_in
from evo_agents.hub.contract import json_option
from evo_agents.hub.runs import PRIORITIES


def cmd_priority(args, project) -> int:
    hub, _ = signed_in()
    run = hub.call("POST", f"{project_path(project(args))}/runs/{args.run}/priority", {"priority": args.level})
    if args.json:
        print_json(run)
        return 0
    print(f"Run #{run['id']} is {run['state']} with priority {run['priority']}.")
    return 0


def register_priority(rsub, with_run, project, run_json) -> None:
    priority = rsub.add_parser(
        "priority",
        help="change the priority of a queued run, which a worker claims its queued runs by (the writer role)",
    )
    with_run(priority)
    priority.add_argument("level", metavar="LEVEL", choices=PRIORITIES, help=f"one of {', '.join(PRIORITIES)}")
    json_option(priority, run_json)
    priority.set_defaults(func=client_command(lambda args: cmd_priority(args, project)))
