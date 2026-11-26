"""``evo-agents hub contract print|check``: the hub's command line as data, and a check of the hub commands written
in markdown, as classes of ``evo_agents.clikit``.

What the contract holds and how a markdown file is checked live in ``evo_agents.hub.contract``, which walks the
parser of the whole CLI. ``print`` is a server command (``evo_agents.hub.cli.ServerCommand``), since the contract
carries the OpenAPI document and needs the hub-server extra; ``check`` needs only the core package.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Command, Context, Group, Result, Text, UsageError
from evo_agents.hub import contract
from evo_agents.hub.cli import MissingExtra, ServerCommand


class ContractPrint(ServerCommand):
    name = "print"
    help = "print the commands, their options and --json keys, and the OpenAPI document, as JSON"

    def run(self, ctx: Context) -> Result:
        try:
            text = contract.render(contract.build())
        except ImportError as exc:
            raise MissingExtra(exc) from exc
        ctx.out.write(text)
        return Result(None)


class ContractCheck(Command[Context]):
    name = "check"
    help = "report `evo-agents hub` commands in markdown code that the command line does not have"
    arguments = (
        Arg("files", metavar="FILE", nargs="+", help="markdown files"),
        Arg("--contract", help="a saved `contract print` output to check against (default: this one)"),
    )

    def context(self, args: argparse.Namespace) -> Context:
        return Context(args)

    def run(self, ctx: Context) -> Result:
        args = ctx.args
        try:
            known = contract.load_commands(args.contract) if args.contract else contract.commands()
            count, problems = contract.check_files(known, args.files)
        except (OSError, ValueError) as exc:
            raise UsageError(str(exc)) from exc  # error: <text>, exit status 2
        files = f"{len(args.files)} file(s)"
        if problems:
            summary = f"{len(problems)} problem(s) in the {count} hub command(s) of {files}"
            return Result(None, Text(*problems, summary), contract.EXIT_PROBLEMS)
        return Result(None, Text(f"{count} hub command(s) in {files}, all in the contract"))


CONTRACT_PRINT = ContractPrint()
CONTRACT = Group(
    "contract",
    "the hub command line as data (seam hub-cli-v1), and a check of docs",
    [CONTRACT_PRINT, ContractCheck()],
)
