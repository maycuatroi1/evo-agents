"""``evo-agents hub registry pull``: the harness registry of this machine, written from the hub.

``pull`` writes the cluster of every hub project the signed-in login sees into the registry (``evo_agents.hub.registry``
does it); other clusters stay as they are, and the previous file is kept as ``registry.json.bak.<timestamp>``. The
command is a class of ``evo_agents.clikit`` (docs/structure.md, section Command classes). Standard library only, like
the rest of the client.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext

if TYPE_CHECKING:
    from evo_agents.hub.registry import PullResult

COLUMNS = ("STATUS", "CLUSTER", "PROJECT", "ROOT")


def pulled_view(result: PullResult) -> Views:
    """What ``pull`` prints of the ``registry.PullResult`` it wrote."""
    if not result.changed:
        said = f"{result.registry} already holds every hub project you see; nothing written."
    elif result.backup:
        said = f"Wrote {result.registry}; the previous version is saved as {result.backup}."
    else:
        said = f"Wrote {result.registry}, which did not exist before."
    rows = [
        (c["status"], c["name"], c["project"], c["root"] + ("" if c["present"] else "  (not on this machine)"))
        for c in result.clusters
    ]
    return Views(Text(said), Table(COLUMNS, rows), Text(*(f"skipped {skipped}" for skipped in result.skipped)))


class RegistryPull(HubCommand):
    name = "pull"
    help = (
        "write the cluster of every hub project you see into the registry; other clusters stay as they are, "
        "and the previous file is kept as registry.json.bak.<timestamp>"
    )
    arguments = (
        Arg("--workspace", help="where the harnesses and repos live here (default: each project's, such as ~/github)"),
        Arg("--registry", help="the registry file (default: ~/.claude/harness/registry.json)"),
    )
    output = returns_object("registry", "backup", "clusters", "skipped")

    def run(self, ctx: HubContext) -> Result:
        from evo_agents.hub.registry import pull  # the harness loader, imported only when the command runs

        args = ctx.args
        workspace = Path(args.workspace).expanduser().resolve() if args.workspace else None
        registry = Path(args.registry).expanduser() if args.registry else None
        result = pull(ctx.hub, registry, workspace)
        return Result(result.as_json(), pulled_view(result))


REGISTRY = Group("registry", "the harness registry of this machine", [RegistryPull()])
