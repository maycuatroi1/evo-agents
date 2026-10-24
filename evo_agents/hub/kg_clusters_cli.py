"""``evo-agents hub kg clusters``: the clusters of a project's graph you can see on the hub.

``clusters --project P`` lists the level-1 clusters, largest first (GET /v1/kg/{project}/clusters without the
members), and with ``--json`` prints them as an array of the hub's ClusterSummary (id, label, size, kinds, ...).
``clusters show ID --project P`` prints one cluster of either level: its subclusters, its neighbouring clusters and
its members (GET /v1/kg/{project}/cluster), and with ``--json`` the hub's ClusterDetail. Sizes, links and labels are
the hub's, counted over what you can see (``evo_agents.hub.kg_map``). Standard library only.
"""

from __future__ import annotations

import sys
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in, _table
from evo_agents.hub.contract import JsonOutput, json_option, returns_object

MEMBERS_SHOWN = 30  # members a text `show` lists; --json lists them all


def _base(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}"


def _summary_row(cluster: dict) -> tuple:
    kinds = ", ".join(f"{kind} {count}" for kind, count in sorted(cluster["kinds"].items(), key=lambda kv: -kv[1]))
    return (cluster["id"], cluster["size"], cluster["subclusters"], cluster["label"], kinds)


def _list(hub, args) -> int:
    found = hub.call("GET", f"{_base(args.project)}/clusters?nodes=false")
    if args.json:
        _print_json(found["clusters"])
        return 0
    if not found["clusters"]:
        print(f"project {args.project} shows you no cluster: {found['reason']}")
        return 0
    _table(("CLUSTER", "SIZE", "SUBS", "LABEL", "KINDS"), [_summary_row(c) for c in found["clusters"]])
    print(f"{len(found['links'])} links between them")
    return 0


def _show(hub, args) -> int:
    found = hub.call("GET", f"{_base(args.project)}/cluster?{urlencode({'id': args.id})}")
    if args.json:
        _print_json(found)
        return 0
    cluster = found["cluster"]
    print(f"Cluster {cluster['id']} (level {cluster['level']}): {cluster['label']}")
    print(f"{cluster['size']} members you can see" + (f", in {cluster['parent']}" if cluster["parent"] else ""))
    if found["subclusters"]:
        print("\nSubclusters:")
        _table(("CLUSTER", "SIZE", "SUBS", "LABEL", "KINDS"), [_summary_row(c) for c in found["subclusters"]])
    if found["neighbours"]:
        print("\nNeighbours:")
        _table(
            ("CLUSTER", "EDGES", "SIZE", "LABEL"),
            [(n["id"], n["weight"], n["size"], n["label"]) for n in found["neighbours"]],
        )
    members = found["members"]
    print("\nMembers:")
    _table(("NODE", "KIND", "NAME"), [(m["id"], m["kind"], m["name"] or "") for m in members[:MEMBERS_SHOWN]])
    if len(members) > MEMBERS_SHOWN:
        print(f"... and {len(members) - MEMBERS_SHOWN} more (--json lists them all)")
    return 0


@_client_command
def cmd_clusters(args) -> int:
    if args.action == "show" and not args.id:
        print("error: clusters show needs a cluster ID", file=sys.stderr)
        return 2
    if args.action is None and args.id:
        print("error: give a cluster ID after show: clusters show ID", file=sys.stderr)
        return 2
    hub, _ = _signed_in()
    return _show(hub, args) if args.action == "show" else _list(hub, args)


CLUSTER_SUMMARY = ("id", "level", "parent", "label", "size", "kinds", "subclusters")
CLUSTER_DETAIL = ("graph", "cluster", "subclusters", "members", "edges", "neighbours")


def register(ksub) -> None:
    clusters = ksub.add_parser(
        "clusters", help="the clusters of a project's graph you can see, or one of them with `show ID`"
    )
    clusters.add_argument("action", nargs="?", choices=["show"], help="show one cluster")
    clusters.add_argument("id", metavar="ID", nargs="?", help="with show: the cluster's id")
    clusters.add_argument("--project", required=True, help="project name on the hub")
    json_option(
        clusters,
        JsonOutput(
            "array",
            CLUSTER_SUMMARY,
            "ClusterSummary",
            variants=(("show", returns_object(*CLUSTER_DETAIL, schema="ClusterDetail")),),
        ),
        help="machine-readable output: an array of level-1 clusters, or with show the cluster",
    )
    clusters.set_defaults(func=cmd_clusters)
