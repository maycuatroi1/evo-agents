"""The health of a project's knowledge graph as the hub models it: what a build covers (``build_coverage``), the files
each git source holds in it (``source_files``), how they compare with the tree of the repo's default branch on the
forge (``compare_files``), and what the facets of sessions and runs say agents got from the graph (``retrieval``).
Pure functions over JSON values and the worker's cache of a project, no database: ``evo_agents.hub.kg_build`` keeps
the first two with each build, and ``evo_agents.hub.server.kg_health`` measures the graph and counts the night's figures
(``kg_health``) from them. ``evo_agents.hub.ledger`` reads its metrics (METRICS) from those figures, among them the
open items of each rule of the review queue (``kg_review_<rule>``, ``evo_agents.hub.kg_review``).

A build's coverage (``kg_builds.coverage``) holds, for each source of the build, its items and mentions and how many of
those resolved, stayed ambiguous or dangled, with their shares, and for a git source the commit it read
(``evo_agents.kg.freshness``: the same field ``kg status`` shows); the same over every source; the dangling edges; the
Python functions and methods outside tests that no call edge reaches (``evo_agents.kg.health.call_reach``); and whether
the build was skipped because nothing had changed.

A git source's files are the live items ``<source>:file:<path>`` of the build's corpus with their revision, the blob
SHA (``evo_agents.kg.connectors.git``), at most MAX_FILES of them. Compared with the tree of the default branch, as far
as the source reads it (its ``exclude`` and ``allow`` over DEFAULT_EXCLUDE, and with ``assets: false`` only what it
selects), a file is ``missing`` when the branch has it and the graph does not, ``stale`` when the graph holds it at
another revision, and ``deleted`` when the graph holds it and the branch no longer does.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime

from evo_agents.hub.facets import KG_LOOKUPS
from evo_agents.kg import freshness
from evo_agents.kg.connectors._files import excluded, selected
from evo_agents.kg.review import RULES as REVIEW_RULES

MAX_FILES = 50_000  # files of one git source a build keeps; past that the list is cut and says so
SAMPLES = 5  # paths, sessions and runs an entry names
MAX_OFF_PATHS = 2000  # paths missing, stale and deleted a measure keeps of one source, for the tier rules
GIT = "git"
MENTION_KEYS = ("mentions", "resolved", "ambiguous", "dangling")
RETRIEVAL_KEYS = ("calls", "lookups", "empty", "searches", "fallbacks", "edited", "in_results", "in_top")
LABEL_KEYS = ("chains", "grep_after", "queries", "clicked")  # of the facets' kg since FACET_VERSION 3
READ_KEYS = ("commit", "ref", "base", "behind")


def _int(value) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def git_sources(knowledge: Mapping | None) -> list[dict]:
    """The sources of a knowledge config that the git connector reads, in its order."""
    sources = (knowledge or {}).get("sources") if isinstance(knowledge, Mapping) else None
    return [
        source
        for source in sources or []
        if isinstance(source, dict) and source.get("connector") == GIT and isinstance(source.get("id"), str)
    ]


# A build


def _mentions(entry: Mapping) -> dict:
    counts = {key: _int(entry.get(key)) for key in MENTION_KEYS}
    whole = counts["mentions"]
    shares = {f"{key}_ratio": ratio(counts[key], whole) for key in ("resolved", "ambiguous", "dangling")}
    return {**counts, **shares}


def build_coverage(coverage: Mapping | None, reach: Mapping | None, *, skipped: bool) -> dict:
    """What ``kg_builds.coverage`` keeps of a build's coverage (``evo_agents.kg.build``), with ``reach``, the call
    reach of its graph (``evo_agents.kg.health.call_reach``)."""
    coverage = coverage if isinstance(coverage, Mapping) else {}
    sources = []
    for entry in coverage.get("sources") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            continue
        row = {"id": entry["id"], "items": _int(entry.get("items")), **_mentions(entry)}
        read = entry.get("read")
        if isinstance(read, Mapping) and isinstance(read.get("commit"), str):
            row["read"] = {key: read.get(key) for key in READ_KEYS}
        sources.append(row)
    totals = {key: sum(row[key] for row in sources) for key in MENTION_KEYS}
    return {
        "sources": sources,
        **_mentions(totals),
        "dangling_edges": _int(coverage.get("dangling_edges")),
        "python": dict(reach) if isinstance(reach, Mapping) else None,
        "skipped": bool(skipped),
    }


def source_files(project) -> dict[str, dict]:
    """For each git source of ``project`` (``evo_agents.kg.project.Project``), what its corpus holds now: the commit
    its cursor names (None without one), its files {path: revision} by path, at most MAX_FILES, and whether the list
    was cut."""
    corpus = project.corpus()
    try:
        found = {}
        for source in git_sources(project.knowledge):
            sid = source["id"]
            prefix = f"{sid}:file:"
            files = {}
            for item in corpus.items(sid):
                if item.item_id.startswith(prefix) and isinstance(item.rev, str):
                    path = (item.record.get("props") or {}).get("path") or item.item_id[len(prefix) :]
                    files[str(path)] = item.rev
            ordered = sorted(files.items())
            read = freshness.read_of(corpus.cursor(sid))
            found[sid] = {
                "commit": read["commit"] if read else None,
                "files": dict(ordered[:MAX_FILES]),
                "truncated": len(ordered) > MAX_FILES,
            }
        return found
    finally:
        corpus.close()


# Against the default branch


def read_by(source: Mapping, path: str) -> bool:
    """Whether the git source ``source`` reads the file at ``path`` of its tree: as an item of its own, or with
    ``assets: false`` only as one it selects."""
    include, exclude, allow = source.get("include"), source.get("exclude"), source.get("allow")
    if source.get("assets", True) is False:
        return selected(path, include, exclude, allow)
    return not excluded(path, exclude, allow)


def compare_files(source: Mapping, tree: Mapping[str, str], held: Mapping[str, str]) -> dict:
    """The files of a default branch's ``tree`` ({path: blob SHA}) that ``source`` reads, against those the graph
    ``held``: how many each has, and the paths missing, stale and deleted, counted and the first SAMPLES of each."""
    branch = {path: sha for path, sha in tree.items() if read_by(source, path)}
    missing = sorted(set(branch) - set(held))
    deleted = sorted(set(held) - set(branch))
    stale = sorted(path for path in set(branch) & set(held) if branch[path] != held[path])
    return {
        "tree_files": len(branch),
        "graph_files": len(held),
        "missing": len(missing),
        "stale": len(stale),
        "deleted": len(deleted),
        "samples": {"missing": missing[:SAMPLES], "stale": stale[:SAMPLES], "deleted": deleted[:SAMPLES]},
    }


def off_paths(source: Mapping, tree: Mapping[str, str], held: Mapping[str, str]) -> dict:
    """The paths ``compare_files`` counts as missing, stale and deleted, each list sorted, MAX_OFF_PATHS of them in
    all, and whether there were more (``cut``): what the tier rules read of a path whose file the graph lacks or holds
    older than the default branch (``evo_agents.hub.server.change_tiers``)."""
    branch = {path: sha for path, sha in tree.items() if read_by(source, path)}
    found = {
        "missing": sorted(set(branch) - set(held)),
        "stale": sorted(path for path in set(branch) & set(held) if branch[path] != held[path]),
        "deleted": sorted(set(held) - set(branch)),
    }
    room, kept = MAX_OFF_PATHS, {}
    for key, paths in found.items():
        kept[key] = paths[:room]
        room -= len(kept[key])
    return {**kept, "cut": sum(len(paths) for paths in found.values()) > MAX_OFF_PATHS}


def file_evidence(repo: str | None, samples: Mapping) -> list[str]:
    """Evidence a finding can cite for the files of ``samples`` (``compare_files``): ``code:REPO:PATH``, the missing
    first, at most SAMPLES."""
    if not repo:
        return []
    paths = [path for key in ("missing", "stale", "deleted") for path in samples.get(key) or []]
    return [f"code:{repo}:{path}" for path in dict.fromkeys(paths)][:SAMPLES]


def lag_hours(oldest_unread: str | None, now: datetime) -> float | None:
    """Hours from the oldest commit of the default branch the graph has not read (its committer time, ISO 8601) to
    ``now``; None when that time does not read."""
    if not isinstance(oldest_unread, str):
        return None
    try:
        when = datetime.fromisoformat(oldest_unread.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return round(max((now - when).total_seconds(), 0.0) / 3600, 2)


# What agents got from the graph


def _lookups(kg: Mapping) -> int:
    return sum(
        _int(item.get("calls"))
        for item in kg.get("tools") or []
        if isinstance(item, Mapping) and item.get("tool") in KG_LOOKUPS
    )


def percentile(values: list[int], share: float) -> int:
    """The nearest-rank percentile ``share`` (0.5, 0.9) of ``values``; 0 for none."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(math.ceil(share * len(ordered)) - 1, 0)]


def _spread(values: list[int]) -> dict:
    return {"p50": percentile(values, 0.5), "p90": percentile(values, 0.9)}


def retrieval(bodies: Iterable[tuple[str, Mapping | None]]) -> dict:
    """What the facets' ``kg`` (``evo_agents.hub.facets.KgTrail``) of sessions and runs add up to, each given with the
    evidence that names it (``session:ID``, ``run:ID:SEQ``): those that called the graph, its calls and lookups, the
    lookups that came back empty, the searches after a call and the first after an empty one (a fall back to grep),
    the files edited after the first lookup, those a result named and those among its top 5; the shares of empty
    lookups, of empty ones followed by a search, and of edited files in the top 5; ``misses``, the edited files
    outside it; and the evidence of those with the most empty lookups and fall backs.

    From the facets of version 3 on: ``tokens_per_lookup``, the p50 and p90 of the tokens of one lookup's result;
    ``tokens_to_answer`` and ``lookups_to_answer``, of the sessions and runs that read or edited a file a result named
    (``answered``), the p50 and p90 of the tokens and the lookups until the first such read; ``chains``, the runs of
    lookups with no other call between them, ``grep_after`` those a grep, rg or find followed right away, and
    ``grep_after_rate`` their share; ``queries``, the kg_search calls with a result, ``clicked`` those with a file of
    their results read or edited within ``facets.KG_WINDOW`` calls, ``click_rate`` their share, and ``click_ranks``,
    per rank, the searches whose best clicked result had it."""
    sums = dict.fromkeys(RETRIEVAL_KEYS, 0)
    more = dict.fromkeys(LABEL_KEYS, 0)
    used, worst = 0, []
    tokens: list[int] = []
    answers: list[tuple[int, int]] = []
    ranks: dict[int, int] = {}
    for evidence, kg in bodies:
        if not isinstance(kg, Mapping) or not _int(kg.get("calls")):
            continue
        used += 1
        found = {key: _int(kg.get(key)) for key in RETRIEVAL_KEYS if key != "lookups"} | {"lookups": _lookups(kg)}
        for key in RETRIEVAL_KEYS:
            sums[key] += found[key]
        for key in LABEL_KEYS:
            more[key] += _int(kg.get(key))
        tokens.extend(_int(n) for n in kg.get("lookup_tokens") or [] if not isinstance(n, bool))
        answer = kg.get("answer")
        if isinstance(answer, Mapping) and _int(answer.get("lookups")):
            answers.append((_int(answer.get("tokens")), _int(answer.get("lookups"))))
        clicks = kg.get("click_ranks")
        for rank, n in clicks.items() if isinstance(clicks, Mapping) else ():
            if str(rank).isdigit() and _int(n):
                ranks[int(rank)] = ranks.get(int(rank), 0) + _int(n)
        if found["empty"] or found["fallbacks"]:
            worst.append((-(found["empty"] + found["fallbacks"]), evidence))
    return {
        "used": used,
        **sums,
        "empty_rate": ratio(sums["empty"], sums["lookups"]),
        "fallback_rate": ratio(sums["fallbacks"], sums["empty"]),
        "in_top_rate": ratio(sums["in_top"], sums["edited"]),
        "misses": max(sums["edited"] - sums["in_top"], 0),
        "evidence": [evidence for _, evidence in sorted(worst)[:SAMPLES]],
        "tokens_per_lookup": _spread(tokens),
        "answered": len(answers),
        "tokens_to_answer": _spread([n for n, _ in answers]),
        "lookups_to_answer": _spread([n for _, n in answers]),
        **more,
        "grep_after_rate": ratio(more["grep_after"], more["chains"]),
        "click_rate": ratio(more["clicked"], more["queries"]),
        "click_ranks": {str(rank): ranks[rank] for rank in sorted(ranks)},
    }


# Metrics of the ledger


def behind(figure: Mapping | None) -> int:
    """The commits of the default branches the graph lacks, over its git sources (``kg_health.sources``)."""
    return sum(_int(item.get("behind")) for item in _sources(figure))


def files_off(figure: Mapping | None) -> int:
    """The files the graph lacks, holds stale or holds deleted, over its git sources."""
    return sum(_int(item.get(key)) for item in _sources(figure) for key in ("missing", "stale", "deleted"))


def _sources(figure: Mapping | None) -> list[Mapping]:
    sources = figure.get("sources") if isinstance(figure, Mapping) else None
    return [item for item in sources or [] if isinstance(item, Mapping)]


def _retrieved(figure: Mapping | None, key: str) -> int:
    found = figure.get("retrieval") if isinstance(figure, Mapping) else None
    return _int(found.get(key)) if isinstance(found, Mapping) else 0


def _figure(figure: Mapping | None) -> Mapping:
    found = figure.get("retrieval") if isinstance(figure, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def _spread_of(figure: Mapping | None, key: str, share: str) -> int:
    found = _figure(figure).get(key)
    return _int(found.get(share)) if isinstance(found, Mapping) else 0


def grep_after_percent(figure: Mapping | None) -> int:
    """Of the chains of kg lookups, the percent a grep, rg or find followed right away."""
    found = _figure(figure).get("grep_after_rate")
    return round(found * 100) if isinstance(found, (int, float)) and not isinstance(found, bool) else 0


def unclicked_percent(figure: Mapping | None) -> int:
    """Of the kg_search calls with a result, the percent whose result files the agent did not read or edit after."""
    found = _figure(figure)
    if not _int(found.get("queries")):
        return 0
    rate = found.get("click_rate")
    rate = rate if isinstance(rate, (int, float)) and not isinstance(rate, bool) else 0
    return max(100 - round(rate * 100), 0)


def review_open(figure: Mapping | None, rule: str) -> int:
    """The items of the review queue of ``rule`` open to the owner (``kg_health.review``)."""
    found = figure.get("review") if isinstance(figure, Mapping) else None
    counts = found.get("open") if isinstance(found, Mapping) else None
    return _int(counts.get(rule)) if isinstance(counts, Mapping) else 0


REVIEW_WORDS = {
    "dangling_mention": "documents and plans of the knowledge graph that mention code it does not hold",
    "unmentioned_document": "documents of the knowledge graph nothing mentions",
    "cluster_without_knowledge": "clusters of the knowledge graph with code and no document, plan, problem or claim",
    "possible_duplicate": "groups of nodes of the knowledge graph that may be duplicates",
    "ambiguous_match": "mentions and links of the knowledge graph that matched one of several nodes by name",
    "bulk_noise": "groups of images, generated files and repeated plan steps in the knowledge graph",
}


def _review_metric(rule: str):
    return lambda figure: review_open(figure, rule)


# metric: (what it counts, how it reads from the night's ``kg_health``)
METRICS = {
    "kg_behind": ("commits of the default branches the knowledge graph has not read", behind),
    "kg_files_off": ("files the knowledge graph lacks, holds stale or holds after their deletion", files_off),
    "kg_empty": ("kg lookups that came back empty", lambda figure: _retrieved(figure, "empty")),
    "kg_fallbacks": ("searches right after an empty kg lookup", lambda figure: _retrieved(figure, "fallbacks")),
    "kg_misses": (
        "files agents edited after a kg lookup that its top 5 did not name",
        lambda figure: _retrieved(figure, "misses"),
    ),
    "kg_lookup_tokens_p50": (
        "tokens of the median kg lookup's result",
        lambda figure: _spread_of(figure, "tokens_per_lookup", "p50"),
    ),
    "kg_lookup_tokens_p90": (
        "tokens of the kg lookup result at the 90th percentile",
        lambda figure: _spread_of(figure, "tokens_per_lookup", "p90"),
    ),
    "kg_answer_tokens": (
        "tokens of kg results, at the median, before an agent read a file they named",
        lambda figure: _spread_of(figure, "tokens_to_answer", "p50"),
    ),
    "kg_grep_after": ("percent of chains of kg lookups a grep, rg or find followed right away", grep_after_percent),
    "kg_unclicked": ("percent of kg searches with results whose files the agent did not open", unclicked_percent),
}
METRICS.update(
    {f"kg_review_{rule}": (f"open review items: {REVIEW_WORDS[rule]}", _review_metric(rule)) for rule in REVIEW_RULES}
)
REVIEW = frozenset(f"kg_review_{rule}" for rule in REVIEW_RULES)  # open items of the review queue, per rule
# what agents get from a lookup: a size or a share, not a count that grows with the activity
RETRIEVAL_LEVELS = frozenset(
    {"kg_lookup_tokens_p50", "kg_lookup_tokens_p90", "kg_answer_tokens", "kg_grep_after", "kg_unclicked"}
)
LEVELS = frozenset({"kg_behind", "kg_files_off"}) | REVIEW | RETRIEVAL_LEVELS  # not counts of activity
