"""``evo-agents hub mcp``: the MCP server over stdio that every runtime starts, as a class of ``evo_agents.clikit``.

The proxy itself, which carries each JSON-RPC line to the hub's /mcp and answers in the hub's place when it cannot,
lives in ``evo_agents.hub.mcp_proxy``; :class:`Mcp` finds the session's project and starts it on stdin and stdout.
"""

from __future__ import annotations

import argparse
import sys

from evo_agents.clikit import Arg, Command, Context, Result
from evo_agents.hub import mcp_proxy
from evo_agents.hub.memory import AGENT_SINK


class Mcp(Command[Context]):
    name = "mcp"
    help = "MCP server over stdio for any runtime, answered by the hub's /mcp with this machine's token"
    arguments = (
        Arg("--project", help="project name or harness path (default: the project of the working directory)"),
        Arg("--sink", default=AGENT_SINK, help=f"sink whose clearance applies (default: {AGENT_SINK})"),
    )

    def context(self, args: argparse.Namespace) -> Context:
        return Context(args)

    def run(self, ctx: Context) -> Result:
        project = mcp_proxy.hub_project(ctx.args.project)
        if project is None and mcp_proxy.current_run() is None:  # a run's agent gets the run's project from the hub
            print(
                "evo-agents hub mcp: no project for this directory: the kg_* tools need --project, the other tools "
                "a project argument",
                file=sys.stderr,
            )
        from evo_agents.hub import claude_plugins

        kg = not claude_plugins.session_has(mcp_proxy.KG_PLUGIN)
        proxy = mcp_proxy.Proxy(project, ctx.args.sink, kg=kg)
        return Result(None, status=proxy.run(sys.stdin.buffer, sys.stdout.buffer))


MCP = Mcp()
