"""The per-project settings of the knowledge graph's search and build (``evo_agents.kg.settings``).

- GET /v1/kg/{project}/settings: what the project is tuned with (every key), which keys it overrides, the last changes.
- PUT /v1/kg/{project}/settings {values, reason, figures}: a writer, or the agent of a run of this project, sets keys on
  top of what the project has. A key that acts on a query holds at once; a key that acts on a build waits for the next
  build (``needs_rebuild`` lists them). An unknown key or a value out of range is a 422 and nothing is written.
- POST /v1/kg/{project}/settings/undo {reason}: puts back what the newest change that is not undone replaced.

- POST /v1/kg/{project}/measure {values, split, max_questions}: the kg_search score of the project's question set (its
  ``tune`` split by default, or ``holdout``) on the newest graph with the project's own settings and with ``values`` on
  top of them, nothing stored. Only the keys that act on a query can be tried on a graph already built; the others come
  back in ``needs_rebuild`` and are left out of the candidate. Read through the caller's own audience.

Every change is a row with its author, its reason and the figures it cites, so it can be reviewed and put back.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from evo_agents.hub import kg_health_eval
from evo_agents.hub import kg_optimize as model
from evo_agents.hub.kg_graph import GraphUnavailable, HubSession, reading
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.settings import MAX_REASON_CHARS as MAX_REASON_CHARS
from evo_agents.hub.server.kg.models.settings import (
    MeasureBody,
    MeasureOut,
    SetBody,
    SettingsOut,
    UndoBody,
)
from evo_agents.hub.server.kg.service import settings as service
from evo_agents.hub.server.kg.service.tools import read_sink, snapshot_view
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 422)},
)


@router.get("/{project}/settings", response_model=SettingsOut)
async def get_settings(request: Request, project: ProjectName, user: CurrentUser) -> SettingsOut:
    """What the project's search and build are tuned with, and the newest changes."""
    return await service.read_settings(request.app.state.engine, user, project)


@router.put("/{project}/settings", response_model=SettingsOut)
async def put_settings(request: Request, project: ProjectName, body: SetBody, user: CurrentUser) -> SettingsOut:
    """Set keys of the project's KG settings; the change keeps who made it, why, and the figures it cites."""
    return await service.set_settings(request.app.state.engine, user, project, body)


@router.post("/{project}/settings/undo", response_model=SettingsOut)
async def undo_settings(request: Request, project: ProjectName, body: UndoBody, user: CurrentUser) -> SettingsOut:
    """Put back what the newest change that is not undone replaced."""
    return await service.undo_settings(request.app.state.engine, user, project, body.reason)


def _by_type(found: dict) -> dict:
    """The kg_search figures of each question type of an eval's ``found``: aggregates only, never a question."""
    rows = found.get("by_type") or {}
    return {t: fig for t, row in rows.items() if (fig := model.figures((row or {}).get("kg_search")))}


def _measure(project, access, sink, audience, graphs, found_set, current, candidate, split, limit, snapshot=None):
    """The split of ``found_set`` asked of the newest graph with ``current`` and with ``candidate``: ``snapshot``, the
    snapshot of the hub's tables, when given, else the graph file (``kg_graph.reading``). Blocking."""
    from evo_agents.kg.evaluate import HOLDOUT, TUNE

    question_set = found_set[0] if found_set else None
    if question_set is None:
        return {"why": "the project has no eval set yet"}
    part = question_set.part(TUNE if split == "tune" else HOLDOUT)
    questions = [q for q in part if q.project in (None, project)][:limit]
    if not questions:
        return {"why": f"the project's eval set has no {split} question"}
    ceiling = access.rules.ceiling(access.max_level, sink, audience)
    with reading(project, snapshot) as (opened, graph):
        session = HubSession(project, access.rules.policy, ceiling, sink, opened, graph, graphs[0])

        def asked(tuning):
            opened.tuning = tuning
            session.semantic = tuning.semantic_mode == "on" and opened.vector_info() is not None
            found = kg_health_eval.score(questions, [], 0, session, plan_steps=0, sink=sink)
            return model.figures((found.get("methods") or {}).get("kg_search")), _by_type(found)

        now, now_types = asked(current)
        then, then_types = asked(candidate)
        by_type = {
            t: {"current": now_types.get(t), "candidate": then_types.get(t)}
            for t in sorted(set(now_types) | set(then_types))
        }
        return {
            "build_id": graph.build_id,
            "questions": len(questions),
            "current": now,
            "candidate": then,
            "by_type": by_type or None,
        }


@router.post("/{project}/measure", response_model=MeasureOut)
async def measure(request: Request, project: ProjectName, body: MeasureBody, user: CurrentUser) -> MeasureOut:
    """Score the project's settings and a candidate on its question set; nothing is stored or changed."""
    state = request.app.state
    found = await service.measuring(state.engine, user, project, body.values)
    access, graphs, audience = found.access, found.graphs, found.audience
    empty = {"build_id": None, "split": body.split, "questions": 0, "current": None, "candidate": None, "by_type": None}
    out: dict[str, Any] = dict(empty, candidate_settings=found.candidate.to_json(), needs_rebuild=found.needs_rebuild)
    if not graphs:
        return MeasureOut(**out, why=f"project {project} has no graph on the hub yet")
    sink = read_sink(access, "cli")
    snapshot = await snapshot_view(
        state, access.project_id, graphs, access.rules.ceiling(access.max_level, sink, audience)
    )
    try:
        measured = await asyncio.to_thread(
            _measure,
            project,
            access,
            sink,
            audience,
            graphs,
            found.found_set,
            found.current,
            found.candidate,
            body.split,
            body.max_questions,
            snapshot,
        )
    except GraphUnavailable as e:
        return MeasureOut(**out, why=str(e))
    if "why" in measured:
        return MeasureOut(**out, why=measured["why"])
    return MeasureOut(**{**out, **measured})
