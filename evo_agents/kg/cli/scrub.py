"""``evo-agents kg scrub``: old copies of secrets of a project on the hub (``evo-agents hub kg scrub``, which a hub
admin runs; ``evo_agents.hub.server.kg_scrub``), for the project of the current directory, ``--project``, or every
project with ``--all``.

By default it only reads: the graph in the hub's tables, and the old graph files and source blobs holding a secret, by
hash, kind and the kinds of secret, never a value. ``--purge`` says what it is about to delete, asks (or takes
``--yes``), and has the hub delete the old ones. A machine keeps no corpus, memo or graph to scrub.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result
from evo_agents.kg.cli.base import JSON, PROJECT, KgCommand, KgContext, done, hub_project_args

ALL = Arg("--all", action="store_true", help="every project on the hub")


class KgScrub(KgCommand):
    name = "scrub"
    help = "find old copies of secrets of a project on the hub, and with --purge delete them (a hub admin)"
    arguments = (
        PROJECT,
        ALL,
        Arg("--purge", action="store_true", help="delete the old ones; the graph in the tables and live blobs stay"),
        Arg("--yes", action="store_true", help="with --purge: delete without asking"),
        JSON,
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.cli import kg as kg_cli

        return done(kg_cli.KG_SCRUB(hub_project_args(ctx.args)))


SCRUB = KgScrub()
