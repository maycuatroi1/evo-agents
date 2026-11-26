"""``evo-agents hub sessions``: the transcripts of Claude Code and Codex sessions on the hub (``docs/hub.md``, Session
transcripts).

``sessions backfill`` sends every transcript of this machine whose directory belongs to a hub project and that the
hub does not hold whole yet, the subagents' with them (``evo_agents.hub.transcript_sync``); ``--project`` keeps one
project's, ``--since YYYY-MM-DD`` those changed on that day or later, and ``--dry-run`` only counts what is missing,
saying how many of the sessions missing a transcript ended more than an hour ago. It can run again at any time: a
second run uploads nothing. ``sessions sync`` is what the evo-hub hooks start in the background after a turn; run by
hand it sends what they queued and what changed lately.

``sessions list`` lists the transcripts of a project the caller may read (GET /v1/projects/{p}/transcripts), filtered
by ``--session``, ``--login``, ``--machine``, ``--runtime`` and ``--since``; ``sessions show SESSION`` shows one
session's transcripts with their parts; ``sessions usage`` prints what the project's transcripts come to in the blob
store, per member, with the retention (GET .../transcripts/usage). ``sessions delete SESSION`` deletes the content of
one session's transcripts and ``sessions delete --all`` every transcript of the project, which only an admin of the
project may (DELETE .../transcripts[/{session}]).

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, or the report built here, with the keys declared next to the flag. Standard library only.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Transcripts`
is the gateway to the project's transcripts, and :data:`SESSIONS` their group.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, Views, returns_object
from evo_agents.hub import transcript_sync
from evo_agents.hub.cli.base import EXIT_FAILED, HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.cli.reports import Reported, to_stderr
from evo_agents.hub.transcripts import RUNTIMES

# The keys of the report sync and backfill print with --json (``transcript_sync.Report.as_json``).
REPORT_KEYS = (
    "hub",
    "dry_run",
    "project",
    "since",
    "sessions",
    "transcripts",
    "complete",
    "missing",
    "missing_sessions",
    "missing_ended",
    "uploaded",
    "projects",
    "skipped",
    "errors",
)
USAGE_KEYS = (
    "project",
    "sessions",
    "transcripts",
    "parts",
    "bytes",
    "raw_bytes",
    "source_bytes",
    "blob_bytes",
    "deleted",
    "first_at",
    "last_at",
    "retention_days",
    "members",
)
DELETED_KEYS = ("project", "session_id", "transcripts", "parts", "bytes")
REPORT = returns_object(*REPORT_KEYS)
# The help of --project here, as it was: it names "the directory", not "the current directory".
PROJECT = Arg("--project", help="hub project (default: hub.project in the harness.yaml around the directory)")


class Transcripts(ProjectResource):
    """The transcripts of a project: GET and DELETE /v1/projects/{p}/transcripts and the routes below it (Gateway)."""

    collection = "transcripts"

    def list(self, **filters: Any) -> Any:
        return self.call("GET", **filters)

    def session(self, session: str) -> Any:
        return self.call("GET", session)

    def usage(self) -> Any:
        return self.call("GET", "usage")

    def delete(self, session: str | None) -> Any:
        return self.call("DELETE", *([session] if session else []))


def size_text(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024 or unit == "GiB":
            return f"{count} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return str(count)


def reported(report: transcript_sync.Report | None) -> Result:
    """What sync and backfill show of ``report``: None when another sync held this machine, which says so on stderr."""
    if report is None:
        said = "another sync of transcripts runs on this machine; it takes what the hooks queued"
        return Reported(None, after=to_stderr(said), printed=False)
    errors = to_stderr(*(f"error: {error}" for error in report.errors))  # with --json, the report holds them
    return Result(report.as_json(), Views(Text(report.summary()), errors), EXIT_FAILED if report.errors else 0)


class SessionsBackfill(HubCommand):
    name = "backfill"
    help = (
        "send every transcript of this machine's sessions of hub projects that the hub does not hold whole; a "
        "second run sends nothing"
    )
    arguments = (
        Arg("--project", help="only the sessions of this hub project"),
        Arg("--since", metavar="YYYY-MM-DD", help="only transcripts changed on this day or later"),
        Arg("--dry-run", action="store_true", help="count what is missing, send nothing"),
    )
    output = REPORT

    def check(self, args: argparse.Namespace) -> None:
        try:
            transcript_sync.since_of(args.since)
        except ValueError:
            raise UsageError(f"--since takes a day as YYYY-MM-DD or an ISO time, not {args.since!r}") from None

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        hub, credentials = ctx.session

        def progress(line: str) -> None:
            print(line, file=sys.stderr, flush=True)

        report = transcript_sync.backfill(
            hub,
            credentials.login,
            project=args.project,
            since=args.since,
            dry_run=args.dry_run,
            progress=None if args.json else progress,
        )
        return reported(report)


class SessionsSync(HubCommand):
    name = "sync"
    help = "send what the evo-hub hooks queued and the transcripts changed lately; the hooks run it in the background"
    arguments = (
        Arg(
            "--wait",
            type=int,
            default=0,
            choices=range(0, 301),
            metavar="SECONDS",
            help="wait this long first, 0 to 300",
        ),
    )
    output = REPORT

    def run(self, ctx: HubContext) -> Result:
        hub, credentials = ctx.session
        return reported(transcript_sync.sync(hub, credentials.login, wait=ctx.args.wait))


class SessionsList(HubCommand):
    name = "list"
    help = "the transcripts of a project you may read, the latest updated first"
    arguments = (
        PROJECT,
        Arg("--session", metavar="ID", help="of this session"),
        Arg("--login", help="uploaded by this member"),
        Arg("--machine", help="from this machine"),
        Arg("--runtime", choices=list(RUNTIMES), help="of this runtime"),
        Arg("--since", metavar="TIME", help="updated at or after this ISO time, such as 2026-10-01T00:00Z"),
        Arg("--limit", type=int, default=50, choices=range(1, 501), metavar="N", help="1 to 500"),
        Arg("--offset", type=int, default=0, metavar="N", help="the transcripts to pass over"),
    )
    output = returns_object("transcripts", "total", "limit", "offset", schema="TranscriptList")
    columns = ("SESSION", "FILE", "RUNTIME", "LOGIN", "MACHINE", "LINES", "SIZE", "UPDATED")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Transcripts.of(ctx).list(
            session_id=args.session,
            login=args.login,
            machine=args.machine,
            runtime=args.runtime,
            since=args.since,
            limit=args.limit,
            offset=args.offset or None,
        )
        if args.json:
            return Result(found)
        rows = [self.row(t) for t in found["transcripts"]]
        said = f"{len(rows)} of {found['total']} transcript(s) of {ctx.project}"
        return Result(found, Views(Table(self.columns, rows), Text(said)))

    @staticmethod
    def row(t: dict) -> tuple:
        updated = "deleted" if t["deleted_at"] else when(t["updated_at"])
        return (
            t["session_id"],
            t["file"],
            t["runtime"],
            t["login"],
            t["machine"],
            t["lines"],
            size_text(t["size"]),
            updated,
        )


class SessionsShow(HubCommand):
    name = "show"
    help = "one session's transcripts, with their parts"
    arguments = (Arg("session", metavar="SESSION", help="the session's id"), PROJECT)
    output = returns_object("project", "session_id", "transcripts", schema="SessionTranscripts")

    def run(self, ctx: HubContext) -> Result:
        found = Transcripts.of(ctx).session(ctx.args.session)
        lines = [f"Session {found['session_id']} of {found['project']}:"]
        for t in found["transcripts"]:
            state = f"deleted {when(t['deleted_at'])}" if t["deleted_at"] else f"{len(t['part_list'])} part(s)"
            lines.append(
                f"  {t['file']}: {t['runtime']} on {t['machine']} by {t['login']}, {t['lines']} line(s), "
                f"{size_text(t['size'])} stored, {state}, {when(t['started_at'])} to {when(t['ended_at'])} UTC"
            )
        return Result(found, Text(*lines))


class SessionsUsage(HubCommand):
    name = "usage"
    help = "what a project's transcripts come to in the blob store, per member"
    arguments = (PROJECT,)
    output = returns_object(*USAGE_KEYS, schema="TranscriptUsage")

    def run(self, ctx: HubContext) -> Result:
        found = Transcripts.of(ctx).usage()
        said = (
            f"{found['project']}: {found['sessions']} session(s), {found['transcripts']} transcript(s), "
            f"{found['parts']} part(s), {size_text(found['bytes'])} in the blob store ({size_text(found['raw_bytes'])} "
            f"of redacted lines, from {size_text(found['source_bytes'])} on the machines); {found['deleted']} deleted; "
            f"kept {found['retention_days']} days after their latest part"
        )
        rows = [(m["login"], m["sessions"], m["transcripts"], size_text(m["bytes"])) for m in found["members"]]
        return Result(found, Views(Text(said), Table(("LOGIN", "SESSIONS", "TRANSCRIPTS", "SIZE"), rows)))


class SessionsDelete(HubCommand):
    name = "delete"
    help = "delete the content of a session's transcripts, or with --all of the project's; an admin only"
    arguments = (
        Arg("session", nargs="?", metavar="SESSION", help="the session's id"),
        Arg("--all", action="store_true", help="every transcript of the project"),
        PROJECT,
    )
    output = returns_object(*DELETED_KEYS, schema="TranscriptsDeleted")

    def check(self, args: argparse.Namespace) -> None:
        if (args.session is None) == (not args.all):
            raise UsageError("name one SESSION, or --all for every transcript of the project")

    def run(self, ctx: HubContext) -> Result:
        found = Transcripts.of(ctx).delete(ctx.args.session)
        what = f"session {found['session_id']}" if found["session_id"] else "every session"
        said = (
            f"Deleted the content of {found['transcripts']} transcript(s) of {what} in {found['project']}: "
            f"{found['parts']} part(s), {size_text(found['bytes'])}. Their rows stay, marked deleted, so no backfill "
            "sends them again."
        )
        return Result(found, Text(said))


SESSIONS = Group(
    "sessions",
    "the transcripts of Claude Code and Codex sessions on the hub: backfill, list, usage, delete",
    [SessionsBackfill(), SessionsSync(), SessionsList(), SessionsShow(), SessionsUsage(), SessionsDelete()],
)
