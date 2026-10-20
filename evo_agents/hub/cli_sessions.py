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
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import quote, urlencode

from evo_agents.hub import transcript_sync
from evo_agents.hub.cli_client import EXIT_FAILED, client_command, print_json, signed_in, table, when
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.plan_cli import project_of_args
from evo_agents.hub.transcripts import RUNTIMES

EXIT_USAGE = 2
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


def _path(project: str, *rest: str) -> str:
    return "/".join([f"/v1/projects/{quote(project, safe='')}/transcripts", *(quote(part, safe="") for part in rest)])


def _size(count: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024 or unit == "GiB":
            return f"{count} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return str(count)


def _report(report: transcript_sync.Report | None, as_json: bool) -> int:
    if report is None:
        print("another sync of transcripts runs on this machine; it takes what the hooks queued", file=sys.stderr)
        return 0
    if as_json:
        print_json(report.as_json())
    else:
        print(report.summary())
        for error in report.errors:
            print(f"error: {error}", file=sys.stderr)
    return EXIT_FAILED if report.errors else 0


@client_command
def cmd_backfill(args) -> int:
    try:
        transcript_sync.since_of(args.since)
    except ValueError:
        print(f"error: --since takes a day as YYYY-MM-DD or an ISO time, not {args.since!r}", file=sys.stderr)
        return EXIT_USAGE
    hub, credentials = signed_in()

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
    return _report(report, args.json)


@client_command
def cmd_sync(args) -> int:
    hub, credentials = signed_in()
    return _report(transcript_sync.sync(hub, credentials.login, wait=args.wait), args.json)


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    filters = {
        "session_id": args.session,
        "login": args.login,
        "machine": args.machine,
        "runtime": args.runtime,
        "since": args.since,
        "limit": args.limit,
        "offset": args.offset or None,
    }
    query = urlencode({key: value for key, value in filters.items() if value is not None})
    found = hub.call("GET", _path(project) + (f"?{query}" if query else ""))
    if args.json:
        print_json(found)
        return 0
    rows = [
        (
            t["session_id"],
            t["file"],
            t["runtime"],
            t["login"],
            t["machine"],
            t["lines"],
            _size(t["size"]),
            "deleted" if t["deleted_at"] else when(t["updated_at"]),
        )
        for t in found["transcripts"]
    ]
    if rows:
        table(("SESSION", "FILE", "RUNTIME", "LOGIN", "MACHINE", "LINES", "SIZE", "UPDATED"), rows)
    print(f"{len(rows)} of {found['total']} transcript(s) of {project}")
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("GET", _path(project, args.session))
    if args.json:
        print_json(found)
        return 0
    print(f"Session {found['session_id']} of {found['project']}:")
    for t in found["transcripts"]:
        state = f"deleted {when(t['deleted_at'])}" if t["deleted_at"] else f"{len(t['part_list'])} part(s)"
        print(
            f"  {t['file']}: {t['runtime']} on {t['machine']} by {t['login']}, {t['lines']} line(s), "
            f"{_size(t['size'])} stored, {state}, {when(t['started_at'])} to {when(t['ended_at'])} UTC"
        )
    return 0


@client_command
def cmd_usage(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("GET", _path(project, "usage"))
    if args.json:
        print_json(found)
        return 0
    print(
        f"{found['project']}: {found['sessions']} session(s), {found['transcripts']} transcript(s), "
        f"{found['parts']} part(s), {_size(found['bytes'])} in the blob store ({_size(found['raw_bytes'])} of "
        f"redacted lines, from {_size(found['source_bytes'])} on the machines); {found['deleted']} deleted; kept "
        f"{found['retention_days']} days after their latest part"
    )
    rows = [(m["login"], m["sessions"], m["transcripts"], _size(m["bytes"])) for m in found["members"]]
    if rows:
        table(("LOGIN", "SESSIONS", "TRANSCRIPTS", "SIZE"), rows)
    return 0


@client_command
def cmd_delete(args) -> int:
    if (args.session is None) == (not args.all):
        print("error: name one SESSION, or --all for every transcript of the project", file=sys.stderr)
        return EXIT_USAGE
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("DELETE", _path(project, *([args.session] if args.session else [])))
    if args.json:
        print_json(found)
        return 0
    what = f"session {found['session_id']}" if found["session_id"] else "every session"
    print(
        f"Deleted the content of {found['transcripts']} transcript(s) of {what} in {found['project']}: "
        f"{found['parts']} part(s), {_size(found['bytes'])}. Their rows stay, marked deleted, so no backfill sends "
        "them again."
    )
    return 0


def _with_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", help="hub project (default: hub.project in the harness.yaml around the directory)")


def register_sessions(hsub) -> None:
    sessions = hsub.add_parser(
        "sessions", help="the transcripts of Claude Code and Codex sessions on the hub: backfill, list, usage, delete"
    )
    ssub = sessions.add_subparsers(dest="sessions_command", required=True)
    report = returns_object(*REPORT_KEYS)

    backfill = ssub.add_parser(
        "backfill",
        help="send every transcript of this machine's sessions of hub projects that the hub does not hold whole; a "
        "second run sends nothing",
    )
    backfill.add_argument("--project", help="only the sessions of this hub project")
    backfill.add_argument("--since", metavar="YYYY-MM-DD", help="only transcripts changed on this day or later")
    backfill.add_argument("--dry-run", action="store_true", help="count what is missing, send nothing")
    json_option(backfill, report)
    backfill.set_defaults(func=cmd_backfill)

    synced = ssub.add_parser(
        "sync",
        help="send what the evo-hub hooks queued and the transcripts changed lately; the hooks run it in the background",
    )
    synced.add_argument(
        "--wait", type=int, default=0, choices=range(0, 301), metavar="SECONDS", help="wait this long first, 0 to 300"
    )
    json_option(synced, report)
    synced.set_defaults(func=cmd_sync)

    listed = ssub.add_parser("list", help="the transcripts of a project you may read, the latest updated first")
    _with_project(listed)
    listed.add_argument("--session", metavar="ID", help="of this session")
    listed.add_argument("--login", help="uploaded by this member")
    listed.add_argument("--machine", help="from this machine")
    listed.add_argument("--runtime", choices=list(RUNTIMES), help="of this runtime")
    listed.add_argument("--since", metavar="TIME", help="updated at or after this ISO time, such as 2026-10-01T00:00Z")
    listed.add_argument("--limit", type=int, default=50, choices=range(1, 501), metavar="N", help="1 to 500")
    listed.add_argument("--offset", type=int, default=0, metavar="N", help="the transcripts to pass over")
    json_option(listed, returns_object("transcripts", "total", "limit", "offset", schema="TranscriptList"))
    listed.set_defaults(func=cmd_list)

    show = ssub.add_parser("show", help="one session's transcripts, with their parts")
    show.add_argument("session", metavar="SESSION", help="the session's id")
    _with_project(show)
    json_option(show, returns_object("project", "session_id", "transcripts", schema="SessionTranscripts"))
    show.set_defaults(func=cmd_show)

    usage = ssub.add_parser("usage", help="what a project's transcripts come to in the blob store, per member")
    _with_project(usage)
    json_option(usage, returns_object(*USAGE_KEYS, schema="TranscriptUsage"))
    usage.set_defaults(func=cmd_usage)

    deleted = ssub.add_parser(
        "delete", help="delete the content of a session's transcripts, or with --all of the project's; an admin only"
    )
    deleted.add_argument("session", nargs="?", metavar="SESSION", help="the session's id")
    deleted.add_argument("--all", action="store_true", help="every transcript of the project")
    _with_project(deleted)
    json_option(deleted, returns_object(*DELETED_KEYS, schema="TranscriptsDeleted"))
    deleted.set_defaults(func=cmd_delete)
