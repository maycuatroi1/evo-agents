"""The sessions of the night's figures, counted from the facets (``evo_agents.hub.facet_job``): what
``evo_agents.hub.server.collect`` puts under ``tools`` (the sessions' part), ``programs``, ``environment`` (the
sessions' part), ``repeated_commands``, ``corrections`` and ``sessions``, and the weekly series ``weekly``.

The sessions of a span are, the latest first, at most MAX_SESSIONS of:

- the facet of each session of the project whose moment (``session_facets.at``: the last line of its transcripts, or
  when its digest was pushed) falls in the span, when the label of its transcripts or digest lets the reader read it
  through the project's hub sink;
- the digest of each session pushed in the span that has no facet yet (the job had no turn since), read as its facet
  would be (``facets.digest_facet``), with the same rule;
- the facet of each local run (a session on a member's machine the hub watches) that ended in the span, only when its
  member is ``owner_id``, the owner of the charter, and only when the hub holds neither a facet nor a digest of the
  same session, which already count it.

An entry of a session names its evidence ``session:ID:FIELD:INDEX``, the entry INDEX of the list FIELD of the session's
facet (``evo_agents.hub.server.proposals`` resolves it there, else in its digest); an entry of a local run names
``run:ID:SEQ``, the event it first came in.

``weekly`` adds up the numbers of every facet the reader may read until the end of the span (``facets.summary``):
sessions as above, and the runs that ended, of a plan the reader may read or a review run, a local run of the owner as
above. A week starts on Monday, in UTC; the latest week comes first.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import facets, review, tables
from evo_agents.hub.server.projects import ProjectAccess

MAX_SESSIONS = 1000  # sessions one count reads, the latest first
MAX_WEEKS = 520
TOP = 30
SAMPLES = 5
SAMPLE_CHARS = 200
LISTS = ("tools", "bash", "errors", "repeated_commands", "commands", "command_at", "user_turns")
WEEK_SUMS = (
    "messages",
    "tool_calls",
    "tool_errors",
    "environment_errors",
    "corrections",
    "kg_calls",
    "kg_empty",
    "compactions",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "hook_ms",
)


def cut(text, limit: int = SAMPLE_CHARS) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def rate(errors: int, calls: int) -> float:
    return round(errors / calls, 4) if calls else 0.0


def _list(value) -> list:
    return value if isinstance(value, list) else []


@dataclass
class Cause:
    """The failures of one environment cause: how many, and a few of the sessions, runs, evidence and texts."""

    count: int = 0
    sessions: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    samples: list = field(default_factory=list)

    def add(self, count: int, *, session: str | None = None, run: int | None = None, evidence: str, sample: str):
        self.count += count
        for listed, value in ((self.sessions, session), (self.runs, run), (self.evidence, evidence)):
            if value is not None and value not in listed and len(listed) < SAMPLES:
                listed.append(value)
        if sample and sample not in self.samples and len(self.samples) < 3:
            self.samples.append(sample)


@dataclass
class Session:
    """One session of the figures: the lists of its facet, and whose evidence they are."""

    source: str  # transcript, digest or local
    at: datetime
    key: str
    session: str | None
    run: int | None
    messages: int
    model: str | None
    lists: dict
    numbers: dict | None = None  # the numbers of a digest that has no facet yet (facets.summary), for the weekly series

    def evidence(self, name: str, index: int, item=None) -> str:
        if self.run is None:
            return f"session:{self.session}:{name}:{index}"
        where = item.get("at") if isinstance(item, dict) else None
        if name == "commands":
            listed = _list(self.lists.get("command_at"))
            where = listed[index] if index < len(listed) else None
        seq = where if isinstance(where, int) and not isinstance(where, bool) and where >= 1 else 1
        return f"run:{self.run}:{seq}"


# The sessions of a span


def _fields(body):
    return [body[name].label(name) for name in LISTS]


def _session_facets(project_id: int, since: datetime, until: datetime):
    f = tables.session_facets
    return (
        select(f.c.session_id, f.c.source, f.c.label, f.c.at, f.c.body["messages"].label("messages"))
        .add_columns(f.c.body["model"].label("model"), *_fields(f.c.body))
        .where(f.c.project_id == project_id, f.c.kind == "session", f.c.body.is_not(None))
        .where(f.c.at >= since, f.c.at < until)
        .order_by(f.c.at.desc(), f.c.session_id)
        .limit(MAX_SESSIONS)
    )


def _made(f, project_id, session_id):
    return exists().where(
        f.c.kind == "session", f.c.project_id == project_id, f.c.session_id == session_id, f.c.body.is_not(None)
    )


def _waiting_digests(project_id: int, since: datetime, until: datetime):
    d, f = tables.session_digests, tables.session_facets
    return (
        select(d.c.session_id, d.c.label, d.c.updated_at.label("at"), d.c.body)
        .where(d.c.project_id == project_id, d.c.updated_at >= since, d.c.updated_at < until)
        .where(~_made(f, d.c.project_id, d.c.session_id))
        .order_by(d.c.updated_at.desc(), d.c.session_id)
        .limit(MAX_SESSIONS)
    )


def _counted_elsewhere(f):
    """Whether the hub holds a facet or a digest of the session of local run facet ``f``."""
    other, d = tables.session_facets.alias("other"), tables.session_digests
    held = exists().where(
        other.c.kind == "session",
        other.c.project_id == f.c.project_id,
        other.c.session_id == f.c.session_id,
        other.c.body.is_not(None),
    )
    digested = exists().where(d.c.project_id == f.c.project_id, d.c.session_id == f.c.session_id)
    return held | digested


def _local_runs(project_id: int, owner_id: int, since: datetime, until: datetime):
    f, r = tables.session_facets, tables.runs
    return (
        select(f.c.run_id, f.c.session_id, f.c.at, f.c.body["messages"].label("messages"))
        .add_columns(f.c.body["model"].label("model"), *_fields(f.c.body))
        .join_from(f, r, r.c.id == f.c.run_id)
        .where(f.c.project_id == project_id, f.c.kind == "run", r.c.kind == "local", f.c.user_id == owner_id)
        .where(f.c.body.is_not(None), f.c.at >= since, f.c.at < until, ~_counted_elsewhere(f))
        .order_by(f.c.at.desc(), f.c.run_id.desc())
        .limit(MAX_SESSIONS)
    )


def _lists(row) -> dict:
    return {name: getattr(row, name) for name in LISTS}


async def sessions_of(
    conn: AsyncConnection, access: ProjectAccess, since: datetime, until: datetime, owner_id: int | None
) -> list[Session]:
    """The sessions of [since, until) as the module says, the latest first."""
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    found: list[Session] = []
    if through is not None:
        for row in await conn.execute(_session_facets(access.project_id, since, until)):
            if access.visible(row.label, through):
                session = Session(row.source, row.at, row.session_id, row.session_id, None, 0, row.model, _lists(row))
                session.messages = facets.count(row.messages) or 0
                found.append(session)
        for row in await conn.execute(_waiting_digests(access.project_id, since, until)):
            if access.visible(row.label, through):
                body = facets.digest_facet(row.body)
                lists = {name: body.get(name) for name in LISTS}
                messages = facets.count(body.get("messages")) or 0
                session = Session(
                    "digest", row.at, row.session_id, row.session_id, None, messages, body.get("model"), lists
                )
                session.numbers = facets.summary(body)
                found.append(session)
    if owner_id is not None:
        for row in await conn.execute(_local_runs(access.project_id, owner_id, since, until)):
            messages = facets.count(row.messages) or 0
            found.append(
                Session("local", row.at, f"run:{row.run_id}", None, row.run_id, messages, row.model, _lists(row))
            )
    found.sort(key=lambda item: (-item.at.timestamp(), item.key))
    return found[:MAX_SESSIONS]


async def lists_of(conn: AsyncConnection, project_id: int, session_id: str) -> tuple[str, dict] | None:
    """Where the entries of session ``session_id`` that evidence names are: ("facet", its facet) when the hub made one,
    else ("digest", its digest); None when the project holds neither."""
    d, f = tables.session_digests, tables.session_facets
    made = select(f.c.body).where(f.c.kind == "session", f.c.project_id == project_id, f.c.session_id == session_id)
    body = (await conn.execute(made.where(f.c.body.is_not(None)))).scalar_one_or_none()
    if body is not None:
        return "facet", body
    pushed = select(d.c.body).where(d.c.project_id == project_id, d.c.session_id == session_id)
    body = (await conn.execute(pushed)).scalar_one_or_none()
    return None if body is None else ("digest", body)


# The figures of the sessions


class _Counts:
    """What the sessions add up to, one session at a time."""

    def __init__(self):
        self.tools: dict = defaultdict(Counter)
        self.programs: dict = defaultdict(Counter)
        self.environment: dict[str, Cause] = defaultdict(Cause)
        self.repeated: dict[str, dict] = {}
        self.corrections: list = []
        self.models: Counter = Counter()
        self.sources: Counter = Counter()
        self.messages = 0

    def add(self, session: Session) -> None:
        self.messages += session.messages
        self.sources[session.source] += 1
        if session.model:
            self.models[session.model] += 1
        self._tools(session)
        self._environment(session)
        self._repeated(session)
        for index, turn in enumerate(_list(session.lists.get("user_turns"))):
            if isinstance(turn, str) and review.is_correction(turn) and len(self.corrections) < TOP:
                evidence = session.evidence("user_turns", index)
                self.corrections.append(
                    {"session": session.session or session.key, "text": cut(turn), "evidence": evidence}
                )

    def _tools(self, session: Session) -> None:
        for item in _list(session.lists.get("tools")):
            if isinstance(item, dict) and item.get("gen_ai.tool.name"):
                found = self.tools[item["gen_ai.tool.name"]]
                found["calls"] += int(item.get("calls") or 0)
                found["errors"] += int(item.get("errors") or 0)
                found["sessions"] += 1
        for item in _list(session.lists.get("bash")):
            if isinstance(item, dict) and item.get("program"):
                found = self.programs[item["program"]]
                found["calls"] += int(item.get("calls") or 0)
                found["errors"] += int(item.get("errors") or 0)

    def _environment(self, session: Session) -> None:
        for index, item in enumerate(_list(session.lists.get("errors"))):
            if not isinstance(item, dict):
                continue
            cause = review.environment_cause(str(item.get("text") or ""))
            if cause is not None:
                self.environment[cause].add(
                    int(item.get("n") or 1),
                    session=session.session,
                    run=session.run,
                    evidence=session.evidence("errors", index, item),
                    sample=cut(item.get("text")),
                )

    def _entry(self, key: str) -> dict:
        return self.repeated.setdefault(key, {"command": cut(key), "times": 0, "sessions": 0, "evidence": []})

    def _repeated(self, session: Session) -> None:
        seen = set()
        for index, item in enumerate(_list(session.lists.get("repeated_commands"))):
            if isinstance(item, dict) and item.get("command"):
                key = review.normalize_command(item["command"])
                entry = self._entry(key)
                entry["times"] += int(item.get("n") or 0)
                if key not in seen:
                    entry["sessions"] += 1
                    seen.add(key)
                    if len(entry["evidence"]) < SAMPLES:
                        entry["evidence"].append(session.evidence("repeated_commands", index, item))
        for index, command in enumerate(_list(session.lists.get("commands"))):
            key = review.normalize_command(str(command))
            if key in seen or not key:
                continue
            seen.add(key)
            entry = self._entry(key)
            entry["times"] += 1
            entry["sessions"] += 1
            if len(entry["evidence"]) < SAMPLES:
                entry["evidence"].append(session.evidence("commands", index))


def session_figures(sessions: list[Session]) -> dict:
    """tools, programs, environment (the sessions' part, by cause, as Cause), repeated_commands, corrections and
    sessions, from ``sessions``."""
    counts = _Counts()
    for session in sessions:
        counts.add(session)
    tools = [
        {
            "tool": name,
            "source": "sessions",
            "calls": found["calls"],
            "errors": found["errors"],
            "error_rate": rate(found["errors"], found["calls"]),
            "sessions": found["sessions"],
        }
        for name, found in counts.tools.items()
    ]
    programs = [
        {
            "program": name,
            "calls": found["calls"],
            "errors": found["errors"],
            "error_rate": rate(found["errors"], found["calls"]),
        }
        for name, found in counts.programs.items()
        if found["errors"]
    ]
    repeated = [entry for entry in counts.repeated.values() if entry["sessions"] >= 2 or entry["times"] >= 3]
    return {
        "tools": tools,
        "programs": sorted(programs, key=lambda item: (-item["errors"], -item["calls"], item["program"]))[:TOP],
        "environment": counts.environment,
        "repeated_commands": sorted(repeated, key=lambda item: (-item["sessions"], -item["times"], item["command"]))[
            :TOP
        ],
        "corrections": counts.corrections,
        "sessions": {
            "digests": len(sessions),
            "messages": counts.messages,
            "models": [
                {"model": name, "sessions": n}
                for name, n in sorted(counts.models.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            ],
            "transcripts": counts.sources["transcript"],
            "from_digests": counts.sources["digest"],
            "local_runs": counts.sources["local"],
        },
    }


# The weekly series


def week_of(moment: datetime) -> str:
    """The Monday, in UTC, of the week of ``moment``."""
    day = moment.astimezone(UTC).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def _history(project_id: int, until: datetime):
    f, r = tables.session_facets, tables.runs
    local = (r.c.kind == "local") & _counted_elsewhere(f)
    return (
        select(f.c.kind, f.c.label, f.c.at, f.c.user_id, f.c.summary, r.c.kind.label("run_kind"), r.c.plan_id)
        .add_columns(local.label("counted_elsewhere"))
        .select_from(f.outerjoin(r, r.c.id == f.c.run_id))
        .where(f.c.project_id == project_id, f.c.body.is_not(None), f.c.at < until)
        .order_by(f.c.at, f.c.id)
    )


def _readable(row, access: ProjectAccess, through: str | None, plans: set, owner_id: int | None) -> bool:
    if row.kind == "session":
        return through is not None and access.visible(row.label, through)
    if row.run_kind == "local":
        return owner_id is not None and row.user_id == owner_id and not row.counted_elsewhere
    return row.run_kind == "review" or row.plan_id in plans


class _Week:
    def __init__(self):
        self.sessions = 0
        self.runs = 0
        self.members: set = set()
        self.sums: Counter = Counter()
        self.cost = 0.0
        self.peak = 0

    def add(self, kind: str, user_id, numbers: dict) -> None:
        if kind == "session":
            self.sessions += 1
        else:
            self.runs += 1
        if user_id is not None:
            self.members.add(user_id)
        for name in WEEK_SUMS:
            self.sums[name] += facets.count(numbers.get(name)) or 0
        cost = numbers.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.cost += cost
        self.peak = max(self.peak, facets.count(numbers.get("context_peak")) or 0)

    def entry(self, week: str) -> dict:
        calls, errors = self.sums["tool_calls"], self.sums["tool_errors"]
        return {
            "week": week,
            "sessions": self.sessions,
            "runs": self.runs,
            "members": len(self.members),
            **{name: self.sums[name] for name in WEEK_SUMS},
            "error_rate": rate(errors, calls),
            "cost_usd": round(self.cost, 4),
            "context_peak": self.peak or None,
        }


async def weekly(
    conn: AsyncConnection,
    access: ProjectAccess,
    plans: list[str],
    until: datetime,
    owner_id: int | None,
    waiting: list[Session],
) -> list[dict]:
    """The weekly series of every facet the reader may read before ``until``, and of the sessions of ``waiting`` whose
    digest has no facet yet; the latest week first."""
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    weeks: dict[str, _Week] = defaultdict(_Week)
    readable = set(plans)
    for row in await conn.execute(_history(access.project_id, until)):
        if row.at is not None and _readable(row, access, through, readable, owner_id):
            weeks[week_of(row.at)].add(row.kind, row.user_id, row.summary if isinstance(row.summary, dict) else {})
    for session in waiting:
        if session.numbers is not None:
            weeks[week_of(session.at)].add("session", None, session.numbers)
    ordered = sorted(weeks.items(), reverse=True)[:MAX_WEEKS]
    return [found.entry(week) for week, found in ordered]
