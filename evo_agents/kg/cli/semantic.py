"""``evo-agents kg semantic``: whether the project's graph on the hub holds vectors (``evo-agents hub kg semantic``);
with ``--mode`` or ``--model``, set them in the project's settings on the hub first. The hub embeds at its next
build."""

from __future__ import annotations

import argparse
import contextlib
import io

from evo_agents.clikit import Arg, Result
from evo_agents.kg.cli.base import WITH_PROJECT, KgCommand, KgContext, done, on_hub
from evo_agents.kg.cli.profile import SINK


def quietly(command):
    """``command`` with its stdout dropped, and its errors still on stderr."""

    def run(args) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return command(args)

    return run


class KgSemantic(KgCommand):
    name = "semantic"
    help = "show or set whether kg_search on the hub ranks by meaning too (docs/kg-semantic.md)"
    arguments = (
        *WITH_PROJECT,
        Arg(
            "--mode",
            choices=["off", "index", "on"],
            help="off: words only; index: the hub's builds write vectors; on: and kg_search uses them",
        ),
        Arg("--model", help="embedding model the hub's builds use (default BAAI/bge-m3)"),
        Arg("--reason", help="why, kept with the change of the settings"),
        SINK,
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.cli.kg.semantic import SEMANTIC
        from evo_agents.hub.cli.kg.settings import SETTINGS

        args = ctx.args
        if args.mode or args.model:
            values = [f"semantic_mode={args.mode}"] * bool(args.mode) + [f"semantic_model={args.model}"] * bool(
                args.model
            )
            quiet = argparse.Namespace(project=args.project, json=True)  # the settings it printed would come twice
            code = on_hub(
                quiet, quietly(SETTINGS), set=values, undo=False, strategy=None, reason=args.reason, figures=None
            )
            if code:
                return done(code)
        return done(on_hub(args, SEMANTIC, sink=args.sink))


SEMANTIC = KgSemantic()
