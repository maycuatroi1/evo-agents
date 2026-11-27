"""The tool curator_evolve of the hub's /mcp (``evo_agents.hub.server.mcp``): each evolution of a project, or one with
its select tasks, candidates, scores and front, as text and data."""

from __future__ import annotations

from fastapi import Request

from evo_agents.hub import curator
from evo_agents.hub.server.evolve.models import CandidateView, EvolveView
from evo_agents.hub.server.evolve.service.views import candidates_of, evolution_of, evolutions_of, view_of
from evo_agents.hub.server.forge.service.protection import need_reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal


def _candidate_line(item: CandidateView) -> str:
    cost = curator.money(item.cost_usd) if item.cost_usd is not None else "cost -"
    scores = ", ".join(f"{ref} {value:g}" for ref, value in sorted(item.scores.items())) or "no score"
    parents = f" of {', '.join(f'#{p}' for p in item.parents)}" if item.parents else ""
    front = " (on the front)" if item.on_front else ""
    return (
        f"#{item.id} {item.origin}{parents}, round {item.iteration}{front}: {scores}; {cost}, {item.turns or 0} turns"
    )


def _evolution_line(view: EvolveView) -> str:
    what = f"{view.repo} {', '.join(view.paths)}" if view.target == "git" else f"agent {view.agent}"
    best = f", best #{view.best_candidate_id}" if view.best_candidate_id else ""
    why = f": {view.reason}" if view.reason else ""
    return f"#{view.id} {view.target} of {what} ({view.state}), {view.iterations} round(s){best}{why}"


async def mcp_answer(request: Request, project: str, user: Principal, arguments: dict, *, sink: str | None):
    """(text, data) of the MCP tool curator_evolve: each evolution, or one with its select tasks, candidates, scores
    and front. Its tasks are of the select split and probes alone, and never with their verify; no candidate's
    components are in it."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        if "evolve_id" in arguments:
            row = await evolution_of(conn, access, arguments["evolve_id"], sink)
            return _one_answer(view_of(access.name, row, await candidates_of(conn, row.id, components=False)))
        rows = await evolutions_of(conn, access, sink, state=arguments.get("state"), limit=arguments.get("limit", 20))
        return _list_answer(project, [view_of(access.name, row) for row in rows])


def _list_answer(project: str, views: list[EvolveView]) -> tuple[str, dict]:
    lines = [f"The evolutions of project {project}: {len(views)}."] + [_evolution_line(view) for view in views]
    data = {"project": project, "evolutions": [view.model_dump(mode="json") for view in views]}
    return "\n".join(lines), {**data, "summary": f"{len(views)} evolutions"}


def _one_answer(view: EvolveView) -> tuple[str, dict]:
    lines = [f"Evolution {_evolution_line(view)}."]
    lines.append("Tasks (select split and probes): " + (", ".join(task.ref for task in view.tasks) or "none"))
    lines += [_candidate_line(item) for item in view.candidates or []]
    front = ", ".join(f"#{item.candidate_id} leads {item.freq}" for item in view.frontier) or "none yet"
    lines.append(f"Front: {front}.")
    data = view.model_dump(mode="json", exclude={"candidates": {"__all__": {"components"}}})
    return "\n".join(lines), {**data, "summary": f"{len(view.candidates or [])} candidates"}
