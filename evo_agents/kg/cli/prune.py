"""``evo-agents kg prune``: the retention of a project's built graphs on the hub (``evo-agents hub kg prune``, which a
hub admin runs; ``evo_agents.hub.kg_prune``), for the project of the current directory, ``--project``, or every
project with ``--all``.

By default it only counts: the artifacts of graphs older than the ``--keep`` newest that would leave the bucket.
``--delete`` deletes them. A machine keeps no run logs, graph history or memo to prune: its kg home holds what a sync
needs only.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result
from evo_agents.hub.cli import positive
from evo_agents.kg.cli.base import JSON, PROJECT, KgCommand, KgContext, done, hub_project_args
from evo_agents.kg.cli.scrub import ALL


class KgPrune(KgCommand):
    name = "prune"
    help = "count, and with --delete delete, the artifacts of a project's older graphs on the hub (a hub admin)"
    arguments = (
        PROJECT,
        ALL,
        Arg(
            "--keep",
            type=positive,
            help="newest graphs of each project whose artifact stays (default: the hub's EVO_HUB_KG_KEEP_ARTIFACTS, 3)",
        ),
        Arg("--delete", action="store_true", help="delete what the count finds"),
        JSON,
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.cli import kg as kg_cli

        args = hub_project_args(ctx.args)
        args.dry_run, args.graph_files = not args.delete, False
        return done(kg_cli.KG_PRUNE(args))


PRUNE = KgPrune()
