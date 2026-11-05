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
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from evo_agents.hub import kg_health_eval, tables
from evo_agents.hub import kg_optimize as model
from evo_agents.hub import kg_settings as store
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_graph import ArtifactMismatch, GraphUnavailable, HubSession, open_graph
from evo_agents.hub.server import audit, kg_eval_sets
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import GRAPHS_TRIED, built_graphs, member, read_sink
from evo_agents.hub.server.kg_audience import reader_audience
from evo_agents.hub.server.kg_web import require_reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg import settings as kg_settings

MAX_REASON_CHARS = 1000
MAX_FIGURES_CHARS = store.MAX_FIGURES_CHARS

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 422)},
)


class ChangeOut(BaseModel):
    id: int
    kind: str
    changed_by: str | None
    run_id: int | None
    reason: str | None
    figures: dict | None
    before: dict
    after: dict
    undoes: int | None
    changed_at: datetime


class SettingsOut(BaseModel):
    values: dict = Field(description="every key with the value the project is tuned with")
    overrides: dict = Field(description="the keys that differ from the defaults")
    defaults: dict
    needs_rebuild: list[str] = Field(description="the keys changed by this call that act on the next build")
    changes: list[ChangeOut] = Field(description="the newest changes, newest first")
    can_change: bool


class SetBody(BaseModel):
    values: dict = Field(description="keys of the KG settings and their new values")
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS)
    figures: dict | None = Field(None, description="the numbers the change rests on, as the bench or measure gave them")


class UndoBody(BaseModel):
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS)


async def may_change(conn, user, access) -> None:
    require_reader(access)
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"changing the KG settings of project {access.name} needs the writer role on it")
    run = user.acts_for_run
    if run is not None:
        project = (await conn.execute(select(tables.runs.c.project_id).where(tables.runs.c.id == run))).scalar()
        if project != access.project_id:
            raise HTTPException(403, f"run {run} is not a run of project {access.name}")


async def _out(conn, project_id: int, role, *, needs_rebuild: list[str], before=None) -> SettingsOut:
    tuning = await store.read(conn, project_id)
    found = await store.changes(conn, project_id)
    ids = {c.changed_by for c in found if c.changed_by is not None}
    logins = {}
    if ids:
        rows = await conn.execute(select(tables.users.c.id, tables.users.c.login).where(tables.users.c.id.in_(ids)))
        logins = {r.id: r.login for r in rows}
    return SettingsOut(
        values=tuning.to_json(),
        overrides=tuning.overrides(),
        defaults=kg_settings.DEFAULT.to_json(),
        needs_rebuild=needs_rebuild,
        changes=[
            ChangeOut(
                id=c.id,
                kind=c.kind,
                changed_by=logins.get(c.changed_by),
                run_id=c.run_id,
                reason=c.reason,
                figures=c.figures,
                before=c.before,
                after=c.after,
                undoes=c.undoes,
                changed_at=c.changed_at,
            )
            for c in found
        ],
        can_change=has_role(role, "writer"),
    )


@router.get("/{project}/settings", response_model=SettingsOut)
async def get_settings(request: Request, project: ProjectName, user: CurrentUser) -> SettingsOut:
    """What the project's search and build are tuned with, and the newest changes."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        return await _out(conn, access.project_id, access.role, needs_rebuild=[])


@router.put("/{project}/settings", response_model=SettingsOut)
async def put_settings(request: Request, project: ProjectName, body: SetBody, user: CurrentUser) -> SettingsOut:
    """Set keys of the project's KG settings; the change keeps who made it, why, and the figures it cites."""
    if body.figures is not None and len(repr(body.figures)) > MAX_FIGURES_CHARS:
        raise HTTPException(422, f"figures are over {MAX_FIGURES_CHARS} characters")
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            before, after, made = await store.change(
                conn,
                access.project_id,
                body.values,
                user_id=user.user_id,
                run_id=user.acts_for_run,
                reason=body.reason,
                figures=body.figures,
            )
        except kg_settings.SettingsError as e:
            raise HTTPException(422, str(e)) from e
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.KG_SETTINGS,
            target=f"{project} change={made} kind=set",
            project_id=access.project_id,
        )
        return await _out(conn, access.project_id, access.role, needs_rebuild=kg_settings.rebuild_keys(before, after))


@router.post("/{project}/settings/undo", response_model=SettingsOut)
async def undo_settings(request: Request, project: ProjectName, body: UndoBody, user: CurrentUser) -> SettingsOut:
    """Put back what the newest change that is not undone replaced."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            before, after, made = await store.undo(
                conn, access.project_id, user_id=user.user_id, run_id=user.acts_for_run, reason=body.reason
            )
        except store.NothingToUndo as e:
            raise HTTPException(404, f"project {access.name} has no change of its KG settings to undo") from e
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.KG_SETTINGS,
            target=f"{project} change={made} kind=undo",
            project_id=access.project_id,
        )
        return await _out(conn, access.project_id, access.role, needs_rebuild=kg_settings.rebuild_keys(before, after))


class MeasureBody(BaseModel):
    values: dict = Field(default_factory=dict, description="the candidate's keys, on top of the project's settings")
    split: Literal["tune", "holdout"] = Field("tune", description="the part of the question set that is asked")
    max_questions: int = Field(300, ge=1, le=1000)


class MeasureOut(BaseModel):
    build_id: int | None
    split: str
    questions: int
    current: dict | None = Field(description="the figures with the project's settings (n, hit@5, tokens per hit, p95)")
    candidate: dict | None = Field(description="the figures with the candidate's query keys on top")
    candidate_settings: dict
    needs_rebuild: list[str] = Field(description="keys of the candidate that need a new build and were not measured")
    why: str | None = Field(None, description="why nothing was measured")


def _measure(cache, blobs, project, access, sink, audience, graphs, found_set, current, candidate, split, limit):
    """The split of ``found_set`` asked of the newest graph with ``current`` and with ``candidate``. Blocking."""
    from evo_agents.kg.evaluate import HOLDOUT, TUNE

    question_set = found_set[0] if found_set else None
    if question_set is None:
        return {"why": "the project has no eval set yet"}
    part = question_set.part(TUNE if split == "tune" else HOLDOUT)
    questions = [q for q in part if q.project in (None, project)][:limit]
    if not questions:
        return {"why": f"the project's eval set has no {split} question"}
    ceiling = access.rules.ceiling(access.max_level, sink, audience)
    path, graph = cache.acquire(blobs, project, graphs)
    try:
        opened = open_graph(path, project, graph)
        try:
            session = HubSession(project, access.rules.policy, ceiling, sink, opened, graph, graphs[0])

            def asked(tuning):
                opened.tuning = tuning
                session.semantic = tuning.semantic_mode == "on" and opened.vector_info() is not None
                found = kg_health_eval.score(questions, [], 0, session, plan_steps=0, sink=sink)
                return model.figures((found.get("methods") or {}).get("kg_search"))

            return {
                "build_id": graph.build_id,
                "questions": len(questions),
                "current": asked(current),
                "candidate": asked(candidate),
            }
        finally:
            opened.close()
    finally:
        cache.release(path)


@router.post("/{project}/measure", response_model=MeasureOut)
async def measure(request: Request, project: ProjectName, body: MeasureBody, user: CurrentUser) -> MeasureOut:
    """Score the project's settings and a candidate on its question set; nothing is stored or changed."""
    state = request.app.state
    async with state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        member(access)
        current = await store.read(conn, access.project_id)
        try:
            wanted = kg_settings.of(body.values, current)
        except kg_settings.SettingsError as e:
            raise HTTPException(422, str(e)) from e
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        found_set = await kg_eval_sets.latest_set(conn, access.project_id) if graphs else None
        audience = await reader_audience(conn, user) if graphs else frozenset()
    needs = kg_settings.rebuild_keys(current, wanted)
    # what a graph already built can be asked with: the query keys of the candidate; its build keys stay as they are
    kept = {k: getattr(wanted, k) for k in kg_settings.KEYS if k not in kg_settings.BUILD_KEYS}
    if "semantic_mode" in needs:
        kept["semantic_mode"] = current.semantic_mode
    candidate = kg_settings.of(kept, current)
    empty = {"build_id": None, "split": body.split, "questions": 0, "current": None, "candidate": None}
    out = dict(empty, candidate_settings=candidate.to_json(), needs_rebuild=needs)
    if not graphs:
        return MeasureOut(**out, why=f"project {project} has no graph on the hub yet")
    try:
        found = await asyncio.to_thread(
            _measure,
            state.kg_graphs,
            state.blobs,
            project,
            access,
            read_sink(access, "cli"),
            audience,
            graphs,
            found_set,
            current,
            candidate,
            body.split,
            body.max_questions,
        )
    except (GraphUnavailable, ArtifactMismatch) as e:
        return MeasureOut(**out, why=str(e))
    if "why" in found:
        return MeasureOut(**out, why=found["why"])
    return MeasureOut(**{**out, **found})
