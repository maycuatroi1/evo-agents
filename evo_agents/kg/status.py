"""``evo-agents kg status``: per-source freshness, coverage and held deletions, plus the graph snapshot.

Exit code is non-zero when a source has never synced, its last run failed, or deletions are held.
"""

from __future__ import annotations

import datetime as dt

from evo_agents.kg.project import Project

DERIVED_FROM_HINT = (
    "keep one definition per code; give each copy a derived_from field in its frontmatter, naming the original"
    " (design 4.6)"
)


def code_line(code: dict) -> str:
    """What the code backend of one source did: code items per extractor, ``none`` shown as no symbols."""
    used = ", ".join(f"{'no symbols' if name == 'none' else name} {n}" for name, n in code["extractors"].items())
    return f"code backend {code['backend']}: {code['items']} item(s); {used}"


def _age_seconds(stamp: str | None) -> float | None:
    if not stamp:
        return None
    then = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - then).total_seconds()


def _human_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def project_status(project: Project) -> dict:
    corpus = project.corpus()
    sources = []
    ok = True
    for src in project.sources():
        sid = src["id"]
        runs = corpus.runs(sid, limit=1)
        last = runs[0] if runs else None
        alive = len(corpus.items(sid))
        total = len(corpus.items(sid, include_deleted=True))
        held = corpus.held(sid)
        entry = {
            "id": sid,
            "connector": src.get("connector"),
            "items": alive,
            "deleted": total - alive,
            "held_removals": sum(len(h["ids"]) for h in held),
            "held": [{"scope": h["scope"], "reason": h["reason"]} for h in held],
            "last_run": None,
            "age_seconds": None,
        }
        if last:
            detail = last["detail"]
            entry["last_run"] = {
                "run_id": last["run_id"],
                "status": last["status"],
                "finished_at": last["finished_at"],
                "items": detail.get("items"),
                "errors": detail.get("errors"),
                "rejected": detail.get("rejected"),
                "removals": detail.get("removals"),
                "issues": detail.get("issues", [])[:5],
                "seconds": detail.get("seconds"),
            }
            ok_runs = [r for r in corpus.runs(sid, limit=50) if r["status"] == "ok"]
            entry["age_seconds"] = _age_seconds(ok_runs[0]["finished_at"]) if ok_runs else None
        if not last or last["status"] != "ok" or held:
            ok = False
        sources.append(entry)

    status = {
        "project": project.name,
        "harness": str(project.harness.root),
        "home": str(corpus.root),
        "ok": ok,
        "sources": sources,
        "graph": None,
        "multiple_definitions": None,
        "warnings": [],
    }
    try:
        from evo_agents.kg.store import StaleSchema, Store

        try:
            store = Store.open_existing(project)
        except StaleSchema as exc:
            store, status["graph"], status["ok"] = None, {"ready": False, "error": str(exc)}, False
        if store is not None:
            status["graph"] = store.summary()
            if not status["graph"].get("ready"):
                status["ok"] = False
            # Coverage gaps the build survived, such as non-Python code without symbols: ``ok`` stays as it is.
            status["warnings"] = status["graph"].get("coverage", {}).get("warnings", [])
            multiple = status["graph"].get("coverage", {}).get("multiple_definitions")
            if multiple is not None:  # a convention warning, not a failure: ``ok`` stays as it is
                status["multiple_definitions"] = {**multiple, "hint": DERIVED_FROM_HINT if multiple["codes"] else None}
    except ImportError:
        pass
    return status


def render_status(status: dict, *, brief: bool = False) -> str:
    lines = []
    graph = status.get("graph")
    if brief:
        stale = [s["id"] for s in status["sources"] if not s["last_run"] or s["last_run"]["status"] != "ok"]
        held = [s["id"] for s in status["sources"] if s["held_removals"]]
        head = f"evo-kg: project {status['project']}"
        if graph and graph.get("ready"):
            head += (
                f", snapshot {_human_age(_age_seconds(graph.get('built_at')))} old,"
                f" {graph['nodes']} nodes, {graph['edges']} edges"
            )
        else:
            head += ", no graph built yet"
        lines.append(head)
        if graph and graph.get("error"):
            lines.append(graph["error"])
        if stale:
            lines.append(f"sources failing or never synced: {', '.join(stale)}")
        if held:
            lines.append(f"deletions held for review: {', '.join(held)}")
        lines += [f"warning: {w}" for w in status.get("warnings", [])]
        return "\n".join(lines)

    lines.append(f"project {status['project']}  harness {status['harness']}")
    lines.append(f"corpus  {status['home']}")
    lines.append("")
    lines.append(f"{'source':<22}{'connector':<12}{'items':>7}{'deleted':>9}  {'last run':<9}{'age':>6}  notes")
    for s in status["sources"]:
        last = s["last_run"]
        state = last["status"] if last else "never"
        notes = []
        if s["held_removals"]:
            notes.append(f"{s['held_removals']} removal(s) held")
        if last and last.get("errors"):
            notes.append(f"{last['errors']} item error(s)")
        if last and last.get("rejected"):
            notes.append(f"{last['rejected']} rejected")
        if last and last["status"] != "ok" and last.get("issues"):
            notes.append(last["issues"][0])
        connector = (s["connector"] or "")[:11]
        lines.append(
            f"{s['id']:<22}{connector:<12}{s['items']:>7}{s['deleted']:>9}  {state:<9}"
            f"{_human_age(s['age_seconds']):>6}  {'; '.join(notes)}"
        )
    if graph:
        lines.append("")
        if graph.get("ready"):
            lines.append(
                f"graph   build {graph['build_id']} ({_human_age(_age_seconds(graph.get('built_at')))} old): "
                f"{graph['nodes']} nodes, {graph['edges']} edges, {graph['units']} units"
            )
            for src in graph.get("coverage", {}).get("sources", []):
                refs = f", refs {src.get('refs_resolved', 0)}/{src.get('refs', 0)}" if src.get("refs") else ""
                lines.append(
                    f"        {src['id']:<20} {src['items']:>6} items, mentions {src['resolved']}/{src['mentions']}"
                    f" ({src['resolved_ratio']:.0%}), {src['dangling']} dangling{refs}"
                )
                if src.get("code"):
                    lines.append(f"          {code_line(src['code'])}")
        else:
            lines.append(f"graph   {graph.get('error') or 'no ready build'}")
        if status.get("warnings"):
            lines.append("")
            lines += [f"warning: {w}" for w in status["warnings"]]
        multiple = status.get("multiple_definitions")
        if multiple and multiple["codes"]:
            lines.append("")
            lines.append(f"codes defined in two or more places: {multiple['codes']}; {multiple['hint']}")
            for sample in multiple["samples"]:
                lines.append(f"        {sample['code']}: {', '.join(sample['places'])}")
            if multiple["codes"] > len(multiple["samples"]):
                lines.append(f"        and {multiple['codes'] - len(multiple['samples'])} more")
    else:
        lines.append("")
        lines.append("graph   not built: run evo-agents kg build")
    lines.append("")
    lines.append("OK" if status["ok"] else "NOT OK: a source failed, never synced, or has deletions held")
    return "\n".join(lines)
