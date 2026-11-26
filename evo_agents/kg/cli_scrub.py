"""``evo-agents kg scrub``: old copies of secrets of a project on the hub (``evo-agents hub kg scrub``, which a hub
admin runs; ``evo_agents.hub.server.kg_scrub``), for the project of the current directory, ``--project``, or every
project with ``--all``.

By default it only reads: the graph in the hub's tables, and the old graph files and source blobs holding a secret, by
hash, kind and the kinds of secret, never a value. ``--purge`` says what it is about to delete, asks (or takes
``--yes``), and has the hub delete the old ones. A machine keeps no corpus, memo or graph to scrub.
"""

from __future__ import annotations

import sys

from evo_agents.kg.project import ProjectError, resolve_project


def project_name(args) -> str | None:
    """The hub project to act on: None with --all (every project), else --project or the project of the cwd. Raises
    ProjectError."""
    if args.all:
        return None
    name = args.project
    if name and "/" not in name and name not in (".", ".."):
        return name
    return resolve_project(name).name


def cmd_scrub(args) -> int:
    from evo_agents.hub.cli import kg as kg_cli

    if args.all and args.project:
        print("error: --all goes without --project", file=sys.stderr)
        return 2
    try:
        args.project = project_name(args)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return kg_cli.KG_SCRUB(args)


def register(ksub) -> None:
    scrub = ksub.add_parser(
        "scrub", help="find old copies of secrets of a project on the hub, and with --purge delete them (a hub admin)"
    )
    scrub.add_argument("--project", help="project name or harness path (default: from the cwd)")
    scrub.add_argument("--all", action="store_true", help="every project on the hub")
    scrub.add_argument(
        "--purge", action="store_true", help="delete the old ones; the graph in the tables and live blobs stay"
    )
    scrub.add_argument("--yes", action="store_true", help="with --purge: delete without asking")
    scrub.add_argument("--json", action="store_true", help="machine-readable output")
    scrub.set_defaults(func=cmd_scrub)
