"""``evo-agents hub kg profile``: the data profile of a project's newest graph on the hub.

Every build writes a profile into its coverage (``evo_agents.kg.profile``): counts and shapes of what the corpus holds,
never a value. The hub answers it inside kg_status (``graph.coverage.profile``) to a caller whose audience holds every
audience of the graph; a caller who does not hold them gets a coverage without it, and this command says so. ``--json``
prints the project, the build and the profile. Standard library only.
"""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in
from evo_agents.hub.contract import json_option, returns_object


def read_profile(hub, project: str, sink: str | None = None) -> tuple[int | None, dict | None, str | None]:
    """(build id, profile, why not) of ``project``'s newest graph, as ``sink`` (the agent session's by default) reads it."""
    body = {"arguments": {}, **({"sink": sink} if sink else {})}
    found = hub.call("POST", f"/v1/kg/{quote(project, safe='')}/tools/kg_status", body)
    if found.get("isError"):
        text = " ".join(block.get("text", "") for block in found.get("content", []))
        return None, None, text.removeprefix("error: ") or "the hub refused kg_status"
    graph = (found.get("structuredContent") or {}).get("graph") or {}
    profile = (graph.get("coverage") or {}).get("profile")
    if isinstance(profile, dict):
        return graph.get("build_id"), profile, None
    if not graph.get("ready"):
        return None, None, f"project {project} has no ready graph on the hub"
    return (
        graph.get("build_id"),
        None,
        "the newest build holds no profile: it was made before profiles, or the graph has items outside your audience",
    )


def profile_json(project: str, build_id: int | None, profile: dict) -> dict:
    """What ``--json`` prints."""
    return {"project": project, "build_id": build_id, "profile": profile}


@_client_command
def cmd_profile(args) -> int:
    from evo_agents.kg.profile import render

    hub, _ = _signed_in()
    build_id, profile, why = read_profile(hub, args.project, args.sink)
    if profile is None:
        print(f"error: {why}")
        return 1
    if args.json:
        _print_json(profile_json(args.project, build_id, profile))
    else:
        print(f"project {args.project}, build {build_id}")
        print(render(profile))
    return 0


def register(ksub) -> None:
    parser = ksub.add_parser(
        "profile", help="the data profile of a project's newest graph: what its corpus is made of, counts and shapes"
    )
    parser.add_argument("--project", required=True, help="project name on the hub")
    parser.add_argument("--sink", help="sink whose clearance applies (default: the agent session's)")
    json_option(parser, returns_object("project", "build_id", "profile"))
    parser.set_defaults(func=cmd_profile)
