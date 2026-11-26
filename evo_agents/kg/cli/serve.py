"""``evo-agents kg serve``: the MCP server over stdio of one project's graph, every tool answered by the hub
(``evo_agents.kg.serve``, ``evo_agents.hub.kg_remote``)."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result
from evo_agents.kg.cli.base import KgCommand, KgContext


class KgServe(KgCommand):
    name = "serve"
    help = "MCP server over stdio, bound to one project"
    arguments = (
        Arg("--project", help="project name or harness path (default: from the session directory)"),
        Arg("--sink", default="claude-code@anthropic", help="sink profile whose clearance applies"),
        Arg(
            "--backend",
            choices=["auto", "hub"],
            default="auto",
            help="where the graph is read: the hub; auto (default) is the same, kept for the .mcp.json files that name it",
        ),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.kg_remote import open_session
        from evo_agents.kg.serve import serve_stdio

        args = ctx.args
        return Result(None, status=serve_stdio(session=open_session(args.backend, args.project, args.sink)) or 0)


SERVE = KgServe()
