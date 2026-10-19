"""``evo-agents hub curator history``: the night's history run, which reads the project's sessions and runs with a
cheap model (``evo_agents.hub.history``).

``curator history status`` says where it stands (GET /v1/projects/{p}/curator/history): its cap and the model it runs
on, how many of the project's sessions and runs have a facet of the model and how many wait, and the latest history
runs with what each wrote and cost. ``curator history clusters`` lists the clusters the facets fall into, those of 3
sessions or more as the night's figures hold them (GET .../curator/history/clusters): each with its sessions, runs,
members and its representative session, the latest.

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.curator import money
from evo_agents.hub.plan_cli import project_of_args

STATUS_KEYS = ("project", "budget_usd", "runtime", "model", "llm", "runs", "clusters", "hidden")
CLUSTER_LIST_KEYS = ("project", "min_sessions", "clusters", "hidden", "counted_at")


def _path(project: str, *parts: str) -> str:
    return "/".join((f"/v1/projects/{quote(project, safe='')}/curator/history", *parts))


@client_command
def cmd_status(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("GET", _path(project))
    if args.json:
        print_json(found)
        return 0
    cap = found["budget_usd"]
    if cap is None:
        print(f"Project {project} has no charter: no history run")
    else:
        model = found["model"] or "the cheapest the worker on duty reports"
        spend = f"at most {money(cap)} a night" if cap else "none: history.budget_usd is 0"
        print(f"History run of {project}: {spend}, on {found['runtime']} with {model}")
    llm = found["llm"]
    print(
        f"Facets of the model: {llm['analyzed']} of {llm['items']} sessions and runs, {llm['waiting']} waiting, "
        f"{llm['failed']} whose last attempt failed (version {llm['version']})"
    )
    rows = [
        (
            f"#{item['id']}",
            item["night"] or "-",
            item["state"],
            item["model"] or "-",
            money(item["cost_usd"]) if item["cost_usd"] is not None else "-",
            str(item["analyzed"]),
            str(item["failed"]),
            str(item["named"]),
        )
        for item in found["runs"]
    ]
    if rows:
        table(("RUN", "NIGHT", "STATE", "MODEL", "COST", "FACETS", "FAILED", "NAMED"), rows)
    print(f"{found['clusters']} cluster(s) in the night's figures, {found['hidden']} smaller left out")
    return 0


@client_command
def cmd_clusters(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("GET", _path(project, "clusters") + f"?{urlencode({'limit': args.limit})}")
    if args.json:
        print_json(found)
        return 0
    rows = [
        (
            item["name"],
            str(item["sessions"]),
            str(item["runs"]),
            str(item["members"]),
            item["failure_mode"],
            item["representative"] or "-",
        )
        for item in found["clusters"]
    ]
    if rows:
        table(("CLUSTER", "SESSIONS", "RUNS", "MEMBERS", "MODE", "REPRESENTATIVE"), rows)
    print(
        f"{len(rows)} cluster(s) of {found['min_sessions']} sessions or more in {project}, {found['hidden']} smaller "
        "left out"
    )
    return 0


def register_history(csub, with_project) -> None:
    """``curator history`` and its commands."""
    parser = csub.add_parser("history", help="the night's history run: facets of a cheap model and their clusters")
    hsub = parser.add_subparsers(dest="history_command", required=True)
    status = hsub.add_parser("status", help="its cap and model, the facets of the model, its latest runs")
    with_project(status)
    json_option(status, returns_object(*STATUS_KEYS, schema="HistoryStatus"))
    status.set_defaults(func=cmd_status)
    clusters = hsub.add_parser("clusters", help="the clusters of 3 sessions or more, as the night's figures hold them")
    with_project(clusters)
    clusters.add_argument("--limit", type=int, default=50, choices=range(1, 201), metavar="N", help="1 to 200")
    json_option(clusters, returns_object(*CLUSTER_LIST_KEYS, schema="FacetClusterList"))
    clusters.set_defaults(func=cmd_clusters)
