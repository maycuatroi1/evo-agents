"""``evo-agents hub kg graph list|create|update|delete``: the named graphs of a hub project (``docs/kg.md``, Named
graphs). A named graph is a set of the project's sources; the kg tools read one with ``graph``
(``evo-agents kg query --graph NAME``), ``default`` being every source. Writing needs the writer role. Standard library
only."""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object

GRAPH_KEYS = ("project", "graph")
LIST_KEYS = ("project", "graphs", "sources", "can_change")


def graph_json(project: str, found: dict) -> dict:
    """What ``create --json`` and ``update --json`` print, from the hub's answer: the named graph as it is now."""
    return {"project": project, "graph": found}


def _base(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}/graphs"


def _graph_line(graph: dict) -> str:
    sources = ", ".join(graph["sources"]) if graph["sources"] else "-"
    line = f"graph {graph['name']}: {sources}"
    if graph.get("missing"):
        line += f" (not in the newest build: {', '.join(graph['missing'])})"
    return line


@client_command
def cmd_graph_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _base(args.project))
    out = {"project": args.project, **{k: found[k] for k in LIST_KEYS if k != "project"}}
    if args.json:
        print_json(out)
        return 0
    rows = [
        (
            g["name"],
            "every source" if g["default"] else (", ".join(g["sources"]) or "-"),
            ", ".join(g.get("missing") or []) or "-",
            g.get("description") or "",
        )
        for g in out["graphs"]
    ]
    table(("GRAPH", "SOURCES", "NOT BUILT", "DESCRIPTION"), rows)
    print(f"sources of the newest build: {', '.join(out['sources']) or '-'}")
    return 0


@client_command
def cmd_graph_create(args) -> int:
    hub, _ = signed_in()
    body = {"name": args.name, "sources": list(args.source or []), "description": args.description or ""}
    found = hub.call("POST", _base(args.project), body)
    if args.json:
        print_json(graph_json(args.project, found))
        return 0
    print(f"created {_graph_line(found)}")
    return 0


@client_command
def cmd_graph_update(args) -> int:
    body: dict = {"add": list(args.add or []), "remove": list(args.remove or [])}
    if args.source is not None:
        body["sources"] = list(args.source)
    if args.rename is not None:
        body["rename"] = args.rename
    if args.description is not None:
        body["description"] = args.description
    if body == {"add": [], "remove": []}:
        print("error: say what changes: --rename, --description, --source, --add or --remove", file=sys.stderr)
        return 2
    hub, _ = signed_in()
    found = hub.call("PATCH", f"{_base(args.project)}/{quote(args.name, safe='')}", body)
    if args.json:
        print_json(graph_json(args.project, found))
        return 0
    print(f"updated {_graph_line(found)}")
    return 0


@client_command
def cmd_graph_delete(args) -> int:
    hub, _ = signed_in()
    found = hub.call("DELETE", f"{_base(args.project)}/{quote(args.name, safe='')}")
    if args.json:
        print_json(found)
        return 0
    print(f"deleted graph {found['deleted']} of project {args.project}; its sources and the build stay")
    return 0


def register(ksub) -> None:
    """``hub kg graph``, under the subparsers ``ksub`` of ``hub kg``."""
    graph = ksub.add_parser(
        "graph",
        help="list, create, update or delete the named graphs of a project: sets of its sources",
        description="A named graph is a set of a project's sources (repos, uploads, connectors); a source can be in "
        "several. The kg tools take `graph` and then answer from those sources alone, on the project's one build. "
        "'default' is every source.",
    )
    gsub = graph.add_subparsers(dest="graph_command", required=True)

    listed = gsub.add_parser("list", help="default and the named graphs of a project, with the sources built")
    listed.add_argument("--project", required=True, help="project name on the hub")
    json_option(listed, returns_object(*LIST_KEYS, schema="NamedGraphsOut", added=("project",)))
    listed.set_defaults(func=cmd_graph_list)

    create = gsub.add_parser("create", help="add a named graph of some of the project's sources (writer role)")
    create.add_argument("name", metavar="NAME", help="a-z, 0-9, '.', '_', '-'; not 'default'")
    create.add_argument("--project", required=True, help="project name on the hub")
    create.add_argument("--source", action="append", metavar="SOURCE", help="a source it holds (repeatable)")
    create.add_argument("--description", help="what it is for")
    json_option(create, returns_object(*GRAPH_KEYS))
    create.set_defaults(func=cmd_graph_create)

    update = gsub.add_parser("update", help="rename a named graph, or give it sources or strip it of some")
    update.add_argument("name", metavar="NAME", help="the named graph")
    update.add_argument("--project", required=True, help="project name on the hub")
    update.add_argument("--rename", metavar="NEW", help="its new name")
    update.add_argument("--description", help="what it is for")
    update.add_argument(
        "--source", action="append", metavar="SOURCE", help="its sources, all of them, in place of what it holds"
    )
    update.add_argument("--add", action="append", metavar="SOURCE", help="a source it gets (repeatable)")
    update.add_argument("--remove", action="append", metavar="SOURCE", help="a source it loses (repeatable)")
    json_option(update, returns_object(*GRAPH_KEYS))
    update.set_defaults(func=cmd_graph_update)

    delete = gsub.add_parser("delete", help="delete a named graph; its sources and the build stay")
    delete.add_argument("name", metavar="NAME", help="the named graph")
    delete.add_argument("--project", required=True, help="project name on the hub")
    json_option(delete, returns_object("project", "deleted", schema="NamedGraphDeleted"))
    delete.set_defaults(func=cmd_graph_delete)
