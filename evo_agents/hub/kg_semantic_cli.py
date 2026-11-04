"""``evo-agents hub kg semantic``: whether a project's hub graph holds vectors, as its newest build recorded.

Every build with the project's mode at ``index`` or ``on`` writes what it embedded into its coverage
(``coverage.vectors``: model, mode, rows with a body, rows with a vector, partial). The hub loads the model itself, so
``model_available`` is whether that build could: false when it reports an error, such as a model not yet downloaded.
Standard library only.
"""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in
from evo_agents.hub.contract import json_option, returns_object


def read_vectors(hub, project: str, sink: str | None = None) -> tuple[int | None, dict | None, str | None]:
    """(build id, the vectors of its coverage or {} for a build without, why not)."""
    body = {"arguments": {}, **({"sink": sink} if sink else {})}
    found = hub.call("POST", f"/v1/kg/{quote(project, safe='')}/tools/kg_status", body)
    if found.get("isError"):
        text = " ".join(block.get("text", "") for block in found.get("content", []))
        return None, None, text.removeprefix("error: ") or "the hub refused kg_status"
    graph = (found.get("structuredContent") or {}).get("graph") or {}
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


@_client_command
def cmd_semantic(args) -> int:
    hub, _ = _signed_in()
    build_id, vectors, why = read_vectors(hub, args.project, args.sink)
    if vectors is None:
        print(f"error: {why}")
        return 1
    found = semantic_json(args.project, build_id, vectors)
    if args.json:
        _print_json(found)
        return 0
    print(f"project {args.project}, build {build_id}: mode {found['mode']}, model {found['model'] or '-'}")
    if found["mode"] == "off":
        print("no vectors: this project's builds search by words alone")
    elif found["error"]:
        print(f"model not available on the hub: {found['error']}")
    else:
        print(
            f"{found['vectors']} of {found['rows']} rows hold a vector"
            + (" (the next build goes on)" if found["partial"] else "")
        )
    return 0


def register(ksub) -> None:
    parser = ksub.add_parser(
        "semantic", help="whether a project's newest graph holds vectors: mode, model, model available, coverage"
    )
    parser.add_argument("--project", required=True, help="project name on the hub")
    parser.add_argument("--sink", help="sink whose clearance applies (default: the agent session's)")
    json_option(
        parser,
        returns_object(
            "project", "build_id", "mode", "model", "model_available", "rows", "vectors", "coverage", "partial", "error"
        ),
    )
    parser.set_defaults(func=cmd_semantic)
