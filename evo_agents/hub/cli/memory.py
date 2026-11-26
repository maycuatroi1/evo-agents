"""``evo-agents hub memory push|pull|search|list|restore``: the client commands over ``evo_agents.hub.memory``.

``push`` and ``pull`` take a working directory (default: the current one) or ``--all``, ``--dry-run`` to say what
would change without writing anything here or on the hub, and ``--prune`` to let deletions cross. They print one
summary line and the notes that need a person; ``--quiet`` prints nothing but conflicts and errors, on stderr, for
the hooks that run them at every session. ``list --agent H`` shows the memories of agent profile H, which no pull
brings here, and ``restore ID`` brings back one of those the hub archived, nobody having opened it for 30 days.
Exit status 1 when a file could not be synced, 0 otherwise, conflicts
included: a conflict is resolved by keeping both versions. Run inside a Claude Code session (CLAUDE_CODE_SESSION_ID),
``push`` sends the audience of the session's label with each file, as the Stop hook does (``evo_agents.hub.hooks``).

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Memories` is
the gateway to /v1/memories, and :data:`MEMORY` their group.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_array, returns_object
from evo_agents.hub.cli.base import EXIT_FAILED, HubCommand, HubContext, HubRoutes, when
from evo_agents.hub.cli.reports import Reported, to_stderr
from evo_agents.hub.client import HubError
from evo_agents.hub.memory import AGENT_SINK, MemorySync, Report, well_formed
from evo_agents.hub.memory_files import frontmatter

# What push, pull and search print with --json (the contract, `evo-agents hub contract print`).
SYNC_OUTPUT = returns_object("command", "hub", "dry_run", "counts", "places", "conflicts", "errors", "notes")
MEMORY_KEYS = (
    "id",
    "scope",
    "project",
    "location",
    "name",
    "type",
    "owner",
    "agent",
    "label",
    "body",
    "revision",
    "deleted",
    "created_at",
    "updated_at",
    "updated_by",
    "read_at",
    "archived_at",
    "stale_at",
    "promoted_at",
    "promoted_into",
)

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
        ("unindexed", "MEMORY.md line(s) removed"),
    ),
}
SINK = Arg(
    "--sink",
    default=AGENT_SINK,
    help=f"the sink whose clearance bounds what is read (default: {AGENT_SINK}, which reads the files)",
)
ONE_PROJECT = Arg("--project", help="only the memories of this hub project")


class Memories(HubRoutes):
    """GET /v1/memories and /v1/memories/search, and POST /v1/memories/{id}/restore (Gateway)."""

    path = "/v1/memories"

    def items(self, tail: str, what: str) -> list:
        """The memories the hub answers ``GET <path><tail>`` with; HubError when it answered something else."""
        found = self.call("GET", tail)
        items = found.get("items") if isinstance(found, dict) else None
        if not isinstance(items, list) or not all(well_formed(item) for item in items):
            raise HubError(f"the hub at {self.hub.url} answered {what} this client cannot read")
        return items

    def search(self, query: str, *, project: str | None = None, limit: int = 10, sink: str = AGENT_SINK) -> list:
        """The memories one sees whose name or text matches ``query``, best first."""
        params = {"q": query, "limit": limit, "sink": sink, **({"project": project} if project else {})}
        return self.items("/search?" + urlencode(params), "a search")

    def listed(self, *, project: str | None = None, agent: str | None = None, limit: int = 100) -> list:
        """The memories one sees, oldest change first: those of agent profile ``agent`` when named, else of no
        agent."""
        params = {"limit": limit, **({"project": project} if project else {}), **({"agent": agent} if agent else {})}
        return self.items("?" + urlencode(params), "a listing")

    def restore(self, memory: int) -> Any:
        return self.call("POST", f"/{memory}/restore")


def session_audience(payload: dict | None = None) -> list[str]:
    """The audience of the label of the Claude Code session ``payload`` (a hook's) or the environment names, which a
    push sends with each file; HubError when the label cannot be read, so nothing leaves without it."""
    from evo_agents.kg.sessions import current_session_id
    from evo_agents.kg.sessions import session_audience as recorded

    try:
        return recorded(current_session_id(payload))
    except ValueError as exc:
        raise HubError(
            f"{exc}: memories were not pushed, since they would leave without the audience it read"
        ) from None


def summary(report: Report) -> str:
    counts = [f"{report.counts[key]} {label}" for key, label in COUNTS[report.command] if report.counts.get(key)]
    if report.dry_run:
        verb = "Dry run, nothing written: would " + {"push": "push to", "pull": "pull from"}[report.command]
    else:
        verb = {"push": "Pushed to", "pull": "Pulled from"}[report.command]
    return f"{verb} {report.hub}: " + (", ".join(counts) or "nothing to do") + "."


def report_view(report: Report) -> Views:
    places = sorted(report.places.items()) if report.dry_run else []
    notes = [f"note: {note}" for note in report.notes]
    return Views(Text(summary(report)), Table(("PLACE", "MEMORIES"), places), Text(*notes))


def trouble_lines(report: Report) -> list[str]:
    """The conflicts and errors of ``report``, which stderr gets whatever else is printed."""
    lines = []
    for conflict in report.conflicts:
        kept = "deleted there" if conflict["deleted_on_hub"] else f"at revision {conflict['hub_revision']}"
        saved = f"this machine's version is saved as {conflict['copy']}" if conflict["copy"] else "nothing was lost"
        lines.append(f"conflict: {conflict['file']}: the hub's version is kept ({kept}); {saved}")
    return lines + [f"error: {error}" for error in report.errors]


def sync_arguments(parser: argparse.ArgumentParser, all_help: str, prune_help: str) -> None:
    """The arguments of push and pull, which differ by the help of ``--all`` and ``--prune``, up to ``--json``."""
    parser.add_argument(
        "dir",
        metavar="DIR",
        nargs="?",
        help="a working directory, or its directory under ~/.claude/projects (default: the current directory)",
    )
    parser.add_argument("--all", action="store_true", help=all_help)
    parser.add_argument("--dry-run", action="store_true", help="say what would change, write nothing anywhere")
    parser.add_argument("--prune", action="store_true", help=prune_help)
    parser.add_argument("--quiet", action="store_true", help="print only conflicts and errors, on stderr")


def synced(ctx: HubContext, command: str) -> Result:
    """What ``push`` or ``pull`` (``command``) did, its conflicts and errors written to stderr with --json too."""
    args = ctx.args
    if args.all and args.dir:
        raise HubError("pass a directory or --all, not both")
    hub, credentials = ctx.session
    audience = session_audience() if command == "push" else []
    sync = MemorySync(hub, credentials.login, sink=args.sink, dry_run=args.dry_run, prune=args.prune, audience=audience)
    target = Path(args.dir) if args.dir else None
    report = sync.push(target, everything=args.all) if command == "push" else sync.pull(target, everything=args.all)
    view = Views() if args.quiet else report_view(report)
    return Reported(report.as_json(), view, EXIT_FAILED if report.errors else 0, to_stderr(*trouble_lines(report)))


class MemoryPush(HubCommand):
    name = "push"
    help = "send the memory files of a directory to the hub"
    output = SYNC_OUTPUT

    def configure(self, parser: argparse.ArgumentParser) -> None:
        sync_arguments(parser, "every memory directory here", "turn files deleted here into tombstones on the hub")
        super().configure(parser)
        SINK.add_to(parser)  # after --json, where it always was

    def run(self, ctx: HubContext) -> Result:
        return synced(ctx, "push")


class MemoryPull(HubCommand):
    name = "pull"
    help = "bring the hub's memories of a directory here; MEMORY.md gets the lines it lacks"
    output = SYNC_OUTPUT

    def configure(self, parser: argparse.ArgumentParser) -> None:
        sync_arguments(
            parser, "every memory you see on the hub", "delete the files the hub deleted, when they did not change here"
        )
        super().configure(parser)
        SINK.add_to(parser)  # after --json, where it always was

    def run(self, ctx: HubContext) -> Result:
        return synced(ctx, "pull")


class MemorySearch(HubCommand):
    name = "search"
    help = "full-text search of the memories you see"
    arguments = (
        Arg("query", metavar="QUERY", help='words, "a phrase", or -excluded'),
        ONE_PROJECT,
        Arg("--limit", type=int, default=10, choices=range(1, 51), metavar="N", help="1 to 50 (default 10)"),
    )
    output = returns_array(*MEMORY_KEYS, schema="Memory")
    columns = ("PROJECT", "LOCATION", "NAME", "TYPE", "UPDATED (UTC)", "DESCRIPTION")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        super().configure(parser)
        SINK.add_to(parser)  # after --json, where it always was

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Memories(ctx.hub).search(args.query, project=args.project, limit=args.limit, sink=args.sink)
        empty = f"No memory you can see matches {args.query!r}."
        return Result(found, Table(self.columns, [self.row(memory) for memory in found], empty))

    @staticmethod
    def row(memory: dict) -> tuple:
        description = " ".join(str(frontmatter(memory["body"]).get("description") or "").split())
        return (
            memory["project"] or "(personal)",
            memory["location"],
            memory["name"],
            memory["type"],
            when(memory["updated_at"]),
            description[:60] + ("..." if len(description) > 60 else ""),
        )


class MemoryList(HubCommand):
    name = "list"
    help = "list the memories you see, or those of one agent profile"
    arguments = (
        ONE_PROJECT,
        Arg("--agent", metavar="HANDLE", help="only the memories of this agent profile (no pull brings them)"),
        Arg("--limit", type=int, default=100, choices=range(1, 501), metavar="N", help="1 to 500 (default 100)"),
    )
    output = returns_array(*MEMORY_KEYS, schema="Memory")
    columns = ("ID", "PROJECT", "LOCATION", "NAME", "TYPE", "AGENT", "UPDATED (UTC)")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        agent = args.agent.lstrip("@") if args.agent else None
        found = Memories(ctx.hub).listed(project=args.project, agent=agent, limit=args.limit)
        empty = "No memory you can see" + (f" of agent {args.agent}." if args.agent else ".")
        return Result(found, Table(self.columns, [self.row(memory) for memory in found], empty))

    @staticmethod
    def row(m: dict) -> tuple:
        agent = "@" + m["agent"] if m.get("agent") else "-"
        return (
            m["id"],
            m["project"] or "(personal)",
            m["location"],
            m["name"],
            m["type"],
            agent,
            when(m["updated_at"]),
        )


class MemoryRestore(HubCommand):
    name = "restore"
    help = "bring back an agent's memory the hub archived, as a writer of its project"
    arguments = (Arg("id", metavar="ID", type=int, help="the memory's id (`hub memory list --agent`)"),)
    output = returns_object(*MEMORY_KEYS, schema="Memory")

    def run(self, ctx: HubContext) -> Result:
        memory = Memories(ctx.hub).restore(ctx.args.id)
        said = (
            f"Memory {memory['id']} ({memory['name']}) of @{memory.get('agent')} is active: back in the index and the "
            "searches of its agent's runs."
        )
        return Result(memory, Text(said))


MEMORY = Group(
    "memory",
    "Claude Code's memory files on the hub: push, pull and search them",
    [MemoryPush(), MemoryPull(), MemorySearch(), MemoryList(), MemoryRestore()],
)
