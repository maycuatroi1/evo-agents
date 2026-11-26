"""``evo-agents hub kg semantic``: whether a project's hub graph holds vectors, as its newest build recorded.

Every build with the project's mode at ``index`` or ``on`` writes what it embedded into its coverage
(``coverage.vectors``: model, mode, rows with a body, rows with a vector, partial). The hub loads the model itself, so
``model_available`` is whether that build could: false when it reports an error, such as a model not yet downloaded.
Standard library only.

:data:`SEMANTIC` is a class command of ``evo_agents.clikit``; it asks kg_status through
:class:`~evo_agents.hub.cli.kg.resources.KgTools`.
"""

from __future__ import annotations

from evo_agents.clikit import Result, Text, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, SINK, GraphUnread, KgTools, unread


def read_vectors(hub, project: str, sink: str | None = None) -> tuple[int | None, dict | None, str | None]:
    """(build id, the vectors of its coverage or {} for a build without, why not)."""
    found, why = KgTools(hub, project).status(sink)
    if why is not None:
        return None, None, why
    graph = found.get("graph") or {}
    if not graph.get("ready"):
        return None, None, f"project {project} has no ready graph on the hub"
    vectors = (graph.get("coverage") or {}).get("vectors")
    return graph.get("build_id"), vectors if isinstance(vectors, dict) else {}, None


def semantic_json(project: str, build_id: int | None, vectors: dict) -> dict:
    """What ``--json`` prints: the mode and model of the newest build, whether the hub could load the model, and how
    many rows hold a vector."""
    rows, held = int(vectors.get("rows") or 0), int(vectors.get("vectors") or 0)
    return {
        "project": project,
        "build_id": build_id,
        "mode": vectors.get("mode") or "off",
        "model": vectors.get("model"),
        "model_available": bool(vectors) and not vectors.get("error"),
        "rows": rows,
        "vectors": held,
        "coverage": round(held / rows, 4) if rows else None,
        "partial": bool(vectors.get("partial")),
        "error": vectors.get("error"),
    }


def semantic_lines(found: dict) -> list[str]:
    """What a person reads of :func:`semantic_json`."""
    lines = [
        f"project {found['project']}, build {found['build_id']}: mode {found['mode']}, model {found['model'] or '-'}"
    ]
    if found["mode"] == "off":
        lines.append("no vectors: this project's builds search by words alone")
    elif found["error"]:
        lines.append(f"model not available on the hub: {found['error']}")
    else:
        partial = " (the next build goes on)" if found["partial"] else ""
        lines.append(f"{found['vectors']} of {found['rows']} rows hold a vector{partial}")
    return lines


class KgSemantic(HubCommand):
    name = "semantic"
    help = "whether a project's newest graph holds vectors: mode, model, model available, coverage"
    arguments = (KG_PROJECT, SINK)
    output = returns_object(
        "project", "build_id", "mode", "model", "model_available", "rows", "vectors", "coverage", "partial", "error"
    )

    def run(self, ctx: HubContext) -> Result:
        build_id, vectors, why = read_vectors(ctx.hub, ctx.project, ctx.args.sink)
        if vectors is None:
            raise GraphUnread(why)
        found = semantic_json(ctx.project, build_id, vectors)
        return Result(found) if ctx.args.json else Result(found, Text(*semantic_lines(found)))

    def failed(self, exc: Exception) -> int | None:
        return unread(exc) or super().failed(exc)


SEMANTIC = KgSemantic()
NODES = (SEMANTIC,)
