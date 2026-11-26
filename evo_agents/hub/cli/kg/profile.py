"""``evo-agents hub kg profile``: the data profile of a project's newest graph on the hub.

Every build writes a profile into its coverage (``evo_agents.kg.profile``): counts and shapes of what the corpus holds,
never a value. The hub answers it inside kg_status (``graph.coverage.profile``) to a caller whose audience holds every
audience of the graph; a caller who does not hold them gets a coverage without it, and this command says so. ``--json``
prints the project, the build and the profile. Standard library only.

:data:`PROFILE` is a class command of ``evo_agents.clikit``; it asks kg_status through
:class:`~evo_agents.hub.cli.kg.resources.KgTools`.
"""

from __future__ import annotations

from evo_agents.clikit import Result, Text, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, SINK, GraphUnread, KgTools, unread


def read_profile(hub, project: str, sink: str | None = None) -> tuple[int | None, dict | None, str | None]:
    """(build id, profile, why not) of ``project``'s newest graph, as ``sink`` (the agent session's by default) reads it."""
    found, why = KgTools(hub, project).status(sink)
    if why is not None:
        return None, None, why
    graph = found.get("graph") or {}
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


class KgProfile(HubCommand):
    name = "profile"
    help = "the data profile of a project's newest graph: what its corpus is made of, counts and shapes"
    arguments = (KG_PROJECT, SINK)
    output = returns_object("project", "build_id", "profile")

    def run(self, ctx: HubContext) -> Result:
        from evo_agents.kg.profile import render

        build_id, profile, why = read_profile(ctx.hub, ctx.project, ctx.args.sink)
        if profile is None:
            raise GraphUnread(why)
        found = profile_json(ctx.project, build_id, profile)
        if ctx.args.json:  # the text needs a whole profile; --json prints whatever the hub kept
            return Result(found)
        return Result(found, Text(f"project {ctx.project}, build {build_id}", render(profile)))

    def failed(self, exc: Exception) -> int | None:
        return unread(exc) or super().failed(exc)


PROFILE = KgProfile()
NODES = (PROFILE,)
