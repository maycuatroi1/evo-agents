"""The kg_init run of a project (plan kg-data-profile-curator, criterion a5): the Curator configures the project's
knowledge graph from its own data profile, without waiting for a person. A run is a row of kg_init_runs; the job
curator.collect moves it on each minute (``advance``):

- ``queued``: asked for by the first successful build of a project with a knowledge config, by a profile that changed a
  lot since the last run's (``evo_agents.hub.kg_init_db.on_build``), or by a call (POST /v1/kg/{project}/init,
  ``evo-agents hub kg init --project P``; POST /v1/admin/kg-init for every project, an admin's). A project with no eval
  set, or only its commits' set with under 30 questions, first gets a review run with lens kg_questions that writes the
  starter questions, waited for until it ends or for two hours (``evo_agents.hub.server.kg_questions``). The hub measures the
  starter questions (the tune split of the project's eval set, by words and by meaning when the graph has vectors),
  writes the prompt (the profile, the build's coverage, the strategies, the measures, the optimize loop's last figures)
  and queues a review run with lens kg_init on the project's default agent pool, under the charter's review cost cap
  (``kg_init.HUB_CAP_USD`` without a charter). No night shift is needed. The run stays queued, and says why in
  ``body["notes"]``, while no node of the pool fits it or another review run of the project is active.
- ``running``: the agent decides and applies. It records its decision with POST /v1/kg/{project}/init/{id}/decision
  (``evo-agents hub kg init --decide FILE``): the hub validates it (``evo_agents.kg.init_run.parse_decision``: each
  part cites figures), sets the strategy and the settings with their reasons and figures in the settings' history, and
  queues a rebuild when a setting needs one. Sources and changes to the KG code, a dependency or the worker's runtime go
  through the proposal flow (kg_propose); the decision keeps them with their reasons.
- ``done`` once the review run ended with a decision, ``failed`` once it ended without one.

The prompt holds counts, shapes and figures, never the text or the name of an item: what a sink may not see cannot
reach a prompt. One run of a project is in flight at a time: a second call returns the first. Limits on the hub: the
candidate strategies are not measured here (the figures are those of the current settings), a strategy the Curator
creates sets its settings and keeps its identifiers, ontology and steps as pending for the proposal flow, and the
``strategy`` setting is set to built-in strategies only, as the worker that builds cannot read a file created here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, null, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, kg_health_eval, kg_init, kg_init_db, kg_settings, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build import QueueBusy, kg_root, queue_build
from evo_agents.hub.kg_graph import ArtifactMismatch, GraphCache, GraphUnavailable, HubSession, open_graph
from evo_agents.hub.server import audit, kg_eval_sets, kg_health, kg_questions
from evo_agents.hub.server.admin import ProjectName, admin
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import built_graphs
from evo_agents.hub.server.kg_audience import agent_audience
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.tables_kg import INIT_ACTIVE
from evo_agents.kg import init_run
from evo_agents.kg import settings as tuning
from evo_agents.kg import strategy as kg_strategy
from evo_agents.kg.evaluate import TUNE

log = logging.getLogger(__name__)

CACHE = "init"  # the directory of the worker's cache (``kg_root``) the measured graphs are fetched into
MAX_TUNE = 300  # starter questions asked
QUEUE_WAIT = timedelta(hours=24)  # a run that no node could take in this long fails
MAX_LISTED = 10
KG_INIT = "kg.init"  # the audit action of an asked-for run
WITHHELD = "(text withheld)"

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})
admin_router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(admin)], responses={401: {"model": ErrorBody}}
)


# Measuring


def _starter(cache: GraphCache, blobs, project: str, access, sink: str, graphs, found_set, audience) -> dict:
    """The tune split of ``found_set`` asked of the latest graph by words and by meaning, read with ``audience``.
    Blocking: run it in a thread. {"why": ...} when it cannot; the holdout is never asked."""
    question_set = found_set[0] if found_set else None
    questions = [q for q in (question_set.part(TUNE) if question_set else []) if q.project in (None, project)]
    if not questions:
        return {"why": "the project's eval set has no tune question"}
    questions = questions[:MAX_TUNE]
    ceiling = access.rules.ceiling(access.max_level, sink, audience)
    path, graph = cache.acquire(blobs, project, graphs)
    try:
        store = open_graph(path, project, graph)
        try:
            if store.latest_ready() is None:
                return {"why": f"build {graph.build_id} holds no ready graph"}
            session = HubSession(project, access.rules.policy, ceiling, sink, store, graph, graphs[0])

            def asked(*, meaning: bool):
                session.semantic = meaning
                found = kg_health_eval.score(questions, [], 0, session, plan_steps=0, sink=sink)
                return init_run.figures((found.get("methods") or {}).get("kg_search"))

            found = {"build_id": graph.build_id, "words": asked(meaning=False), "meaning": None}
            if store.vector_info() is not None:
                try:
                    found["meaning"] = asked(meaning=True)
                except Exception as exc:  # no model here: the figures by words stand
                    found["why_meaning"] = f"{type(exc).__name__}: {exc}"[:200]
            return found
        finally:
            store.close()
    finally:
        cache.release(path)


async def measure_starter(engine: AsyncEngine, row, blobs, data_dir: Path | None) -> dict:
    """The starter measure of ``row``'s project (project_id, project, owner_id, owner), as its owner's agents read the
    graph; {"why": ...} when it cannot."""
    async with engine.begin() as conn:
        graphs = await built_graphs(conn, row.project_id, kg_health.GRAPHS_TRIED)
        access, sink, why = await kg_health._owner_reads(conn, row, blobs, data_dir, graphs, agent=True)
        if why is not None:
            return {"why": why}
        found_set = await kg_eval_sets.latest_set(conn, row.project_id)
        audience = await agent_audience(conn, row.owner_id, None, None)
    cache = GraphCache(kg_root(data_dir) / CACHE, keep=1)
    try:
        return await asyncio.to_thread(_starter, cache, blobs, row.project, access, sink, graphs, found_set, audience)
    except (GraphUnavailable, ArtifactMismatch) as exc:
        return {"why": str(exc)}
    except Exception as exc:  # a run goes on without figures
        log.exception("the starter measure of a kg init run failed", extra={"project": row.project})
        return {"why": f"the measure failed: {type(exc).__name__}"}


Measure = Callable[[AsyncEngine, object, object, "Path | None"], Awaitable[dict]]


# The rows


async def _set(conn: AsyncConnection, run_id: int, **values) -> None:
    t = tables.kg_init_runs
    await conn.execute(update(t).where(t.c.id == run_id).values(**values, updated_at=func.now()))


def _context():
    """The runs in flight with their project's name."""
    t, p = tables.kg_init_runs, tables.projects
    return select(t, p.c.name.label("project")).join_from(t, p, p.c.id == t.c.project_id)


async def _latest_build(conn: AsyncConnection, project_id: int):
    b = tables.kg_builds
    query = (
        select(b.c.id, b.c.coverage)
        .where(b.c.project_id == project_id, b.c.status == "succeeded")
        .order_by(b.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def _asker(conn: AsyncConnection, ctx) -> tuple[int, str] | None:
    """(id, login) of the user the review run is dispatched by: who asked, else the first admin of the project."""
    u, g = tables.users, tables.grants
    if ctx.requested_by is not None:
        row = (await conn.execute(select(u.c.id, u.c.login).where(u.c.id == ctx.requested_by))).first()
        if row is not None:
            return row.id, row.login
    query = (
        select(u.c.id, u.c.login)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == ctx.project_id, g.c.role == "admin")
        .order_by(u.c.id)
        .limit(1)
    )
    row = (await conn.execute(query)).first()
    return None if row is None else (row.id, row.login)


async def _last_optimize(conn: AsyncConnection, project_id: int) -> dict | None:
    """What the newest ended KG optimize run measured before and after, for the Curator to decide keep or revert."""
    o = tables.kg_optimize_runs
    row = (
        await conn.execute(
            select(o.c.id, o.c.state, o.c.body, o.c.asked_weight)
            .where(o.c.project_id == project_id, o.c.ended_at.is_not(None), o.c.state.in_(("done", "reverted")))
            .order_by(o.c.id.desc())
            .limit(1)
        )
    ).first()
    if row is None or not isinstance(row.body, dict):
        return None
    body = row.body
    handed = body.get("handover") or {}
    changes = [f"run #{row.id} ended {row.state}"]
    for key in ("semantic", "asked_weight", "regressions", "decisions", "pulls"):
        if handed.get(key):
            changes.append(f"{key}: {handed[key]}")
    return {"before": handed.get("before"), "after": handed.get("after"), "changes": changes}


def _coverage_text(coverage) -> dict:
    """The build's coverage for a prompt: counts; the text of any error or warning, which can name an item, is withheld."""
    found = coverage if isinstance(coverage, dict) else {}
    return {
        "errors": [WITHHELD] * int(len(found.get("errors") or [])),
        "warnings": [WITHHELD] * int(len(found.get("warnings") or [])),
        "dangling_edges": found.get("dangling_edges", 0),
    }


async def _queue_agent(
    conn: AsyncConnection, ctx, body: dict, measures: dict | None, lens: str = "kg_init"
) -> int | None:
    """Queue the review run of ``ctx`` through ``lens`` (kg_init, or kg_questions for the run that writes its starter
    questions first); its id, or None with why in ``body["notes"]``."""
    from evo_agents.hub.server.collect import review_place
    from evo_agents.hub.server.night_duty import on_duty
    from evo_agents.hub.server.run_state import notify_queued, write_event
    from evo_agents.hub.server.runs.service.dispatch import insert_run

    who = await _asker(conn, ctx)
    if who is None:
        body.setdefault("notes", []).append("no review run: the project has no admin to dispatch it as")
        return None
    c = tables.charters
    charter = (
        await conn.execute(
            select(c.c.body).where(c.c.project_id == ctx.project_id).order_by(c.c.revision.desc()).limit(1)
        )
    ).scalar_one_or_none() or {}
    due = SimpleNamespace(project_id=ctx.project_id, project=ctx.project)
    duty, why = await on_duty(conn, due)
    if duty is None:
        body.setdefault("notes", []).append(f"no review run: {why}")
        return None
    runtime = (charter.get("reviewer") or {}).get("runtime") or curator.DEFAULT_RUNTIME
    repos, problems = await review_place(conn, due, duty, runtime, repoless=True)
    if problems:
        body.setdefault("notes", []).append("no review run: " + "; ".join(problems))
        return None
    caps = kg_init.budget(charter)
    if caps["max_usd"] < curator.MIN_RUN_USD:
        body.setdefault("notes", []).append("no review run: the review cost cap leaves no money")
        return None
    values = {
        "kind": "review",
        "project_id": ctx.project_id,
        "plan_id": None,
        "title": f"KG init of {ctx.project}" if lens == "kg_init" else f"KG starter questions of {ctx.project}",
        "plan_revision": None,
        "dispatched_by": who[0],
        "dispatched_via": "machine",
        "pinned_worker_id": None,
        "selector": duty.columns()["selector"],
        "tolerations": duty.columns()["tolerations"],
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": (charter.get("reviewer") or {}).get("model"),
        "mode": "headless",
        "approval": "auto",
        "timeout_s": caps["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "repos": repos or null(),  # SQL NULL: a project fed by uploads alone has no repo (schema 0202)
        "budget": caps,
    }
    try:
        async with conn.begin_nested():
            run_id = await insert_run(conn, values)
    except IntegrityError as exc:
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):
            body.setdefault("notes", []).append("no review run: another review run of the project is active")
            return None
        raise
    text = (
        f"Queued for KG init run #{ctx.id} of project {ctx.project}, {kg_init.CALLED.format(who=who[1])}: lens "
        f"{lens}; cost cap {curator.money(caps['max_usd'])}, {caps['max_turns']} turns, "
        f"{caps['max_seconds'] // 60} minutes of agent time."
    )
    await write_event(conn, run_id, {"text": text, "init_run_id": ctx.id, "lenses": [lens], **caps})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=who[0],
        token_id=None,
        action=KG_INIT,
        target=f"{ctx.project} run:{run_id} init={ctx.id}" + ("" if lens == "kg_init" else f" lens={lens}"),
        project_id=ctx.project_id,
    )
    return run_id


async def _queued(engine: AsyncEngine, ctx, blobs, data_dir, measure: Measure, now: datetime, count=None) -> str:
    """A run not yet given to an agent: wait for the run that writes its starter questions when the project needs one
    (``kg_questions.phase``), measure, write the prompt, queue the review run."""
    async with engine.begin() as conn:
        latest = await _latest_build(conn, ctx.project_id)
    if latest is None:
        raise InitFailed("the project has no successful build to profile")
    profile = ctx.profile or (latest.coverage or {}).get("profile")
    if not isinstance(profile, dict) or not profile:
        raise InitFailed("the latest build holds no data profile: build the project again")
    body = dict(ctx.body or {})
    if body.get("measures") is None:
        async with engine.begin() as conn:
            asker = await _asker(conn, ctx)
        row = SimpleNamespace(
            project_id=ctx.project_id,
            project=ctx.project,
            owner_id=asker[0] if asker else None,
            owner=asker[1] if asker else None,
        )
        waits, body = await kg_questions.phase(
            engine,
            ctx,
            body,
            row if asker else None,
            now,
            blobs=blobs,
            data_dir=data_dir,
            count=count,
            queue=_queue_agent,
        )
        if waits:
            return "questions"
        found = await measure(engine, row, blobs, data_dir) if asker else {"why": "no admin to read the graph as"}
        if found.get("why"):
            body["measures"] = {}
            body.setdefault("notes", []).append(f"no starter figures: {found['why']}")
        else:
            body["measures"] = {"words": found.get("words"), "meaning": found.get("meaning")}
    async with engine.begin() as conn:
        settings = await kg_settings.read(conn, ctx.project_id)
        optimize = await _last_optimize(conn, ctx.project_id)
        body["prompt"] = kg_init.prompt(
            project=ctx.project,
            run_id=ctx.id,
            why=list(ctx.why or []),
            profile=profile,
            coverage=_coverage_text(latest.coverage),
            strategies=[s.to_json() for s in kg_strategy.builtins().values()],
            measures=body["measures"] or None,
            settings=settings.to_json(),
            optimize=optimize,
        )
        body["notes"] = [n for n in body.get("notes") or [] if not n.startswith("no review run")]
        agent_run_id = await _queue_agent(conn, ctx, body, body["measures"])
        if agent_run_id is None:
            if ctx.created_at + QUEUE_WAIT < now:
                raise InitFailed("no review run could be queued in " + str(QUEUE_WAIT))
            await _set(conn, ctx.id, body=body, build_id=latest.id, profile=profile)
            return "waiting"
        await _set(
            conn, ctx.id, state="running", body=body, build_id=latest.id, profile=profile, agent_run_id=agent_run_id
        )
    return "queued"


async def _running(engine: AsyncEngine, ctx) -> str:
    r = tables.runs
    async with engine.begin() as conn:
        state = await conn.scalar(select(r.c.state).where(r.c.id == ctx.agent_run_id)) if ctx.agent_run_id else None
        if state in runs.ACTIVE_STATES:
            return "running"
        body = dict(ctx.body or {})
        decided = bool(body.get("decision"))
        error = None if decided else f"the review run ended ({state or 'gone'}) without a decision"
        await _end(conn, ctx, body, "done" if decided else "failed", error)
    return "done" if decided else "failed"


async def _end(conn: AsyncConnection, ctx, body: dict, state: str, error: str | None) -> None:
    text = kg_init.report(ctx.project, ctx.id, list(ctx.why or []), state, body, error)
    await _set(
        conn,
        ctx.id,
        state=state,
        body=body,
        report=text,
        error=error and error[: kg_init.ERROR_CHARS],
        ended_at=func.now(),
    )


class InitFailed(Exception):
    pass


async def advance(
    engine: AsyncEngine,
    *,
    blobs=None,
    data_dir=None,
    now: datetime | None = None,
    measure: Measure | None = None,
    count: kg_questions.Count | None = None,
) -> dict:
    """Move each kg_init run in flight one phase on (see the module), each on its own; how many did what. ``measure``
    and ``count`` stand for the reads of the graph (the starter measure, what the questions run's sink reads)."""
    measure = measure or measure_starter
    t = tables.kg_init_runs
    async with engine.begin() as conn:
        moment = now or await conn.scalar(select(func.now()))
        rows = (await conn.execute(_context().where(t.c.state.in_(INIT_ACTIVE)).order_by(t.c.id))).all()
    outcomes: dict[str, int] = {}
    for ctx in rows:
        try:
            if ctx.state == "queued":
                word = await _queued(engine, ctx, blobs, data_dir, measure, moment, count)
            else:
                word = await _running(engine, ctx)
        except InitFailed as exc:
            await _fail(engine, ctx, str(exc))
            word = "failed"
        except Exception as exc:  # one run failing leaves the others their turn
            log.exception("a kg init run failed", extra={"project": ctx.project, "init_run_id": ctx.id})
            await _fail(engine, ctx, f"{type(exc).__name__}: {exc}")
            word = "failed"
        outcomes[word] = outcomes.get(word, 0) + 1
    return dict(sorted(outcomes.items()))


async def _fail(engine: AsyncEngine, ctx, error: str) -> None:
    async with engine.begin() as conn:
        body = dict(ctx.body or {})
        body.setdefault("notes", []).append(f"failed: {error}")
        await _end(conn, ctx, body, "failed", error)


# The decision


async def _strategy_values(chosen: dict) -> tuple[dict, dict | None, str]:
    """(settings to set, what stays pending, a name for the reason) of the strategy part of a decision."""
    if "use" in chosen:
        builtin = kg_strategy.builtins().get(chosen["use"])
        if builtin is None:
            raise HTTPException(
                422, f"no built-in strategy named {chosen['use']!r}; known: {', '.join(sorted(kg_strategy.builtins()))}"
            )
        return {**builtin.settings, "strategy": builtin.name}, None, builtin.name
    made = kg_strategy.parse(chosen["create"], origin="created by the Curator")
    pending = {
        "name": made.name,
        "identifiers": list(made.identifiers),
        "ontology": made.ontology,
        "steps": list(made.steps),
        "file": chosen["create"],
    }
    return dict(made.settings), pending, made.name


def _reasons(decision: dict, *about: str) -> tuple[str, dict]:
    found = [r for r in decision.get("reasons") or [] if r["about"] in about]
    why = "; ".join(r["why"] for r in found)
    figures: dict = {}
    for r in found:
        figures.update({f"{r['about']}.{k}": v for k, v in r["figures"].items()})
    return why, figures


async def apply_decision(conn: AsyncConnection, access, user: Principal, row, decision: dict) -> dict:
    """Apply a validated ``decision`` to the project on the hub: the strategy's and the settings' values in one change of
    the settings' history, a rebuild queued when one is needed; what each part did, by part."""
    applied: dict = {}
    values: dict = {}
    chosen = decision.get("strategy")
    if chosen:
        values, pending, name = await _strategy_values(chosen)
        applied["strategy"] = {"name": name, "settings": {k: v for k, v in values.items()}}
        if pending:
            applied["strategy"]["pending"] = pending
    if decision.get("settings"):
        values = {**values, **decision["settings"]}
        applied["settings"] = dict(decision["settings"])
    if values:
        why, figures = _reasons(decision, "strategy", "settings")
        try:
            before, after, made = await kg_settings.change(
                conn,
                access.project_id,
                values,
                user_id=user.user_id,
                run_id=user.acts_for_run,
                reason=f"kg init #{row.id}: {why}"[:500],
                figures=figures or None,
            )
        except tuning.SettingsError as exc:
            raise HTTPException(422, f"settings: {exc}") from None
        needs = tuning.rebuild_keys(before, after)
        applied["change"] = {"id": made, "needs_rebuild": needs}
        if needs:
            try:
                build_id, _ = await queue_build(JobQueue.in_transaction(), conn, access.project_id, access.name, None)
            except QueueBusy:
                build_id = None
            applied["change"]["build_id"] = build_id
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.KG_SETTINGS,
            target=f"{access.name} change={made} kind=set init={row.id}",
            project_id=access.project_id,
        )
    for part, how in (
        ("sources", "file each source with kg_propose (kind kg_source): the Judge checks it, the build reads it"),
        ("code_changes", "file each with kg_propose or `evo-agents hub curator propose --lens kg_init`"),
    ):
        if decision.get(part):
            applied[part] = {"through": "the proposal flow", "how": how, "items": decision[part]}
    return applied


async def record_decision(conn: AsyncConnection, user: Principal, project: str, run_id: int, data) -> dict:
    """Validate and apply ``data``, the decision of run ``run_id``; the run's row as a dict of what was applied."""
    access = await project_access(conn, user, project)
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"deciding a kg_init run of project {access.name} needs the writer role on it")
    t = tables.kg_init_runs
    row = (
        await conn.execute(select(t).where(t.c.id == run_id, t.c.project_id == access.project_id).with_for_update())
    ).first()
    if row is None:
        raise HTTPException(404, f"project {access.name} has no kg_init run {run_id}")
    if row.state not in INIT_ACTIVE:
        raise HTTPException(409, f"kg_init run {run_id} is {row.state}: it takes no decision")
    acting = user.acts_for_run
    if acting is not None and acting != row.agent_run_id:
        raise HTTPException(403, f"run {acting} is not the review run of kg_init run {run_id}")
    body = dict(row.body or {})
    if body.get("decision"):
        raise HTTPException(409, f"kg_init run {run_id} has a decision already")
    try:
        decision = init_run.parse_decision(data)
    except init_run.DecisionError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        applied = await apply_decision(conn, access, user, row, decision)
    except kg_strategy.StrategyError as exc:
        raise HTTPException(422, str(exc)) from None
    body["decision"] = decision
    body["applied"] = applied
    await _set(conn, run_id, body=body)
    return {"run": run_id, "applied": applied}


# The routes


class InitRun(BaseModel):
    id: int
    project: str
    via: Literal["first_build", "profile", "manual"]
    state: Literal["queued", "running", "done", "failed"]
    why: list[str] = Field(description="what asked for the run")
    build_id: int | None
    agent_run_id: int | None = Field(description="the review run (lens kg_init) of the agent that decides")
    questions: dict | None = Field(
        default=None,
        description="the run that wrote the starter questions first (lens kg_questions): state, why, agent_run_id, "
        "what the agents' sink reads, the target, the eval set version it made; null before the run looked",
    )
    summary: str
    decisions: dict = Field(description="what the Curator decided and what was applied; empty until it decided")
    notes: list[str]
    report: str | None = Field(description="the report, markdown; null until the run ends")
    error: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None


class InitRuns(BaseModel):
    active: InitRun | None
    runs: list[InitRun]


class InitAll(BaseModel):
    started: list[str] = Field(description="the projects a run was queued for")
    skipped: dict[str, str] = Field(description="the projects left out, with why")


class DecisionBody(BaseModel):
    decision: dict = Field(description="the decision: strategy, settings, sources, code_changes and reasons")


class DecisionOut(BaseModel):
    run: int
    applied: dict = Field(description="what each part of the decision did")


def _shown(row, project: str) -> InitRun:
    body = row.body if isinstance(row.body, dict) else {}
    return InitRun(
        id=row.id,
        project=project,
        via=row.via,
        state=row.state,
        why=list(row.why or []),
        build_id=row.build_id,
        agent_run_id=row.agent_run_id,
        questions=kg_questions.shown(body),
        summary=kg_init.summary(project, row.state, body),
        decisions={"decision": body.get("decision") or {}, "applied": body.get("applied") or {}},
        notes=[str(n) for n in body.get("notes") or []],
        report=row.report,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        ended_at=row.ended_at,
    )


async def _begin(conn: AsyncConnection, user: Principal, access, why_all: bool):
    """Queue a manual run for ``access``'s project; (the run, started), HTTPException 409 without a build or a config."""
    found = await kg_init_db.active_of(conn, access.project_id)
    if found is not None:
        return found, False
    c = tables.kg_configs
    if (await conn.execute(select(c.c.project_id).where(c.c.project_id == access.project_id))).first() is None:
        raise HTTPException(409, f"project {access.name} has no knowledge config on the hub; nothing was started")
    latest = await _latest_build(conn, access.project_id)
    profile = ((latest.coverage or {}).get("profile") if latest else None) or None
    row, made = await kg_init_db.create(
        conn,
        access.project_id,
        "manual",
        kg_init.why_manual(user.login, why_all),
        requested_by=user.user_id,
        build_id=None if latest is None else latest.id,
        profile=profile,
    )
    if made:
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=KG_INIT,
            target=f"{access.name} init:{row.id}",
            project_id=access.project_id,
        )
        log.info("kg init run asked for", extra={"project": access.name, "login": user.login, "id": row.id})
    return row, made


async def start(conn: AsyncConnection, user: Principal, project: str) -> tuple[InitRun, bool]:
    """Start a kg_init run of ``project`` for ``user`` (a writer): (the run, True), or (the run in flight, False)."""
    access = await project_access(conn, user, project)
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"asking for a kg_init run of project {access.name} needs the writer role on it")
    row, made = await _begin(conn, user, access, False)
    return _shown(row, access.name), made


async def start_all(conn: AsyncConnection, user: Principal) -> InitAll:
    """Queue a run for every project with a knowledge config that has none in flight (an admin's)."""
    names = (await conn.execute(select(tables.projects.c.name).order_by(tables.projects.c.name))).scalars().all()
    out = InitAll(started=[], skipped={})
    for name in names:
        access = await project_access(conn, user, name)
        try:
            _, made = await _begin(conn, user, access, True)
        except HTTPException as exc:
            out.skipped[name] = str(exc.detail)
            continue
        if made:
            out.started.append(name)
        else:
            out.skipped[name] = "a run is in flight"
    return out


async def listing(conn: AsyncConnection, user: Principal, project: str, limit: int = MAX_LISTED) -> InitRuns:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the kg_init runs of project {access.name} needs a grant on it")
    t = tables.kg_init_runs
    rows = (
        await conn.execute(select(t).where(t.c.project_id == access.project_id).order_by(t.c.id.desc()).limit(limit))
    ).all()
    shown = [_shown(row, access.name) for row in rows]
    return InitRuns(active=next((run for run in shown if run.state in INIT_ACTIVE), None), runs=shown)


REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}


@router.post(
    "/{project}/init",
    response_model=InitRun,
    status_code=201,
    responses={200: {"model": InitRun, "description": "a run is in flight already: that run"}, **REFUSALS},
)
async def start_init(request: Request, project: ProjectName, user: CurrentUser):
    """Ask for a kg_init run of the project now: 201 with the new run, or 200 with the run in flight."""
    from fastapi.responses import JSONResponse

    async with request.app.state.engine.begin() as conn:
        found, started = await start(conn, user, project)
    if started:
        return found
    return JSONResponse(found.model_dump(mode="json"), status_code=200)


@router.get("/{project}/init", response_model=InitRuns, responses={code: {"model": ErrorBody} for code in (403, 404)})
async def list_init(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=50, description="the newest runs listed")] = MAX_LISTED,
) -> InitRuns:
    """The project's kg_init runs, newest first, with the one in flight, its decisions and its report."""
    async with request.app.state.engine.begin() as conn:
        return await listing(conn, user, project, limit)


@router.post("/{project}/init/{run_id}/decision", response_model=DecisionOut, responses=REFUSALS)
async def decide_init(request: Request, project: ProjectName, run_id: int, body: DecisionBody, user: CurrentUser):
    """Record the Curator's decision of a kg_init run: validated, then applied (the strategy and settings, with the
    reasons and figures kept in the settings' history, a rebuild queued when needed)."""
    async with request.app.state.engine.begin() as conn:
        return DecisionOut(**await record_decision(conn, user, project, run_id, body.decision))


@admin_router.post("/kg-init", response_model=InitAll, status_code=201)
async def start_init_all(request: Request, user: CurrentUser) -> InitAll:
    """Queue a kg_init run for every project with a knowledge config (an admin's)."""
    async with request.app.state.engine.begin() as conn:
        return await start_all(conn, user)
