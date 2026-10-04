"""``evo-agents hub memory push|pull|search``: the client commands over ``evo_agents.hub.memory``.

``push`` and ``pull`` take a working directory (default: the current one) or ``--all``, ``--dry-run`` to say what
would change without writing anything here or on the hub, and ``--prune`` to let deletions cross. They print one
summary line and the notes that need a person; ``--quiet`` prints nothing but conflicts and errors, on stderr, for
the hooks that run them at every session. Exit status 1 when a file could not be synced, 0 otherwise, conflicts
included: a conflict is resolved by keeping both versions.
"""

from __future__ import annotations

import sys
from pathlib import Path

from evo_agents.hub.cli_client import EXIT_FAILED, _client_command, _print_json, _signed_in, _table, _when
from evo_agents.hub.client import HubError
from evo_agents.hub.memory import AGENT_SINK, MemorySync, Report, frontmatter, search

COUNTS = {
    "push": (
        ("created", "new"),
        ("updated", "changed"),
        ("moved", "moved"),
        ("deleted", "deleted on the hub"),
        ("conflicts", "conflict(s)"),
        ("unchanged", "unchanged"),
        ("kept_on_hub", "deleted here only"),
        ("held", "held back, see the notes"),
    ),
    "pull": (
        ("pulled", "new"),
        ("updated", "changed"),
        ("restored", "restored"),
        ("deleted", "deleted here"),
        ("conflicts", "conflict(s)"),
        ("unchanged", "unchanged"),
        ("changed_here", "changed here, not pushed yet"),
        ("kept_here", "deleted on the hub only"),
        ("no_directory", "without a directory here"),
        ("indexed", "MEMORY.md line(s) added"),
    ),
}


def _summary(report: Report) -> str:
    counts = [f"{report.counts[key]} {label}" for key, label in COUNTS[report.command] if report.counts.get(key)]
    if report.dry_run:
        verb = "Dry run, nothing written: would " + {"push": "push to", "pull": "pull from"}[report.command]
    else:
        verb = {"push": "Pushed to", "pull": "Pulled from"}[report.command]
    return f"{verb} {report.hub}: " + (", ".join(counts) or "nothing to do") + "."


def _sync(args, command: str) -> int:
    if args.all and args.dir:
        raise HubError("pass a directory or --all, not both")
    hub, credentials = _signed_in()
    sync = MemorySync(hub, credentials.login, sink=args.sink, dry_run=args.dry_run, prune=args.prune)
    target = Path(args.dir) if args.dir else None
    report = sync.push(target, everything=args.all) if command == "push" else sync.pull(target, everything=args.all)
    if args.json:
        _print_json(report.as_json())
    elif not args.quiet:
        print(_summary(report))
        if report.dry_run and report.places:
            _table(("PLACE", "MEMORIES"), sorted(report.places.items()))
        for note in report.notes:
            print(f"note: {note}")
    for conflict in report.conflicts:
        kept = "deleted there" if conflict["deleted_on_hub"] else f"at revision {conflict['hub_revision']}"
        saved = f"this machine's version is saved as {conflict['copy']}" if conflict["copy"] else "nothing was lost"
        print(f"conflict: {conflict['file']}: the hub's version is kept ({kept}); {saved}", file=sys.stderr)
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    return EXIT_FAILED if report.errors else 0


@_client_command
def cmd_memory_push(args) -> int:
    return _sync(args, "push")


@_client_command
def cmd_memory_pull(args) -> int:
    return _sync(args, "pull")


@_client_command
def cmd_memory_search(args) -> int:
    hub, _ = _signed_in()
    found = search(hub, args.query, project=args.project, limit=args.limit, sink=args.sink)
    if args.json:
        _print_json(found)
        return 0
    if not found:
        print(f"No memory you can see matches {args.query!r}.")
        return 0
    rows = []
    for memory in found:
        description = str(frontmatter(memory["body"]).get("description") or "")
        description = " ".join(description.split())
        rows.append(
            (
                memory["project"] or "(personal)",
                memory["location"],
                memory["name"],
                memory["type"],
                _when(memory["updated_at"]),
                description[:60] + ("..." if len(description) > 60 else ""),
            )
        )
    _table(("PROJECT", "LOCATION", "NAME", "TYPE", "UPDATED (UTC)", "DESCRIPTION"), rows)
    return 0


def register_memory(hsub) -> None:
    memory = hsub.add_parser("memory", help="Claude Code's memory files on the hub: push, pull and search them")
    msub = memory.add_subparsers(dest="memory_command", required=True)
    sink_help = f"the sink whose clearance bounds what is read (default: {AGENT_SINK}, which reads the files)"
    for name, func, what in (
        ("push", cmd_memory_push, "send the memory files of a directory to the hub"),
        ("pull", cmd_memory_pull, "bring the hub's memories of a directory here; MEMORY.md gets the lines it lacks"),
    ):
        parser = msub.add_parser(name, help=what)
        parser.add_argument(
            "dir",
            metavar="DIR",
            nargs="?",
            help="a working directory, or its directory under ~/.claude/projects (default: the current directory)",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="every memory directory here" if name == "push" else "every memory you see on the hub",
        )
        parser.add_argument("--dry-run", action="store_true", help="say what would change, write nothing anywhere")
        parser.add_argument(
            "--prune",
            action="store_true",
            help="turn files deleted here into tombstones on the hub"
            if name == "push"
            else "delete the files the hub deleted, when they did not change here",
        )
        parser.add_argument("--quiet", action="store_true", help="print only conflicts and errors, on stderr")
        parser.add_argument("--json", action="store_true", help="machine-readable output")
        parser.add_argument("--sink", default=AGENT_SINK, help=sink_help)
        parser.set_defaults(func=func)

    found = msub.add_parser("search", help="full-text search of the memories you see")
    found.add_argument("query", metavar="QUERY", help='words, "a phrase", or -excluded')
    found.add_argument("--project", help="only the memories of this hub project")
    found.add_argument("--limit", type=int, default=10, choices=range(1, 51), metavar="N", help="1 to 50 (default 10)")
    found.add_argument("--json", action="store_true", help="machine-readable output")
    found.add_argument("--sink", default=AGENT_SINK, help=sink_help)
    found.set_defaults(func=cmd_memory_search)
