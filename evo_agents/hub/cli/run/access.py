"""``evo-agents hub run access RUN --as-me|--revoke``, for ``evo_agents.hub.cli.run``: let a run of yours call the hub's
API as you, secrets, tokens and workers aside, or stop it (PUT /v1/projects/{p}/runs/{id}/access). With ``--revoke``
the run's token ends at once, and its agent's next call to the hub gets 401 (``docs/credentials.md``, "A run that runs
as its owner"). Only the run's owner may, never a run's own token. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Result, Text
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext
from evo_agents.hub.cli.run.keys import RUN_OUTPUT
from evo_agents.hub.cli.run.options import AS_ME_HELP, RUN_ID
from evo_agents.hub.cli.run.resources import Runs


def said(run: dict) -> str:
    """What ``access`` prints of the run the hub answered."""
    if run.get("as_owner"):
        return (
            f"Run #{run['id']} runs as you: while a worker holds it, its agent calls the hub's API as you, secrets, "
            f"tokens and workers aside, through a token of its own. `evo-agents hub run access {run['id']} --revoke` "
            "stops it."
        )
    return f"Run #{run['id']} no longer runs as you: the token it held, if any, has ended."


class RunAccess(HubCommand):
    name = "access"
    help = "let a run of yours call the hub's API as you, secrets, tokens and workers aside, or stop it (owner only)"
    arguments = (RUN_ID, PROJECT)
    output = RUN_OUTPUT

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        chosen = parser.add_mutually_exclusive_group(required=True)
        chosen.add_argument("--as-me", dest="as_me", action="store_true", default=None, help=AS_ME_HELP)
        chosen.add_argument(
            "--revoke",
            dest="as_me",
            action="store_false",
            default=None,
            help="stop it: the run's token ends at once, and its agent's next call to the hub gets 401",
        )

    def run(self, ctx: HubContext) -> Result:
        run = Runs.of(ctx).access(ctx.args.run, ctx.args.as_me)
        return Result(run, Text(said(run)))
