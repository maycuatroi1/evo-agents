"""What ``evo-agents kg status`` prints of the hub's kg_status (``evo_agents.hub.kg_graph.HubSession``), which counts
the build in the hub's tables: ``render_brief`` with ``--brief`` and in the SessionStart note, and the lines the hub's
text gives the code backend of a source (``code_line``), the clusters (``cluster_line``) and the codes defined in two or
more places (``multiple_lines``). This machine holds no corpus or graph to count.

For each git source the graph read, the note names the commit and how many commits that is behind the remote's default
branch, as the machine that read it counted (``evo_agents.kg.freshness``), in one line of names and numbers.
"""

from __future__ import annotations

import datetime as dt

from evo_agents.kg import freshness

DERIVED_FROM_HINT = (
    "keep one definition per code; give each copy a derived_from field in its frontmatter, naming the original"
    " (design 4.6)"
)


def code_line(code: dict) -> str:
    """What the code backend of one source did: code items per extractor, ``none`` shown as no symbols."""
    used = ", ".join(f"{'no symbols' if name == 'none' else name} {n}" for name, n in code["extractors"].items())
    return f"code backend {code['backend']}: {code['items']} item(s); {used}"


def cluster_line(clusters: dict | None) -> str:
    """What the build's coverage says of its clusters (``evo_agents.kg.clusters``)."""
    if clusters is None:
        return "clusters: none, the build predates them: evo-agents hub kg health shows the hub's"
    if not clusters.get("computed"):
        return f"clusters: none, {clusters.get('reason')}"
    return (
        f"clusters: {clusters['clusters']} clusters, {clusters['subclusters']} subclusters over"
        f" {clusters['members']} nodes ({clusters['engine']})"
    )


def _age_seconds(stamp: str | None) -> float | None:
    if not stamp:
        return None
    then = dt.datetime.fromisoformat(stamp)
    return (dt.datetime.now(dt.UTC) - then).total_seconds()


def _human_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def render_brief(status: dict) -> str:
    """The SessionStart note: the snapshot's age and size, the commit each git source read, what failed. Names and
    counts only. The hub's kg_status names no source run, so its note says none failed or held."""
    graph = status.get("graph")
    sources = status.get("sources") or []
    stale = [
        s["id"] for s in sources if not s.get("withheld") and (not s["last_run"] or s["last_run"]["status"] != "ok")
    ]
    held = [s["id"] for s in sources if s.get("held_removals")]
    head = f"evo-kg: project {status['project']}"
    if graph and graph.get("ready"):
        head += (
            f", snapshot {_human_age(_age_seconds(graph.get('built_at')))} old,"
            f" {graph['nodes']} nodes, {graph['edges']} edges"
        )
    else:
        head += ", no graph built yet"
    lines = [head]
    if graph and graph.get("error"):
        lines.append(graph["error"])
    reads = freshness.brief(status.get("reads") or [])
    if reads:
        lines.append(reads)
    if stale:
        lines.append(f"sources failing or never synced: {', '.join(stale)}")
    if held:
        lines.append(f"deletions held for review: {', '.join(held)}")
    lines += [f"warning: {w}" for w in status.get("warnings", [])]
    return "\n".join(lines)


def multiple_lines(multiple: dict | None) -> list[str]:
    """The codes defined in two or more places, with the hint and the samples of the build's coverage."""
    if not multiple or not multiple["codes"]:
        return []
    lines = ["", f"codes defined in two or more places: {multiple['codes']}; {multiple['hint']}"]
    for sample in multiple["samples"]:
        lines.append(f"        {sample['code']}: {', '.join(sample['places'])}")
    if multiple["codes"] > len(multiple["samples"]):
        lines.append(f"        and {multiple['codes'] - len(multiple['samples'])} more")
    return lines
