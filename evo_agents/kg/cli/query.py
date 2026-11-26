"""``evo-agents kg query``: call a kg tool from the shell, as the hub answers it (``evo_agents.hub.kg_remote``)."""

from __future__ import annotations

import argparse
import json

from evo_agents.clikit import Arg, Result, Text
from evo_agents.kg.cli.base import WITH_PROJECT, KgCommand, KgContext, shown

QUERY_TOOLS = ["kg_search", "kg_context", "kg_node", "kg_impact", "kg_path", "kg_status", "kg_more"]
BACKEND_HELP = "where the graph is read: the hub; auto is the same, kept for old scripts"


def query_tool(args) -> None:
    """Resolve ``kg query``'s first argument: a kg_ tool, a short name of one (search, node...), or with no second
    argument the text of kg_search, so ``kg query SnapshotCoordinator`` searches; an unknown tool before a text is
    refused as argparse refuses an invalid choice."""
    if args.tool in QUERY_TOOLS:
        return
    if f"kg_{args.tool}" in QUERY_TOOLS:
        args.tool = f"kg_{args.tool}"
    elif args.text is None:
        args.tool, args.text = "kg_search", args.tool
    else:
        choices = ", ".join(map(repr, QUERY_TOOLS))
        args.query_parser.error(f"argument tool: invalid choice: {args.tool!r} (choose from {choices})")


class KgQuery(KgCommand):
    name = "query"
    help = "call a kg tool from the shell"
    arguments = (
        *WITH_PROJECT,
        Arg(
            "tool",
            metavar="tool",
            help=f"one of {', '.join(QUERY_TOOLS)}, or its short name (search, node...); "
            "alone, any other word is the text of kg_search",
        ),
        Arg("text", nargs="?", help="query, node id (kg_path: from) or handle"),
        Arg("--arg", action="append", help="extra tool argument key=value (JSON values allowed)"),
        Arg("--sink", default="cli", help="sink whose clearance applies (default: cli)"),
        Arg(
            "--graph",
            metavar="NAME",
            help="only the sources of this named graph of the project (`hub kg graph list`; needs the hub); "
            "default: every source",
        ),
        Arg("--backend", choices=["auto", "hub"], default="hub", help=BACKEND_HELP),
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.set_defaults(query_parser=parser)  # query_tool refuses an unknown tool as this parser's error

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.kg_remote import open_session

        args = ctx.args
        query_tool(args)
        session = open_session(args.backend, args.project, args.sink)
        params = {}
        for pair in args.arg or []:
            key, _, value = pair.partition("=")
            try:
                params[key] = json.loads(value)
            except json.JSONDecodeError:
                params[key] = value
        if args.text is not None:
            first = {"kg_node": "id", "kg_impact": "id", "kg_path": "from", "kg_more": "handle"}
            params.setdefault(first.get(args.tool, "query"), args.text)
        if getattr(args, "graph", None):  # a named graph of the project on the hub (docs/kg.md, Named graphs)
            params["graph"] = args.graph
        result = session.call(args.tool, params)
        text = Text(*(block.get("text", "") for block in result.get("content", [])))
        return shown(ctx, result.get("structuredContent", result), text, 1 if result.get("isError") else 0)


QUERY = KgQuery()
