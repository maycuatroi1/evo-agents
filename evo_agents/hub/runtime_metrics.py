"""The runtime and the harness of a project's sessions and runs, as the night's figures count them, and the metrics of
the Curator's ledger that read them. Pure functions over JSON values, standard library only:
``evo_agents.hub.server.runtime_figures`` reads the facets, the runs and the plans the reader may read and calls
``runtime`` and ``harness``; ``evo_agents.hub.ledger`` reads METRICS.

``runtime`` holds, over the sessions and the runs of the span (each counted once, as ``facet_figures`` counts them):

- ``sessions`` and ``runs``: how many report their tokens (``counted``), their tokens (input, output, cache write,
  cache read and the total) and the shares of the cache in what the model read (``cache_read_share`` and
  ``cache_write_share``, of input + cache write + cache read); their cost (over those that name one, ``priced``), in
  all and per session or per run; their context (the largest peak, the median and the 95th percentile of the peaks,
  the median of the first request's); their compactions (how many had one, how many in all, by trigger, the largest
  context compacted); and the heaviest HEAVIEST (sessions by cache reads, runs by cost) with their evidence. A session
  or a run whose tokens are all 0 is left out of the sums of tokens: its source said none (a digest of an older client,
  a run of an older daemon), and 0 would read as a count. ``runs`` adds ``by_kind`` and ``first_request``: what the
  first request of each run read from the cache and wrote to it, the keys its worker reports in its usage
  (``first_request_cache_read_input_tokens``, ``first_request_cache_creation_input_tokens``).
- ``merged_prs`` (``merged_prs``): the pull requests that merged in the span, each (plan, repo) once, the cost of the
  runs of their plans, and that cost per pull request.
- ``hooks`` and ``tools``: per hook (its name and its command) and per tool, the calls timed, the mean, the median and
  the 95th percentile, read from the buckets of ``facets.BUCKETS_MS``: the bound of the bucket each falls in, at most the
  longest call when the facet knows it (in the last, open bucket, the longest, else the last bound); the slowest first,
  by p95, at most TOP_TIMINGS. ``hook_ms`` is the time of every hook, in all.

``harness`` holds what the harness puts into the main thread's context of the sessions of Claude Code whose transcripts
the hub read (``facets.CONTEXT_SOURCES``, the text of the skills loaded, and the output of tools, MCP tools apart): per
source, the characters, the tokens they come to (CHARS_PER_TOKEN: an estimate, no tokenizer runs), its share and the
sessions it came in; the tokens of the fixed sources (``facets.CONTEXT_SOURCES``) in all and per session; the skill
list (``skill_list``): its tokens and the part of them that came as a whole list into a context that already held one
(``again_tokens``, with its share and the sessions it came in); the skills whose text filled the most (``skill_bodies``,
TOP_SKILLS); the always-loaded files, each file once with its largest size, grouped by repo (the last directory of its
path that names a repo of the project, else its directory); the tools whose output was largest; the heaviest HEAVIEST
sessions by their fixed sources; and the spool of the sessions' digests (the hook that cuts a long output:
``spool_results``, ``spool_original_chars``, ``spool_reads``).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from evo_agents.hub import curator
from evo_agents.hub.digest import TOOL_NAME
from evo_agents.hub.facet_readers import FIRST_REQUEST, PEAK_CONTEXT, run_usage
from evo_agents.hub.facets import BUCKETS_MS, CONTEXT_SOURCES, SKILL_BODIES, SKILLS_AGAIN, count

CHARS_PER_TOKEN = 4  # an estimate of the tokens of a text: no tokenizer runs on the hub
HEAVIEST = 5
TOP_TIMINGS = 15
TOP_OUTPUT = 10
TOP_REPOS = 10
TOP_FILES = 10
TOP_PLANS = 10
TOP_SKILLS = 10
TOKEN_KEYS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_write": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
}
OUTPUT_SOURCES = ("mcp_output", "tool_output")  # the output of MCP tools (a name mcp__<server>__<tool>), of the others
LOADED_SOURCES = (*CONTEXT_SOURCES, SKILL_BODIES)  # what a facet's sources count, ``skills_again`` (a part) apart
SOURCES = (*LOADED_SOURCES, *OUTPUT_SOURCES)
SPOOL_KEYS = ("spool_results", "spool_original_chars", "spool_reads")  # of a session's digest


def tokens(chars: int) -> int:
    """The tokens ``chars`` characters come to, by CHARS_PER_TOKEN."""
    return round(chars / CHARS_PER_TOKEN)


def share(part: float, whole: float) -> float:
    return round(part / whole, 4) if whole else 0.0


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _map(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def quantile(values: list[int], q: float) -> int | None:
    """The ``q`` quantile of ``values`` by nearest rank; None for none."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def bucket_quantile(buckets, q: float, longest: int | None = None) -> int | None:
    """The ``q`` quantile of the times counted in ``buckets`` (``facets.BUCKETS_MS``, the last one open): the bound of
    the bucket it falls in, at most ``longest`` when it is known; None when nothing was timed."""
    found = [count(n) or 0 for n in _list(buckets)]
    total = sum(found)
    if not total:
        return None
    rank, seen = max(1, math.ceil(q * total)), 0
    for index, n in enumerate(found):
        seen += n
        if seen >= rank:
            bound = BUCKETS_MS[index] if index < len(BUCKETS_MS) else None
            if bound is None:
                return longest or BUCKETS_MS[-1]
            return min(bound, longest) if longest else bound
    return longest or BUCKETS_MS[-1]


@dataclass(frozen=True)
class Item:
    """One session or run the figures count: its evidence and name, and the parts of its facet they read (``usage``,
    ``cost_usd``, ``context``, ``compactions``, ``compaction``, ``tools``, ``hooks``, ``sources``, ``loaded``); a run
    also has its kind, its plan and its own usage, as its worker reported it."""

    evidence: str  # session:ID, or run:ID:1
    name: str  # a session's id, or run:ID
    body: Mapping
    messages: int = 0
    model: str | None = None
    kind: str | None = None
    plan_id: str | None = None
    usage: Mapping | None = None


def run_cost(usage) -> float | None:
    """What a run's usage says it cost; None when it names no cost (Codex reports tokens only)."""
    if not isinstance(usage, Mapping) or not any(key in usage for key in ("total_cost_usd", "cost")):
        return None
    return curator.run_cost(usage)


# The runtime


class _Spend:
    """What the sessions or the runs add up to, one at a time."""

    def __init__(self):
        self.counted = 0
        self.sums: Counter = Counter()
        self.priced = 0
        self.cost = 0.0
        self.peaks: list[int] = []
        self.firsts: list[int] = []
        self.compacted = 0
        self.compactions = 0
        self.triggers: Counter = Counter()
        self.largest: int | None = None

    def add(self, found: dict, cost: float | None, peak, first, body: Mapping) -> None:
        if any(found.values()):
            self.counted += 1
            self.sums.update(found)
        if cost is not None:
            self.priced += 1
            self.cost += cost
        for listed, value in ((self.peaks, peak), (self.firsts, first)):
            if count(value):
                listed.append(count(value))
        compactions = count(body.get("compactions")) or 0
        if compactions:
            self.compacted += 1
            self.compactions += compactions
        for trigger, value in _map(body.get("compaction")).items():
            if trigger == "largest_tokens" and count(value):
                self.largest = max(self.largest or 0, count(value))
            elif trigger != "largest_tokens" and count(value):
                self.triggers[str(trigger)] += count(value)

    def block(self, per: str) -> dict:
        read = self.sums["input"] + self.sums["cache_write"] + self.sums["cache_read"]
        return {
            "counted": self.counted,
            "tokens": {**{name: self.sums[name] for name in TOKEN_KEYS}, "total": sum(self.sums.values())},
            "cache_read_share": share(self.sums["cache_read"], read),
            "cache_write_share": share(self.sums["cache_write"], read),
            "priced": self.priced,
            "cost_usd": round(self.cost, 4),
            f"cost_per_{per}": round(self.cost / self.priced, 4) if self.priced else None,
            "context": {
                "with_peak": len(self.peaks),
                "peak_max": max(self.peaks) if self.peaks else None,
                "peak_p50": quantile(self.peaks, 0.5),
                "peak_p95": quantile(self.peaks, 0.95),
                "first_p50": quantile(self.firsts, 0.5),
            },
            "compactions": {
                "compacted": self.compacted,
                "total": self.compactions,
                "by_trigger": dict(sorted(self.triggers.items())),
                "largest_tokens": self.largest,
            },
        }


def _tokens_of(usage) -> dict:
    found = run_usage(usage)
    return {name: found[key] for name, key in TOKEN_KEYS.items()}


def _sessions(sessions: list[Item]) -> dict:
    spend, heavy = _Spend(), []
    for item in sessions:
        found = _tokens_of(item.body.get("usage"))
        context = _map(item.body.get("context"))
        spend.add(found, _number(item.body.get("cost_usd")), context.get("peak"), context.get("first"), item.body)
        if any(found.values()):
            heavy.append((item, found, count(context.get("peak"))))
    heavy.sort(key=lambda entry: (-entry[1]["cache_read"], -sum(entry[1].values()), entry[0].name))
    return {
        **spend.block("session"),
        "heaviest": [
            {
                "session": item.name,
                "messages": item.messages,
                "model": item.model,
                "cache_read_tokens": found["cache_read"],
                "tokens": sum(found.values()),
                "context_peak": peak,
                "cost_usd": _number(item.body.get("cost_usd")),
                "evidence": item.evidence,
            }
            for item, found, peak in heavy[:HEAVIEST]
        ],
    }


def _runs(runs: list[Item]) -> dict:
    spend, kinds, heavy = _Spend(), defaultdict(Counter), []
    reads, writes, cold = [], [], 0
    for item in runs:
        usage = _map(item.usage)
        found, cost = _tokens_of(usage), run_cost(usage)
        context = _map(item.body.get("context"))
        first = sum(count(usage.get(key)) or 0 for key in FIRST_REQUEST) or context.get("first")
        spend.add(found, cost, usage.get(PEAK_CONTEXT) or context.get("peak"), first, item.body)
        kind = kinds[item.kind or "unknown"]
        kind["runs"] += 1
        kind["tokens"] += sum(found.values())
        kind["cost"] += round((cost or 0.0) * 1_000_000)  # micro-USD, so the sums stay exact
        read, write = (count(usage.get(key)) for key in FIRST_REQUEST)
        if read is not None or write is not None:
            reads.append(read or 0)
            writes.append(write or 0)
            cold += 1 if not read and write else 0
        heavy.append((item, found, cost))
    heavy.sort(key=lambda entry: (-(entry[2] or 0.0), -sum(entry[1].values()), entry[0].name))
    return {
        **spend.block("run"),
        "by_kind": [
            {"kind": name, "runs": n["runs"], "tokens": n["tokens"], "cost_usd": round(n["cost"] / 1_000_000, 4)}
            for name, n in sorted(kinds.items(), key=lambda kv: (-kv[1]["cost"], -kv[1]["runs"], kv[0]))
        ],
        "first_request": {
            "runs": len(reads),
            "cache_read_p50": quantile(reads, 0.5),
            "cache_write_p50": quantile(writes, 0.5),
            "cache_write_tokens": sum(writes),
            "cold": cold,  # its first request wrote to the cache and read nothing from it
        },
        "heaviest": [
            {
                "run": item.name,
                "kind": item.kind,
                "plan_id": item.plan_id,
                "cost_usd": cost,
                "tokens": sum(found.values()),
                "context_peak": count(_map(item.usage).get(PEAK_CONTEXT)),
                "evidence": item.evidence,
            }
            for item, found, cost in heavy[:HEAVIEST]
        ],
    }


class _Timing:
    def __init__(self):
        self.timed = 0
        self.ms = 0
        self.longest = 0
        self.buckets = [0] * (len(BUCKETS_MS) + 1)
        self.runs = 0
        self.failed = 0
        self.timed_out = 0
        self.sessions = 0
        self.of_runs = 0

    def add(self, entry: Mapping, run: bool) -> None:
        self.timed += count(entry.get("timed")) or 0
        self.ms += count(entry.get("ms")) or 0
        self.longest = max(self.longest, count(entry.get("max_ms")) or 0)
        for index, n in enumerate(_list(entry.get("buckets"))[: len(self.buckets)]):
            self.buckets[index] += count(n) or 0
        self.runs += count(entry.get("runs")) or 0
        self.failed += count(entry.get("failed")) or 0
        self.timed_out += count(entry.get("timed_out")) or 0
        self.of_runs += 1 if run else 0
        self.sessions += 0 if run else 1

    def numbers(self) -> dict:
        longest = self.longest or None
        return {
            "timed": self.timed,
            "mean_ms": round(self.ms / self.timed) if self.timed else None,
            "p50_ms": bucket_quantile(self.buckets, 0.5, longest),
            "p95_ms": bucket_quantile(self.buckets, 0.95, longest),
        }


def _slowest(entry: dict) -> tuple:
    return (-(entry["p95_ms"] or 0), -(entry["mean_ms"] or 0), -entry["timed"])


def _timings(sessions: list[Item], runs: list[Item]) -> tuple[list[dict], list[dict], int]:
    hooks: dict[tuple, _Timing] = defaultdict(_Timing)
    tools: dict[str, _Timing] = defaultdict(_Timing)
    hook_ms = 0
    for item, run in [*((item, False) for item in sessions), *((item, True) for item in runs)]:
        for entry in _list(item.body.get("hooks")):
            if isinstance(entry, Mapping):
                hooks[(str(entry.get("hook") or ""), str(entry.get("command") or ""))].add(entry, run)
                hook_ms += count(entry.get("ms")) or 0
        for entry in _list(item.body.get("tools")):
            if isinstance(entry, Mapping) and entry.get(TOOL_NAME) and count(entry.get("timed")):
                tools[str(entry[TOOL_NAME])].add(entry, run)
    hook_list = [
        {
            "hook": name,
            "command": command,
            "runs": found.runs,
            **found.numbers(),
            "max_ms": found.longest or None,
            "failed": found.failed,
            "timed_out": found.timed_out,
            "sessions": found.sessions,
        }
        for (name, command), found in hooks.items()
    ]
    tool_list = [
        {"tool": name, **found.numbers(), "sessions": found.sessions, "runs": found.of_runs}
        for name, found in tools.items()
    ]
    hook_list.sort(key=lambda entry: (*_slowest(entry), entry["hook"], entry["command"]))
    tool_list.sort(key=lambda entry: (*_slowest(entry), entry["tool"]))
    return hook_list[:TOP_TIMINGS], tool_list[:TOP_TIMINGS], hook_ms


def merged_prs(merged: Iterable[Mapping], plan_costs: Mapping[str, Mapping]) -> dict:
    """The pull requests that merged in the span (each ``{plan_id, repo, pr, merged_at}``) and the cost of the runs of
    their plans (``plan_costs``: a plan's ``runs``, ``priced`` and ``cost_usd``): in all, and per pull request."""
    per_plan: dict[str, list] = defaultdict(list)
    for entry in merged:
        per_plan[str(entry["plan_id"])].append({key: entry.get(key) for key in ("repo", "pr", "merged_at")})
    entries = []
    for plan_id, prs in per_plan.items():
        cost = _map(plan_costs.get(plan_id))
        entries.append(
            {
                "plan_id": plan_id,
                "prs": sorted(prs, key=lambda pr: (str(pr.get("merged_at") or ""), str(pr.get("repo") or ""))),
                "runs": count(cost.get("runs")) or 0,
                "priced": count(cost.get("priced")) or 0,
                "cost_usd": round(_number(cost.get("cost_usd")) or 0.0, 4),
            }
        )
    entries.sort(key=lambda entry: (-entry["cost_usd"], entry["plan_id"]))
    prs = sum(len(entry["prs"]) for entry in entries)
    total = sum(entry["cost_usd"] for entry in entries)
    return {
        "prs": prs,
        "plans": len(entries),
        "runs": sum(entry["runs"] for entry in entries),
        "priced_runs": sum(entry["priced"] for entry in entries),
        "cost_usd": round(total, 4),
        "cost_per_pr": round(total / prs, 4) if prs else None,
        "entries": entries[:TOP_PLANS],
    }


def runtime(sessions: list[Item], runs: list[Item], merged: Mapping) -> dict:
    """The night's ``runtime`` (see the module): ``merged`` is what ``merged_prs`` says."""
    hooks, tools, hook_ms = _timings(sessions, runs)
    return {
        "sessions": _sessions(sessions),
        "runs": _runs(runs),
        "merged_prs": dict(merged),
        "hook_ms": hook_ms,
        "hooks": hooks,
        "tools": tools,
    }


# The harness


def is_mcp(tool: str) -> bool:
    return tool.startswith("mcp__")


def _repo_of(path: str, repos: set[str]) -> tuple[str | None, str]:
    """(the repo of the project the path's last directory naming one is, the file's directory)."""
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    named = [part for part in folder.split("/") if part in repos]
    return (named[-1] if named else None), folder


def _loaded(known: list[Item], repos: set[str]) -> dict:
    files: dict[str, dict] = {}
    for item in known:
        for entry in _list(item.body.get("loaded")):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
                continue
            found = files.setdefault(entry["path"], {"type": entry.get("type"), "chars": 0, "sessions": 0})
            found["chars"] = max(found["chars"], count(entry.get("chars")) or 0)
            found["sessions"] += 1
    groups: dict[tuple, list] = defaultdict(list)
    for path, found in files.items():
        repo, folder = _repo_of(path, repos)
        groups[(repo, None if repo else folder)].append({"path": path, **found, "tokens": tokens(found["chars"])})
    listed = []
    for (repo, folder), entries in groups.items():
        entries.sort(key=lambda entry: (-entry["chars"], entry["path"]))
        chars = sum(entry["chars"] for entry in entries)
        listed.append(
            {"repo": repo, "dir": folder, "chars": chars, "tokens": tokens(chars), "files": entries[:TOP_FILES]}
        )
    listed.sort(key=lambda entry: (-entry["chars"], entry["repo"] or "", entry["dir"] or ""))
    chars = sum(found["chars"] for found in files.values())
    return {"files": len(files), "chars": chars, "tokens": tokens(chars), "repos": listed[:TOP_REPOS]}


def _output(item: Item) -> tuple[Counter, Counter]:
    chars, per_tool = Counter(), Counter()
    for entry in _list(item.body.get("tools")):
        if isinstance(entry, Mapping) and entry.get(TOOL_NAME):
            name, n = str(entry[TOOL_NAME]), count(entry.get("output_chars")) or 0
            chars["mcp_output" if is_mcp(name) else "tool_output"] += n
            per_tool[name] += n
    return chars, per_tool


def harness(sessions: list[Item], repos: Iterable[str], spool: Mapping | None) -> dict:
    """The night's ``harness`` (see the module): ``repos`` are the names of the project's repos, ``spool`` the sums of
    the digests' SPOOL_KEYS with how many digests said them (``digests``)."""
    known = [item for item in sessions if isinstance(item.body.get("sources"), Mapping)]
    chars, came, outputs, bodies, heavy = Counter(), Counter(), defaultdict(Counter), defaultdict(Counter), []
    for item in known:
        found = Counter({name: count(item.body["sources"].get(name)) or 0 for name in (*LOADED_SOURCES, SKILLS_AGAIN)})
        output, per_tool = _output(item)
        found.update(output)
        chars.update(found)
        came.update(name for name, n in found.items() if n)
        for name, n in per_tool.items():
            outputs[name]["chars"] += n
            outputs[name]["sessions"] += 1 if n else 0
        for entry in _list(item.body.get("skills")):
            n = count(entry.get("chars")) if isinstance(entry, Mapping) else None
            if n:
                bodies[str(entry.get("skill"))].update(chars=n, sessions=1)
        fixed = sum(found[name] for name in CONTEXT_SOURCES)
        heavy.append((item, fixed, found))
    total, fixed = sum(chars[name] for name in SOURCES), sum(chars[name] for name in CONTEXT_SOURCES)
    heavy.sort(key=lambda entry: (-entry[1], entry[0].name))
    tool_output = sorted(outputs.items(), key=lambda kv: (-kv[1]["chars"], kv[0]))[:TOP_OUTPUT]
    skill_bodies = sorted(bodies.items(), key=lambda kv: (-kv[1]["chars"], kv[0]))[:TOP_SKILLS]
    return {
        "sessions": len(known),
        "chars_per_token": CHARS_PER_TOKEN,
        "sources": [
            {
                "source": name,
                "chars": chars[name],
                "tokens": tokens(chars[name]),
                "share": share(chars[name], total),
                "sessions": came[name],
            }
            for name in SOURCES
        ],
        "fixed_tokens": tokens(fixed),
        "fixed_tokens_per_session": round(tokens(fixed) / len(known)) if known else None,
        "skill_list": {
            "tokens": tokens(chars["skills"]),
            "again_tokens": tokens(chars[SKILLS_AGAIN]),
            "again_share": share(chars[SKILLS_AGAIN], chars["skills"]),
            "sessions_again": came[SKILLS_AGAIN],
        },
        "skill_bodies": [
            {"skill": name, "chars": n["chars"], "tokens": tokens(n["chars"]), "sessions": n["sessions"]}
            for name, n in skill_bodies
        ],
        "always_loaded": _loaded(known, set(repos)),
        "tool_output": [
            {
                "tool": name,
                "mcp": is_mcp(name),
                "chars": n["chars"],
                "tokens": tokens(n["chars"]),
                "sessions": n["sessions"],
            }
            for name, n in tool_output
            if n["chars"]
        ],
        "heaviest": [
            {
                "session": item.name,
                "fixed_tokens": tokens(weight),
                **{f"{name}_tokens": tokens(found[name]) for name in (*LOADED_SOURCES, SKILLS_AGAIN)},
                "evidence": item.evidence,
            }
            for item, weight, found in heavy[:HEAVIEST]
            if weight
        ],
        "spool": dict(spool) if spool is not None else None,
    }


# The metrics of the ledger


def _at(figures: Mapping | None, *path: str):
    found = figures
    for key in path:
        found = found.get(key) if isinstance(found, Mapping) else None
    return found


def _num(value) -> float:
    return _number(value) or 0.0


def _both(figures: Mapping | None, *path: str) -> float:
    return _num(_at(figures, "runtime", "sessions", *path)) + _num(_at(figures, "runtime", "runs", *path))


def _source_tokens(figures: Mapping | None, names: tuple[str, ...]) -> float:
    listed = _list(_at(figures, "harness", "sources"))
    return float(
        sum(
            _num(entry.get("tokens")) for entry in listed if isinstance(entry, Mapping) and entry.get("source") in names
        )
    )


# metric: (what it counts, in words for the owner; its value in a night's figures)
METRICS = {
    "session_cost_usd": (
        "the cost of the sessions that name one, in USD",
        lambda figures: _num(_at(figures, "runtime", "sessions", "cost_usd")),
    ),
    "cache_write_tokens": (
        "tokens the sessions and runs wrote to the prompt cache",
        lambda figures: _both(figures, "tokens", "cache_write"),
    ),
    "context_peak_p95": (
        "the 95th percentile of the sessions' peak context, in tokens",
        lambda figures: _num(_at(figures, "runtime", "sessions", "context", "peak_p95")),
    ),
    "compactions": (
        "compactions of the context in sessions and runs",
        lambda figures: _both(figures, "compactions", "total"),
    ),
    "hook_ms": (
        "milliseconds the hooks of sessions and runs ran",
        lambda figures: _num(_at(figures, "runtime", "hook_ms")),
    ),
    "always_loaded_tokens": (
        "tokens of the always-loaded files (CLAUDE.md, AGENTS.md, memory) the sessions loaded, each file once",
        lambda figures: _num(_at(figures, "harness", "always_loaded", "tokens")),
    ),
    "fixed_context_tokens": (
        "tokens the always-loaded files, MCP instructions, the skill list and hooks put into the sessions",
        lambda figures: _source_tokens(figures, CONTEXT_SOURCES),
    ),
    "tool_output_tokens": (
        "tokens of the tool output the sessions read, MCP tools included",
        lambda figures: _source_tokens(figures, OUTPUT_SOURCES),
    ),
    "skill_list_again_tokens": (
        "tokens of whole skill lists that came again into a context that already held one",
        lambda figures: _num(_at(figures, "harness", "skill_list", "again_tokens")),
    ),
    "skill_body_tokens": (
        "tokens of the text of the skills the sessions loaded",
        lambda figures: _source_tokens(figures, (SKILL_BODIES,)),
    ),
}
LENS_METRICS = {
    "runtime": ("session_cost_usd", "cache_write_tokens", "context_peak_p95", "compactions", "hook_ms"),
    "harness": (
        "always_loaded_tokens",
        "fixed_context_tokens",
        "tool_output_tokens",
        "skill_list_again_tokens",
        "skill_body_tokens",
    ),
}
# The levels a span's sessions are at rather than counts of its activity: compared as they are, not per session or run.
LEVELS = frozenset({"context_peak_p95", "always_loaded_tokens"})
USD = frozenset({"session_cost_usd"})


def evidence(figures: Mapping | None, worse: set[str]) -> list[str]:
    """The evidence ``figures`` hold for the metrics of ``worse`` among METRICS: the heaviest sessions and runs of the
    runtime, and the sessions whose fixed sources weighed most for the harness."""
    found: list[str] = []
    paths = []
    if worse & set(LENS_METRICS["runtime"]):
        paths += [("runtime", "sessions", "heaviest"), ("runtime", "runs", "heaviest")]
    if worse & set(LENS_METRICS["harness"]):
        paths.append(("harness", "heaviest"))
    for path in paths:
        for entry in _list(_at(figures, *path)):
            if isinstance(entry, Mapping) and isinstance(entry.get("evidence"), str) and entry["evidence"] not in found:
                found.append(entry["evidence"])
    return found
