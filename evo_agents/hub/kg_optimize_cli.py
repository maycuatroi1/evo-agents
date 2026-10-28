"""``evo-agents hub kg optimize``: start a KG optimize run of a project now, or read its runs.

``optimize --project P`` asks the hub for a KG optimize run (POST /v1/kg/{project}/optimize, the writer role): at any
hour, under the review cost cap of the project's charter. The hub measures the graph, gives a review run to the worker
on duty, and measures again after the next build (``evo_agents.hub.server.kg_optimize``); the owner of the night shift
gets the report. One run of a project is in flight at a time: asked again, the hub answers with that run. ``--status``
lists the project's runs, newest first, with each one's summary (GET). ``--json`` prints the hub's answer. Standard
library only.
"""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in
from evo_agents.hub.contract import json_option, returns_object

OPTIMIZE_RUN = (
    "id",
    "project",
    "via",
    "state",
    "night",
    "agent_run_id",
    "before_build_id",
    "after_build_id",
    "asked_weight",
    "summary",
    "report",
    "error",
    "created_at",
    "updated_at",
    "ended_at",
    "body",
)
OPTIMIZE_RUNS = ("active", "runs")


def _path(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}/optimize"


def _line(run: dict) -> str:
    review = f", review run {run['agent_run_id']}" if run.get("agent_run_id") else ""
    return f"#{run['id']} {run['via']} {run['state']}{review}: {run['summary']}"


@_client_command
def cmd_optimize(args) -> int:
    hub, _ = _signed_in()
    if args.status:
        found = hub.call("GET", f"{_path(args.project)}?limit={args.limit}")
        if args.json:
            _print_json(found)
            return 0
        if not found["runs"]:
            print(f"project {args.project} has no KG optimize run yet")
        for run in found["runs"]:
            print(_line(run))
        return 0
    found = hub.call("POST", _path(args.project), {})
    if args.json:
        _print_json(found)
        return 0
    print(f"KG optimize run of project {args.project}: {_line(found)}")
    if found.get("report"):
        print(found["report"])
    return 0


def register(ksub) -> None:
    from evo_agents.hub.cli import _positive

    parser = ksub.add_parser(
        "optimize",
        help="start a KG optimize run of a project now (measure, fix, rebuild, measure again), or list its runs",
    )
    parser.add_argument("--project", required=True, help="project name on the hub")
    parser.add_argument("--status", action="store_true", help="list the project's runs instead of starting one")
    parser.add_argument("--limit", type=_positive, default=10, help="with --status: the newest runs listed")
    json_option(
        parser,
        returns_object(
            *OPTIMIZE_RUN,
            schema="OptimizeRun",
            variants=(("--status", returns_object(*OPTIMIZE_RUNS, schema="OptimizeRuns")),),
        ),
        help="machine-readable output: the run started or in flight, or with --status the project's runs",
    )
    parser.set_defaults(func=cmd_optimize)
