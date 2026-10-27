"""``evo-agents hub curator history``: the night's history run, which reads the project's sessions and runs with a
cheap model (``evo_agents.hub.history``), and the project's whole history as the Curator's Reviewer reads it
(``evo_agents.hub.server.history_search``).

``curator history status`` says where it stands (GET /v1/projects/{p}/curator/history): its cap and the model it runs
on, how many of the project's sessions and runs have a facet of the model and how many wait, and the latest history
runs with what each wrote and cost. ``curator history clusters`` lists the clusters the facets fall into, those of 3
sessions or more as the night's figures hold them (GET .../curator/history/clusters): each with its sessions, runs,
members and its representative session, the latest.

``curator history sessions`` lists the sessions and runs of the whole history, the latest first, with the numbers of
their facets, by ``--kind``, ``--since``, ``--until``, ``--login``, ``--runtime``, ``--outcome`` and ``--cluster``
(GET .../curator/history/sessions). ``curator history search QUERY`` finds words in the transcripts, the run logs and
the group chats the caller may read, best first, each with the evidence that names it (GET .../curator/history/search).
``curator history lines SESSION LINE`` prints the lines of a transcript around one (GET
/v1/projects/{p}/transcripts/{session}/lines).

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.curator import money
from evo_agents.hub.plan_cli import project_of_args

STATUS_KEYS = ("project", "budget_usd", "runtime", "model", "llm", "runs", "clusters", "hidden")
CLUSTER_LIST_KEYS = ("project", "min_sessions", "clusters", "hidden", "counted_at")
SESSIONS_KEYS = ("project", "items", "total", "limit", "offset")
SEARCH_KEYS = ("project", "query", "sources", "hits", "total", "limit", "offset", "reranked")
LINES_KEYS = (
    "project",
    "session_id",
    "file",
    "runtime",
    "login",
    "lines",
    "asked",
    "first",
    "last",
    "entries",
    "evidence",
)
SOURCES = ("transcript", "run", "chat")
KINDS = ("session", "run")
RUNTIMES = ("claude-code", "codex")


def _path(project: str, *parts: str) -> str:
    return "/".join((f"/v1/projects/{quote(project, safe='')}/curator/history", *parts))


@client_command
def cmd_status(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("GET", _path(project))
    if args.json:
        print_json(found)
        return 0
    cap = found["budget_usd"]
    if cap is None:
        print(f"Project {project} has no charter: no history run")
    else:
        model = found["model"] or "the cheapest the worker on duty reports"
        spend = f"at most {money(cap)} a night" if cap else "none: history.budget_usd is 0"
        print(f"History run of {project}: {spend}, on {found['runtime']} with {model}")
    llm = found["llm"]
    print(
        f"Facets of the model: {llm['analyzed']} of {llm['items']} sessions and runs, {llm['waiting']} waiting, "
        f"{llm['failed']} whose last attempt failed (version {llm['version']})"
    )
    rows = [
        (
            f"#{item['id']}",
            item["night"] or "-",
            item["state"],
            item["model"] or "-",
            money(item["cost_usd"]) if item["cost_usd"] is not None else "-",
            str(item["analyzed"]),
            str(item["failed"]),
            str(item["named"]),
        )
        for item in found["runs"]
    ]
    if rows:
        table(("RUN", "NIGHT", "STATE", "MODEL", "COST", "FACETS", "FAILED", "NAMED"), rows)
    print(f"{found['clusters']} cluster(s) in the night's figures, {found['hidden']} smaller left out")
    return 0


@client_command
def cmd_clusters(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("GET", _path(project, "clusters") + f"?{urlencode({'limit': args.limit})}")
    if args.json:
        print_json(found)
        return 0
    rows = [
        (
            item["name"],
            str(item["sessions"]),
            str(item["runs"]),
            str(item["members"]),
            item["failure_mode"],
            item["representative"] or "-",
        )
        for item in found["clusters"]
    ]
    if rows:
        table(("CLUSTER", "SESSIONS", "RUNS", "MEMBERS", "MODE", "REPRESENTATIVE"), rows)
    print(
        f"{len(rows)} cluster(s) of {found['min_sessions']} sessions or more in {project}, {found['hidden']} smaller "
        "left out"
    )
    return 0


def _query(**params) -> str:
    pairs = [(key, value) for key, value in params.items() if value is not None and not isinstance(value, list)]
    pairs += [(key, item) for key, value in params.items() if isinstance(value, list) for item in value]
    return f"?{urlencode(pairs)}" if pairs else ""


def _when(value: str | None) -> str:
    return (value or "-")[:16].replace("T", " ")


@client_command
def cmd_sessions(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    asked = _query(
        kind=args.kind,
        since=args.since,
        until=args.until,
        login=args.login,
        runtime=args.runtime,
        outcome=args.outcome,
        cluster=args.cluster,
        limit=args.limit,
        offset=args.offset,
    )
    found = hub.call("GET", _path(project, "sessions") + asked)
    if args.json:
        print_json(found)
        return 0
    rows = [
        (
            item["evidence"],
            item["login"] or "-",
            item["runtime"] or "-",
            _when(item["at"]),
            str(item["numbers"].get("messages", 0)),
            str(item["numbers"].get("tool_errors", 0)),
            item["outcome"] or (item["state"] or "-"),
            item["cluster"] or "-",
        )
        for item in found["items"]
    ]
    if rows:
        table(("ITEM", "MEMBER", "RUNTIME", "AT", "MESSAGES", "ERRORS", "OUTCOME", "CLUSTER"), rows)
    print(f"{len(rows)} of {found['total']} sessions and runs of {project}")
    return 0


@client_command
def cmd_search(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    asked = _query(
        q=args.query,
        source=args.source or None,
        since=args.since,
        until=args.until,
        login=args.login,
        runtime=args.runtime,
        limit=args.limit,
        offset=args.offset,
    )
    found = hub.call("GET", _path(project, "search") + asked)
    if args.json:
        print_json(found)
        return 0
    for hit in found["hits"]:
        where = hit["evidence"] or f"chat #{hit['chat_id']} message {hit['line']}"
        print(f"{where} ({hit['login'] or '-'}, {_when(hit['at'])}, {hit['role']}): {' '.join(hit['text'].split())}")
    print(f"{len(found['hits'])} of {found['total']} hits of {found['query']!r} in {project}")
    return 0


@client_command
def cmd_lines(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    asked = _query(line=args.line, file=args.file, before=args.before, after=args.after)
    path = f"/v1/projects/{quote(project, safe='')}/transcripts/{quote(args.session, safe='')}/lines"
    found = hub.call("GET", path + asked)
    if args.json:
        print_json(found)
        return 0
    print(
        f"Lines {found['first']} to {found['last']} of {found['lines']} of transcript {found['file']} of session "
        f"{found['session_id']} ({found['runtime']}, {found['login']}):"
    )
    for entry in found["entries"]:
        mark = ">" if entry["line"] == found["asked"] else " "
        print(f"{mark} L{entry['line']} {entry['role']}: {entry['text']}")
    return 0


def _filters(parser, *, runtime: bool = True) -> None:
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="from this UTC day on")
    parser.add_argument("--until", metavar="YYYY-MM-DD", help="before this UTC day")
    parser.add_argument("--login", metavar="LOGIN", help="of this member")
    if runtime:
        parser.add_argument("--runtime", choices=RUNTIMES)


def _register_reading(hsub, with_project) -> None:
    sessions = hsub.add_parser("sessions", help="the sessions and runs of the whole history, with their numbers")
    with_project(sessions)
    sessions.add_argument("--kind", choices=KINDS)
    _filters(sessions)
    sessions.add_argument(
        "--outcome", metavar="OUTCOME", help="achieved, partial, failed, abandoned, unclear, or a state"
    )
    sessions.add_argument("--cluster", metavar="KEY", help="a cluster's key, such as fix/FM-3.2")
    sessions.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="N", help="1 to 100")
    sessions.add_argument("--offset", type=int, default=0, metavar="N")
    json_option(sessions, returns_object(*SESSIONS_KEYS, schema="HistorySessions"))
    sessions.set_defaults(func=cmd_sessions)
    search = hsub.add_parser("search", help="words in the transcripts, run logs and group chats, best first")
    search.add_argument("query", metavar="QUERY", help='words, "a phrase", or -excluded')
    with_project(search)
    search.add_argument("--source", action="append", choices=SOURCES, help="this source alone; repeat it")
    _filters(search)
    search.add_argument("--limit", type=int, default=20, choices=range(1, 51), metavar="N", help="1 to 50")
    search.add_argument("--offset", type=int, default=0, metavar="N")
    json_option(search, returns_object(*SEARCH_KEYS, schema="HistorySearch"))
    search.set_defaults(func=cmd_search)
    lines = hsub.add_parser("lines", help="the lines of a session's transcript around one line")
    lines.add_argument("session", metavar="SESSION", help="the session's id")
    lines.add_argument("line", type=int, metavar="LINE", help="the line to read around")
    with_project(lines)
    lines.add_argument("--file", default="main", help="main (default), or subagents/... for a subagent's")
    lines.add_argument("--before", type=int, default=10, choices=range(0, 101), metavar="N", help="0 to 100")
    lines.add_argument("--after", type=int, default=10, choices=range(0, 101), metavar="N", help="0 to 100")
    json_option(lines, returns_object(*LINES_KEYS, schema="TranscriptLines"))
    lines.set_defaults(func=cmd_lines)


def register_history(csub, with_project) -> None:
    """``curator history`` and its commands."""
    parser = csub.add_parser("history", help="the night's history run: facets of a cheap model and their clusters")
    hsub = parser.add_subparsers(dest="history_command", required=True)
    status = hsub.add_parser("status", help="its cap and model, the facets of the model, its latest runs")
    with_project(status)
    json_option(status, returns_object(*STATUS_KEYS, schema="HistoryStatus"))
    status.set_defaults(func=cmd_status)
    clusters = hsub.add_parser("clusters", help="the clusters of 3 sessions or more, as the night's figures hold them")
    with_project(clusters)
    clusters.add_argument("--limit", type=int, default=50, choices=range(1, 201), metavar="N", help="1 to 200")
    json_option(clusters, returns_object(*CLUSTER_LIST_KEYS, schema="FacetClusterList"))
    clusters.set_defaults(func=cmd_clusters)
    _register_reading(hsub, with_project)
