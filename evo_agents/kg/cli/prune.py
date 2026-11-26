"""``evo-agents kg prune``: the retention of a project's built graphs on the hub (``evo-agents hub kg prune``, which a
hub admin runs; ``evo_agents.hub.kg_prune``), for the project of the current directory, ``--project``, or every
project with ``--all``.

By default it only counts: the artifacts of graphs older than the ``--keep`` newest that would leave the bucket.
``--delete`` deletes them. A machine keeps no run logs, graph history or memo to prune: its kg home holds what a sync
needs only.
"""

from __future__ import annotations

import sys

from evo_agents.kg.cli_scrub import project_name
from evo_agents.kg.project import ProjectError


def cmd_prune(args) -> int:
    from evo_agents.hub.cli import kg as kg_cli

    if args.all and args.project:
        print("error: --all goes without --project", file=sys.stderr)
        return 2
    try:
        args.project = project_name(args)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.dry_run, args.graph_files = not args.delete, False
    return kg_cli.KG_PRUNE(args)


def register(ksub) -> None:
    from evo_agents.hub.cli import positive

    prune = ksub.add_parser(
        "prune",
        help="count, and with --delete delete, the artifacts of a project's older graphs on the hub (a hub admin)",
    )
    prune.add_argument("--project", help="project name or harness path (default: from the cwd)")
    prune.add_argument("--all", action="store_true", help="every project on the hub")
    prune.add_argument(
        "--keep",
        type=positive,
        help="newest graphs of each project whose artifact stays (default: the hub's EVO_HUB_KG_KEEP_ARTIFACTS, 3)",
    )
    prune.add_argument("--delete", action="store_true", help="delete what the count finds")
    prune.add_argument("--json", action="store_true", help="machine-readable output")
    prune.set_defaults(func=cmd_prune)
