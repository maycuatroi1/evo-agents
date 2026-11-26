"""``evo-agents hub kg graph list|create|update|delete``: the named graphs of a hub project (``docs/kg.md``, Named
graphs). A named graph is a set of the project's sources; the kg tools read one with ``graph``
(``evo-agents kg query --graph NAME``), ``default`` being every source. Writing needs the writer role. Standard library
only.

:data:`GRAPH` is the group of class commands of ``evo_agents.clikit``: :class:`KgGraphs` is the gateway to its
routes, :class:`GraphChange` the value object that turns the options of ``update`` into its body."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource

GRAPH_KEYS = ("project", "graph")
LIST_KEYS = ("project", "graphs", "sources", "can_change")
GRAPH_COLUMNS = ("GRAPH", "SOURCES", "NOT BUILT", "DESCRIPTION")
NO_CHANGE = "say what changes: --rename, --description, --source, --add or --remove"


class KgGraphs(KgResource):
    """The named graphs of a project: GET and POST /v1/kg/{p}/graphs, PATCH and DELETE .../graphs/{name}."""

    collection = "graphs"

    def list(self) -> dict:
        return self.object("GET")

    def create(self, body: dict) -> dict:
        return self.object("POST", body=body)

    def update(self, name: str, body: dict) -> dict:
        return self.object("PATCH", name, body=body)

    def delete(self, name: str) -> dict:
        return self.object("DELETE", name)


@dataclass(frozen=True)
class GraphChange:
    """What ``graph update`` changes: sources added and removed, and the sources, name and description it sets."""

    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    sources: list[str] | None = None
    rename: str | None = None
    description: str | None = None

    @classmethod
    def of(cls, args: argparse.Namespace) -> GraphChange:
        """UsageError when the options change nothing."""
        sources = None if args.source is None else list(args.source)
        change = cls(list(args.add or []), list(args.remove or []), sources, args.rename, args.description)
        if change.body() == {"add": [], "remove": []}:
            raise UsageError(NO_CHANGE)
        return change

    def body(self) -> dict:
        body: dict = {"add": self.add, "remove": self.remove}
        if self.sources is not None:
            body["sources"] = self.sources
        if self.rename is not None:
            body["rename"] = self.rename
        if self.description is not None:
            body["description"] = self.description
        return body


def graph_json(project: str, found: dict) -> dict:
    """What ``create --json`` and ``update --json`` print, from the hub's answer: the named graph as it is now."""
    return {"project": project, "graph": found}


def graph_line(graph: dict) -> str:
    sources = ", ".join(graph["sources"]) if graph["sources"] else "-"
    line = f"graph {graph['name']}: {sources}"
    if graph.get("missing"):
        line += f" (not in the newest build: {', '.join(graph['missing'])})"
    return line


NAME = Arg("name", metavar="NAME", help="the named graph")
DESCRIPTION = Arg("--description", help="what it is for")


class GraphList(HubCommand):
    name = "list"
    help = "default and the named graphs of a project, with the sources built"
    arguments = (KG_PROJECT,)
    output = returns_object(*LIST_KEYS, schema="NamedGraphsOut", added=("project",))

    def run(self, ctx: HubContext) -> Result:
        found = KgGraphs.of(ctx).list()
        out = {"project": ctx.project, **{k: found[k] for k in LIST_KEYS if k != "project"}}
        rows = [
            (
                g["name"],
                "every source" if g["default"] else (", ".join(g["sources"]) or "-"),
                ", ".join(g.get("missing") or []) or "-",
                g.get("description") or "",
            )
            for g in out["graphs"]
        ]
        built = f"sources of the newest build: {', '.join(out['sources']) or '-'}"
        return Result(out, Views(Table(GRAPH_COLUMNS, rows, bare=True), Text(built)))


class GraphCreate(HubCommand):
    name = "create"
    help = "add a named graph of some of the project's sources (writer role)"
    arguments = (
        Arg("name", metavar="NAME", help="a-z, 0-9, '.', '_', '-'; not 'default'"),
        KG_PROJECT,
        Arg("--source", action="append", metavar="SOURCE", help="a source it holds (repeatable)"),
        DESCRIPTION,
    )
    output = returns_object(*GRAPH_KEYS)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"name": args.name, "sources": list(args.source or []), "description": args.description or ""}
        found = KgGraphs.of(ctx).create(body)
        return Result(graph_json(ctx.project, found), Text(f"created {graph_line(found)}"))


class GraphUpdate(HubCommand):
    name = "update"
    help = "rename a named graph, or give it sources or strip it of some"
    arguments = (
        NAME,
        KG_PROJECT,
        Arg("--rename", metavar="NEW", help="its new name"),
        DESCRIPTION,
        Arg("--source", action="append", metavar="SOURCE", help="its sources, all of them, in place of what it holds"),
        Arg("--add", action="append", metavar="SOURCE", help="a source it gets (repeatable)"),
        Arg("--remove", action="append", metavar="SOURCE", help="a source it loses (repeatable)"),
    )
    output = returns_object(*GRAPH_KEYS)

    def check(self, args: argparse.Namespace) -> None:
        GraphChange.of(args)

    def run(self, ctx: HubContext) -> Result:
        found = KgGraphs.of(ctx).update(ctx.args.name, GraphChange.of(ctx.args).body())
        return Result(graph_json(ctx.project, found), Text(f"updated {graph_line(found)}"))


class GraphDelete(HubCommand):
    name = "delete"
    help = "delete a named graph; its sources and the build stay"
    arguments = (NAME, KG_PROJECT)
    output = returns_object("project", "deleted", schema="NamedGraphDeleted")

    def run(self, ctx: HubContext) -> Result:
        found = KgGraphs.of(ctx).delete(ctx.args.name)
        line = f"deleted graph {found['deleted']} of project {ctx.project}; its sources and the build stay"
        return Result(found, Text(line))


GRAPH = Group(
    "graph",
    "list, create, update or delete the named graphs of a project: sets of its sources",
    (GraphList(), GraphCreate(), GraphUpdate(), GraphDelete()),
    description="A named graph is a set of a project's sources (repos, uploads, connectors); a source can be in "
    "several. The kg tools take `graph` and then answer from those sources alone, on the project's one build. "
    "'default' is every source.",
)
NODES = (GRAPH,)
