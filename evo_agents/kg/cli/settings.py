"""``evo-agents kg settings``: show the project's KG settings on the hub, set some (``--set KEY=VALUE``, a reason with
``--reason``) or put back the last change (``--undo``), as ``evo-agents hub kg settings`` does."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result
from evo_agents.kg.cli.base import WITH_PROJECT, KgCommand, KgContext, done, on_hub


class KgSettings(KgCommand):
    name = "settings"
    help = "show, set (--set KEY=VALUE) or undo the project's KG settings on the hub"
    arguments = (
        *WITH_PROJECT,
        Arg("--set", action="append", metavar="KEY=VALUE", help="set a key (repeatable)"),
        Arg("--reason", help="why, kept with the change"),
        Arg("--figures", help="json object of the figures that justify the change, kept with it"),
        Arg("--undo", action="store_true", help="put back what the last change replaced"),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.hub.cli.kg.settings import SETTINGS

        args = ctx.args
        return done(
            on_hub(
                args, SETTINGS, set=args.set, undo=args.undo, strategy=None, reason=args.reason, figures=args.figures
            )
        )


SETTINGS = KgSettings()
