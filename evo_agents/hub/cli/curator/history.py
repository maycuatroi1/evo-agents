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

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`History` and
:class:`Transcripts` are the gateways to the routes.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource
from evo_agents.hub.curator import money

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


def query_of(**params) -> str:
    """``?<query>`` of the values given, a list as its key repeated after the others; empty when none is."""
    pairs = [(key, value) for key, value in params.items() if value is not None and not isinstance(value, list)]
    pairs += [(key, item) for key, value in params.items() if isinstance(value, list) for item in value]
    return f"?{urlencode(pairs)}" if pairs else ""


class History(ProjectResource):
    """The history of a project: GET /v1/projects/{p}/curator/history and the routes below it."""

    collection = "curator"

    def status(self) -> dict:
        return self.call("GET", "history")

    def clusters(self, limit: int) -> dict:
        return self.call("GET", "history", "clusters", limit=limit)

    def read(self, what: str, **params) -> Any:
        """``what`` (sessions or search) of the whole history, by ``params``."""
        return self.hub.call("GET", self.path("history", what) + query_of(**params))


class Transcripts(ProjectResource):
    """The transcripts of a project's sessions: GET /v1/projects/{p}/transcripts/{session}/lines."""

    collection = "transcripts"

    def lines(self, session: str, **params) -> Any:
        return self.hub.call("GET", self.path(session, "lines") + query_of(**params))


def when_of(value: str | None) -> str:
    return (value or "-")[:16].replace("T", " ")


FILTERS = (  # the filters ``sessions`` and ``search`` share: by day, by member, by runtime
    Arg("--since", metavar="YYYY-MM-DD", help="from this UTC day on"),
    Arg("--until", metavar="YYYY-MM-DD", help="before this UTC day"),
    Arg("--login", metavar="LOGIN", help="of this member"),
    Arg("--runtime", choices=RUNTIMES),
)


def status_lines(found: dict, project: str) -> list[str]:
    """What ``history status`` says above its table of runs: the cap and model, then the facets of the model."""
    cap = found["budget_usd"]
    if cap is None:
        lines = [f"Project {project} has no charter: no history run"]
    else:
        model = found["model"] or "the cheapest the worker on duty reports"
        spend = f"at most {money(cap)} a night" if cap else "none: history.budget_usd is 0"
        lines = [f"History run of {project}: {spend}, on {found['runtime']} with {model}"]
    llm = found["llm"]
    lines.append(
        f"Facets of the model: {llm['analyzed']} of {llm['items']} sessions and runs, {llm['waiting']} waiting, "
        f"{llm['failed']} whose last attempt failed (version {llm['version']})"
    )
    return lines


class HistoryStatus(HubCommand):
    name = "status"
    help = "its cap and model, the facets of the model, its latest runs"
    arguments = (PROJECT,)
    output = returns_object(*STATUS_KEYS, schema="HistoryStatus")
    columns = ("RUN", "NIGHT", "STATE", "MODEL", "COST", "FACETS", "FAILED", "NAMED")

    def run(self, ctx: HubContext) -> Result:
        found = History.of(ctx).status()
        rows = [self.row(item) for item in found["runs"]]
        said = f"{found['clusters']} cluster(s) in the night's figures, {found['hidden']} smaller left out"
        return Result(found, Views(Text(*status_lines(found, ctx.project)), Table(self.columns, rows), Text(said)))

    @staticmethod
    def row(item: dict) -> tuple:
        return (
            f"#{item['id']}",
            item["night"] or "-",
            item["state"],
            item["model"] or "-",
            money(item["cost_usd"]) if item["cost_usd"] is not None else "-",
            str(item["analyzed"]),
            str(item["failed"]),
            str(item["named"]),
        )


class HistoryClusters(HubCommand):
    name = "clusters"
    help = "the clusters of 3 sessions or more, as the night's figures hold them"
    arguments = (
        PROJECT,
        Arg("--limit", type=int, default=50, choices=range(1, 201), metavar="N", help="1 to 200"),
    )
    output = returns_object(*CLUSTER_LIST_KEYS, schema="FacetClusterList")
    columns = ("CLUSTER", "SESSIONS", "RUNS", "MEMBERS", "MODE", "REPRESENTATIVE")

    def run(self, ctx: HubContext) -> Result:
        found = History.of(ctx).clusters(ctx.args.limit)
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
        said = (
            f"{len(rows)} cluster(s) of {found['min_sessions']} sessions or more in {ctx.project}, {found['hidden']} "
            "smaller left out"
        )
        return Result(found, Views(Table(self.columns, rows), Text(said)))


class HistorySessions(HubCommand):
    name = "sessions"
    help = "the sessions and runs of the whole history, with their numbers"
    arguments = (
        PROJECT,
        Arg("--kind", choices=KINDS),
        *FILTERS,
        Arg("--outcome", metavar="OUTCOME", help="achieved, partial, failed, abandoned, unclear, or a state"),
        Arg("--cluster", metavar="KEY", help="a cluster's key, such as fix/FM-3.2"),
        Arg("--limit", type=int, default=20, choices=range(1, 101), metavar="N", help="1 to 100"),
        Arg("--offset", type=int, default=0, metavar="N"),
    )
    output = returns_object(*SESSIONS_KEYS, schema="HistorySessions")
    columns = ("ITEM", "MEMBER", "RUNTIME", "AT", "MESSAGES", "ERRORS", "OUTCOME", "CLUSTER")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = History.of(ctx).read(
            "sessions",
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
        rows = [self.row(item) for item in found["items"]]
        said = f"{len(rows)} of {found['total']} sessions and runs of {ctx.project}"
        return Result(found, Views(Table(self.columns, rows), Text(said)))

    @staticmethod
    def row(item: dict) -> tuple:
        return (
            item["evidence"],
            item["login"] or "-",
            item["runtime"] or "-",
            when_of(item["at"]),
            str(item["numbers"].get("messages", 0)),
            str(item["numbers"].get("tool_errors", 0)),
            item["outcome"] or (item["state"] or "-"),
            item["cluster"] or "-",
        )


class HistorySearch(HubCommand):
    name = "search"
    help = "words in the transcripts, run logs and group chats, best first"
    arguments = (
        Arg("query", metavar="QUERY", help='words, "a phrase", or -excluded'),
        PROJECT,
        Arg("--source", action="append", choices=SOURCES, help="this source alone; repeat it"),
        *FILTERS,
        Arg("--limit", type=int, default=20, choices=range(1, 51), metavar="N", help="1 to 50"),
        Arg("--offset", type=int, default=0, metavar="N"),
    )
    output = returns_object(*SEARCH_KEYS, schema="HistorySearch")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = History.of(ctx).read(
            "search",
            q=args.query,
            source=args.source or None,
            since=args.since,
            until=args.until,
            login=args.login,
            runtime=args.runtime,
            limit=args.limit,
            offset=args.offset,
        )
        lines = [self.line(hit) for hit in found["hits"]]
        lines.append(f"{len(found['hits'])} of {found['total']} hits of {found['query']!r} in {ctx.project}")
        return Result(found, Text(*lines))

    @staticmethod
    def line(hit: dict) -> str:
        where = hit["evidence"] or f"chat #{hit['chat_id']} message {hit['line']}"
        return f"{where} ({hit['login'] or '-'}, {when_of(hit['at'])}, {hit['role']}): {' '.join(hit['text'].split())}"


class HistoryLines(HubCommand):
    name = "lines"
    help = "the lines of a session's transcript around one line"
    arguments = (
        Arg("session", metavar="SESSION", help="the session's id"),
        Arg("line", type=int, metavar="LINE", help="the line to read around"),
        PROJECT,
        Arg("--file", default="main", help="main (default), or subagents/... for a subagent's"),
        Arg("--before", type=int, default=10, choices=range(0, 101), metavar="N", help="0 to 100"),
        Arg("--after", type=int, default=10, choices=range(0, 101), metavar="N", help="0 to 100"),
    )
    output = returns_object(*LINES_KEYS, schema="TranscriptLines")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Transcripts.of(ctx).lines(
            args.session, line=args.line, file=args.file, before=args.before, after=args.after
        )
        lines = [
            f"Lines {found['first']} to {found['last']} of {found['lines']} of transcript {found['file']} of session "
            f"{found['session_id']} ({found['runtime']}, {found['login']}):"
        ]
        for entry in found["entries"]:
            mark = ">" if entry["line"] == found["asked"] else " "
            lines.append(f"{mark} L{entry['line']} {entry['role']}: {entry['text']}")
        return Result(found, Text(*lines))


HISTORY = Group(
    "history",
    "the night's history run: facets of a cheap model and their clusters",
    (HistoryStatus(), HistoryClusters(), HistorySessions(), HistorySearch(), HistoryLines()),
)
