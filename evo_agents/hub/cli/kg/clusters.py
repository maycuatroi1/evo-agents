"""``evo-agents hub kg clusters``: the clusters of a project's graph you can see on the hub.

``clusters --project P`` lists the level-1 clusters, largest first (GET /v1/kg/{project}/clusters without the
members), and with ``--json`` prints them as an array of the hub's ClusterSummary (id, label, size, kinds, ...).
``clusters show ID --project P`` prints one cluster of either level: its subclusters, its neighbouring clusters and
its members (GET /v1/kg/{project}/cluster), and with ``--json`` the hub's ClusterDetail. Sizes, links and labels are
the hub's, counted over what you can see (``evo_agents.hub.kg_map``). Standard library only.

:data:`CLUSTERS` is a class command of ``evo_agents.clikit``; :class:`KgClusters` is the gateway to its routes.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, JsonOutput, Result, Table, Text, UsageError, View, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource

MEMBERS_SHOWN = 30  # members a text `show` lists; --json lists them all
CLUSTER_SUMMARY = ("id", "level", "parent", "label", "size", "kinds", "subclusters")
CLUSTER_DETAIL = ("graph", "cluster", "subclusters", "members", "edges", "neighbours")
SUMMARY_COLUMNS = ("CLUSTER", "SIZE", "SUBS", "LABEL", "KINDS")


class KgClusters(KgResource):
    """The cluster map of a project's graph: GET /v1/kg/{p}/clusters and GET /v1/kg/{p}/cluster?id=ID."""

    def summaries(self) -> dict:
        """The clusters and the links between them, without their members."""
        return self.call("GET", "clusters", nodes="false")

    def get(self, cluster_id: str) -> dict:
        return self.call("GET", "cluster", id=cluster_id)


def summary_row(cluster: dict) -> tuple:
    kinds = ", ".join(f"{kind} {count}" for kind, count in sorted(cluster["kinds"].items(), key=lambda kv: -kv[1]))
    return (cluster["id"], cluster["size"], cluster["subclusters"], cluster["label"], kinds)


def list_view(project: str, found: dict) -> View:
    if not found["clusters"]:
        return Text(f"project {project} shows you no cluster: {found['reason']}")
    rows = [summary_row(c) for c in found["clusters"]]
    return Views(Table(SUMMARY_COLUMNS, rows), Text(f"{len(found['links'])} links between them"))


def detail_view(found: dict) -> View:
    cluster = found["cluster"]
    parts: list[View] = [
        Text(
            f"Cluster {cluster['id']} (level {cluster['level']}): {cluster['label']}",
            f"{cluster['size']} members you can see" + (f", in {cluster['parent']}" if cluster["parent"] else ""),
        )
    ]
    if found["subclusters"]:
        parts += [Text("\nSubclusters:"), Table(SUMMARY_COLUMNS, [summary_row(c) for c in found["subclusters"]])]
    if found["neighbours"]:
        rows = [(n["id"], n["weight"], n["size"], n["label"]) for n in found["neighbours"]]
        parts += [Text("\nNeighbours:"), Table(("CLUSTER", "EDGES", "SIZE", "LABEL"), rows)]
    members = found["members"]
    rows = [(m["id"], m["kind"], m["name"] or "") for m in members[:MEMBERS_SHOWN]]
    parts += [Text("\nMembers:"), Table(("NODE", "KIND", "NAME"), rows, bare=True)]
    if len(members) > MEMBERS_SHOWN:
        parts.append(Text(f"... and {len(members) - MEMBERS_SHOWN} more (--json lists them all)"))
    return Views(*parts)


class KgClustersCommand(HubCommand):
    name = "clusters"
    help = "the clusters of a project's graph you can see, or one of them with `show ID`"
    arguments = (
        Arg("action", nargs="?", choices=["show"], help="show one cluster"),
        Arg("id", metavar="ID", nargs="?", help="with show: the cluster's id"),
        KG_PROJECT,
    )
    output = JsonOutput(
        "array",
        CLUSTER_SUMMARY,
        "ClusterSummary",
        variants=(("show", returns_object(*CLUSTER_DETAIL, schema="ClusterDetail")),),
    )
    json_help = "machine-readable output: an array of level-1 clusters, or with show the cluster"

    def check(self, args: argparse.Namespace) -> None:
        if args.action == "show" and not args.id:
            raise UsageError("clusters show needs a cluster ID")
        if args.action is None and args.id:
            raise UsageError("give a cluster ID after show: clusters show ID")

    def run(self, ctx: HubContext) -> Result:
        clusters = KgClusters.of(ctx)
        if ctx.args.action == "show":
            found = clusters.get(ctx.args.id)
            return Result(found, detail_view(found))
        found = clusters.summaries()
        return Result(found["clusters"], list_view(ctx.project, found))


CLUSTERS = KgClustersCommand()
NODES = (CLUSTERS,)
