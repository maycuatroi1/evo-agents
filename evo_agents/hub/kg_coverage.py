"""The health of a project's knowledge graph as the hub models it: what a build covers (``build_coverage``), the files
each git source holds in it (``source_files``), how they compare with the tree of the repo's default branch on the
forge (``compare_files``), and what the facets of sessions and runs say agents got from the graph (``retrieval``).
Pure functions over JSON values and the worker's cache of a project, no database: ``evo_agents.hub.kg_build`` keeps
the first two with each build, and ``evo_agents.hub.server.kg_health`` measures the graph and counts the night's figures
(``kg_health``) from them. ``evo_agents.hub.ledger`` reads its metrics (METRICS) from those figures.

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

from collections.abc import Iterable, Mapping
from datetime import datetime

from evo_agents.hub.facets import KG_LOOKUPS
from evo_agents.kg import freshness
from evo_agents.kg.connectors._files import excluded, selected

MAX_FILES = 50_000  # files of one git source a build keeps; past that the list is cut and says so
SAMPLES = 5  # paths, sessions and runs an entry names
GIT = "git"
MENTION_KEYS = ("mentions", "resolved", "ambiguous", "dangling")
RETRIEVAL_KEYS = ("calls", "lookups", "empty", "searches", "fallbacks", "edited", "in_results", "in_top")
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


def retrieval(bodies: Iterable[tuple[str, Mapping | None]]) -> dict:
    """What the facets' ``kg`` (``evo_agents.hub.facets.KgTrail``) of sessions and runs add up to, each given with the
    evidence that names it (``session:ID``, ``run:ID:SEQ``): those that called the graph, its calls and lookups, the
    lookups that came back empty, the searches after a call and the first after an empty one (a fall back to grep),
    the files edited after the first lookup, those a result named and those among its top 5; the shares of empty
    lookups, of empty ones followed by a search, and of edited files in the top 5; ``misses``, the edited files
    outside it; and the evidence of those with the most empty lookups and fall backs."""
    sums = dict.fromkeys(RETRIEVAL_KEYS, 0)
    used, worst = 0, []
    for evidence, kg in bodies:
        if not isinstance(kg, Mapping) or not _int(kg.get("calls")):
            continue
        used += 1
        found = {key: _int(kg.get(key)) for key in RETRIEVAL_KEYS if key != "lookups"} | {"lookups": _lookups(kg)}
        for key in RETRIEVAL_KEYS:
            sums[key] += found[key]
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
}
LEVELS = frozenset({"kg_behind", "kg_files_off"})  # the graph's state at a moment, not counts of activity
