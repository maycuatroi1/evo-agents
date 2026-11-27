"""The KG optimize loop of a project (``evo_agents.hub.server.kg_optimize`` says what it does): the measure of the
graph, the phases ``advance`` moves a run through, the morning brief's facts, and starting and listing the runs. Each
phase and each request opens its own transactions with UnitOfWork; the queries are ``kg.repository.optimize``'s."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_asked, kg_health_eval, kg_semantic, kg_snapshots, runs
from evo_agents.hub import kg_optimize as model
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build import QueueBusy, queue_build
from evo_agents.hub.kg_graph import GraphUnavailable, HubSession, reading
from evo_agents.hub.server import audit, kg_eval_sets, kg_health
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg import built_graphs
from evo_agents.hub.server.kg.models.optimize import (
    AFTER_WAIT,
    ERROR_CHARS,
    HOLD_REVIEW,
    KG_OPTIMIZE,
    MAX_LISTED,
    MAX_TUNE,
    NIGHT_LINK_WAIT,
    NOTICE,
    SESSION_DAYS,
    OptimizeRun,
    OptimizeRuns,
    shown,
)
from evo_agents.hub.server.kg.repository import optimize as repo
from evo_agents.hub.server.kg_audience import agent_audience
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.tables_kg import OPTIMIZE_ACTIVE
from evo_agents.kg import rank, semantic
from evo_agents.kg import search as text_search
from evo_agents.kg.evaluate import TUNE

log = logging.getLogger(__name__)


# Measuring


def _measure_graph(
    project: str,
    access,
    sink: str,
    graphs,
    found_set,
    weight: float,
    tune,
    audience: frozenset[str] = frozenset(),
    loaded=None,
):
    """The holdout of ``found_set`` asked of the latest graph by words and by meaning, and with ``tune`` the tune split
    at each weight of model.WEIGHTS, read with ``audience`` (the owner's, as their agents' kg_search reads the graph).
    Of ``loaded``, the snapshot of the hub's tables, when it is of one of ``graphs``, else of the graph file.
    Blocking: run it in a thread."""
    question_set = found_set[0] if found_set else None
    holdout = kg_health_eval.holdout_questions(question_set, project)
    if question_set is None or not holdout:
        return {"why": "the project's eval set has no holdout question"}
    ceiling = access.rules.ceiling(access.max_level, sink, audience)
    with reading(project, kg_health.snapshot_of(loaded, graphs, ceiling)) as (store, graph):
        if store.latest_ready() is None:
            return {"why": f"build {graph.build_id} holds no ready graph"}
        session = HubSession(project, access.rules.policy, ceiling, sink, store, graph, graphs[0])

        def asked(questions, *, meaning: bool, at: float, kept: dict | None = None):
            store.asked_weight = at
            session.semantic = meaning
            found = kg_health_eval.score(questions, [], 0, session, plan_steps=0, sink=sink, series=kept is not None)
            if kept is not None:
                kept[f"{at:g}"] = (found.get("series") or {}).get("kg_search")
            return model.figures((found.get("methods") or {}).get("kg_search"))

        held: dict = {}  # the holdout's per-question series at each weight, for the Pareto choice
        words = asked(holdout, meaning=False, at=weight, kept=held if tune else None)
        meaning, why = None, None
        info = store.vector_info()
        if info is None:
            why = "the graph holds no vectors (search by meaning is off for the project: set its mode to index)"
        else:
            try:
                semantic.embedder(info.model)
            except semantic.SemanticUnavailable as exc:
                why = f"the model {info.model} cannot be loaded here: {exc}"
            else:
                meaning = asked(holdout, meaning=True, at=weight)
        tuned = {}
        if tune:
            questions = [q for q in question_set.part(TUNE) if q.project in (None, project)][:MAX_TUNE]
            if questions:
                tuned = {f"{w:g}": asked(questions, meaning=False, at=w) for w in model.WEIGHTS}
                for w in model.WEIGHTS:
                    if f"{w:g}" not in held:
                        asked(holdout, meaning=False, at=w, kept=held)
        return {
            "build_id": graph.build_id,
            "content_hash": graph.content_hash,
            "words": words,
            "meaning": meaning,
            "why_meaning": why,
            "tuned": tuned,
            "holdout_series": held,
        }


async def measure_graph(engine: AsyncEngine, row, blobs, data_dir: Path | None, weight: float, tune: bool) -> dict:
    """The measure of ``row``'s project (project_id, project, owner_id, owner) as its owner's agents read it, through
    the agents' sink with the owner's audience: ``_measure_graph``'s, with ``review`` (kg_health.measure_review); {"why": ...} when it cannot."""
    async with UnitOfWork(engine) as conn:
        graphs = await built_graphs(conn, row.project_id, kg_health.GRAPHS_TRIED)
        access, sink, why = await kg_health._owner_reads(conn, row, graphs, agent=True)
        if why is not None:
            return {"why": why}
        found_set = await kg_eval_sets.latest_set(conn, row.project_id)
        audience = await agent_audience(conn, row.owner_id, None, None)
    try:
        loaded = await kg_snapshots.load(engine, row.project_id)
        found = await asyncio.to_thread(
            _measure_graph,
            row.project,
            access,
            sink,
            graphs,
            found_set,
            weight,
            tune,
            audience=audience,
            loaded=loaded,
        )
    except GraphUnavailable as exc:
        return {"why": str(exc)}
    review = await kg_health.measure_review(engine, row, blobs, data_dir, graphs, loaded)  # the same snapshot
    return {**found, "review": review}


Measure = Callable[[AsyncEngine, object, object, "Path | None", float, bool], Awaitable[dict]]
Apply = Callable[..., Awaitable[None]]  # (engine, ctx, blobs, data_dir, body): kg_optimize._apply


async def _sessions(conn: AsyncConnection, project_id: int, since: datetime) -> dict:
    """What real sessions and runs got from kg_search since ``since`` (kg_query_labels): searches, the share an agent
    opened a result of (click_rate), the share a grep followed, and the labels made since."""
    row = await repo.search_counts(conn, project_id, since)
    new = await repo.new_labels(conn, project_id, since)
    logged = await repo.explored_searches(conn, project_id, since)
    explored = rank.explore_estimate({"results": r, "clicks": c, "grep_after": g} for r, c, g in logged)
    searches = row.searches or 0
    return {
        "explore": explored,
        "since": since.isoformat(),
        "searches": searches,
        "click_rate": round(row.clicked / searches, 4) if searches else None,
        "grep_rate": round(row.grepped / searches, 4) if searches else None,
        "new_labels": new or 0,
    }


async def _coverage(conn: AsyncConnection, build_id: int | None) -> dict:
    if build_id is None:
        return {}
    found = await repo.build_coverage(conn, build_id)
    if not isinstance(found, dict):
        return {}
    return {
        key: value
        for key, value in found.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and key != "skipped"
    }


def _in_effect(graph: dict, mode: str) -> dict | None:
    """The holdout's figures by the mode kg_search answers in."""
    return graph.get("meaning") if mode == "on" and graph.get("meaning") else graph.get("words")


def _review_open(graph: dict) -> int | None:
    counts = (graph.get("review") or {}).get("open")
    return sum(v for v in counts.values() if isinstance(v, int)) if isinstance(counts, dict) else None


# The rows


active_of = repo.active_of
of_agent_run = repo.of_agent_run


async def night_run(conn: AsyncConnection, due, night: date):
    """The KG optimize run of ``due``'s project and ``night``, started when the project has a knowledge config and no
    other run is in flight: what ``collect.queue_review`` asks before it queues the night's review run."""
    found = await repo.night_row(conn, due.project_id, night)
    if found is not None:
        return found
    if not await repo.has_config(conn, due.project_id) or await repo.active_of(conn, due.project_id) is not None:
        return None
    try:
        async with conn.begin_nested():
            values = {"project_id": due.project_id, "via": "night", "night": night, "state": "measuring", "body": {}}
            await repo.insert_run(conn, **values)
    except IntegrityError:  # another pass started it, or a run was asked for in between
        return None
    log.info("kg optimize run of the night started", extra={"project": due.project, "night": night.isoformat()})
    return await repo.night_row(conn, due.project_id, night)


def holds_review(found, now: datetime) -> bool:
    """Whether the night's review run waits for the first measure of KG optimize run ``found``."""
    return found is not None and found.state == "measuring" and found.created_at + HOLD_REVIEW > now


def with_lens(lenses: list[str], found) -> list[str]:
    """The night's lenses with kg_health when its KG optimize run is in flight: the lens no longer comes from the
    rotation alone."""
    if found is None or found.state not in OPTIMIZE_ACTIVE or "kg_health" in lenses:
        return list(lenses)
    return [*lenses, "kg_health"]


async def link_agent_run(conn: AsyncConnection, found, run_id: int) -> None:
    await repo.set_run(conn, found.id, agent_run_id=run_id)


# The phases


async def _gate(conn: AsyncConnection, ctx):
    """(due, word, gate) of the night shift of ``ctx``'s project for a run asked for now: at any hour, beside the
    schedule's other runs."""
    from evo_agents.hub.server.curator.service.shift import gate_of  # it imports collect, which imports this

    due = await repo.due_schedule(conn, ctx.project_id)
    if due is None:
        return None, "none", None
    word, gate = await gate_of(conn, due, any_hour=True, beside=True, on_demand=True)
    return due, word, gate


async def _queue_agent(conn: AsyncConnection, ctx, body: dict) -> int | None:
    """Queue the review run of an asked-for KG optimize run; its id, or None with why in ``body["notes"]``."""
    from evo_agents.hub.server.build_now import GATE_REFUSALS
    from evo_agents.hub.server.collect import queue_optimize_review

    due, word, gate = await _gate(conn, ctx)
    if gate is None:
        body.setdefault("notes", []).append(f"no review run: {GATE_REFUSALS.get(word, word)}")
        return None
    run_id, why = await queue_optimize_review(conn, due, gate, ctx.id)
    if run_id is None:
        body.setdefault("notes", []).append(f"no review run: {why}")
    return run_id


async def _measuring(engine: AsyncEngine, ctx, blobs, data_dir, measure: Measure) -> str:
    async with UnitOfWork(engine) as conn:
        current = await kg_asked.asked_weight(conn, ctx.project_id)
        weight = text_search.ASKED_WEIGHT if current is None else current
    graph = await measure(engine, ctx, blobs, data_dir, weight, True)
    if graph.get("why"):
        raise MeasureFailed(graph["why"])
    async with UnitOfWork(engine) as conn:
        last = await repo.previous_created(conn, ctx.project_id, ctx.id)
        since = last or (await repo.now(conn)) - timedelta(days=SESSION_DAYS)
        sessions = await _sessions(conn, ctx.project_id, since)
        settings = await kg_semantic.read(conn, ctx.project_id)
        gate = model.semantic_gate(graph.get("words"), graph.get("meaning"), settings.mode, graph.get("why_meaning"))
        gate["previous"] = settings.mode
        gate["applied"] = False  # the gate advises; the Curator's kg_init run decides the mode
        asked = model.best_weight(graph.get("tuned") or {}, weight, sessions["new_labels"])
        asked = model.gate_weight(asked, graph.get("holdout_series"), weight)
        asked["previous"] = weight
        body = {
            "before": {
                "build_id": graph.get("build_id"),
                "holdout": _in_effect(graph, settings.mode),
                "words": graph.get("words"),
                "meaning": graph.get("meaning"),
                "review_open": _review_open(graph),
                "coverage": await _coverage(conn, graph.get("build_id")),
                "sessions": sessions,
            },
            "semantic": gate,
            "asked": asked,
            "notes": [],
        }
        agent_run_id = ctx.agent_run_id
        if ctx.via == "manual":
            agent_run_id = await _queue_agent(conn, ctx, body)
        await repo.set_run(
            conn,
            ctx.id,
            state="running",
            body=body,
            before_build_id=graph.get("build_id"),
            asked_weight=asked["weight"] if asked["changed"] else None,
            agent_run_id=agent_run_id,
        )
    return "measured"


async def _queue_build(conn: AsyncConnection, ctx) -> int | None:
    try:
        build_id, _ = await queue_build(JobQueue.in_transaction(), conn, ctx.project_id, ctx.project, None)
    except QueueBusy:
        return None
    return build_id


async def _running(engine: AsyncEngine, ctx, blobs, data_dir, now: datetime, apply: Apply) -> str:
    async with UnitOfWork(engine) as conn:
        agent_run_id = ctx.agent_run_id
        if agent_run_id is None and ctx.via == "night" and ctx.schedule_id is not None:
            agent_run_id = await repo.night_review_run(conn, ctx.schedule_id, ctx.night)
            if agent_run_id is not None:
                await repo.set_run(conn, ctx.id, agent_run_id=agent_run_id)
        if agent_run_id is not None:
            state = await repo.run_state(conn, agent_run_id)
            if state in runs.ACTIVE_STATES:
                return "running"
        elif ctx.via == "night" and ctx.created_at + NIGHT_LINK_WAIT > now:
            return "running"
    body = dict(ctx.body or {})
    if agent_run_id is None:
        body.setdefault("notes", []).append("no review run of the night")
    await apply(engine, SimpleNamespace(**{**ctx._mapping, "agent_run_id": agent_run_id}), blobs, data_dir, body)
    async with UnitOfWork(engine) as conn:
        body["after_queued_at"] = (await repo.now(conn)).isoformat()
        body["after_build_queued"] = await _queue_build(conn, ctx)
        await repo.set_run(conn, ctx.id, state="after", body=body)
    return "applied"


async def _pulls(conn: AsyncConnection, agent_run_id: int | None) -> list[dict]:
    """The changes of the review run's proposals: repo, branch, pull request, state, merged or not."""
    if agent_run_id is None:
        return []
    return [
        {
            "change_id": row.id,
            "proposal_id": row.proposal_id,
            "repo": row.repo,
            "branch": row.branch,
            "pr_url": row.pr_url,
            "state": "merged" if row.merged_at is not None else row.state,
            "merge_sha": row.merge_sha,
        }
        for row in await repo.pulls(conn, agent_run_id)
    ]


async def _after(engine: AsyncEngine, ctx, blobs, data_dir, now: datetime, measure: Measure) -> str:
    body = dict(ctx.body or {})
    queued_at = datetime.fromisoformat(body["after_queued_at"]) if body.get("after_queued_at") else ctx.updated_at
    async with UnitOfWork(engine) as conn:
        newest = await repo.newest_build_since(conn, ctx.project_id, queued_at)
        graphs = await built_graphs(conn, ctx.project_id, 1)
    built = bool(graphs) and newest is not None and graphs[0].build_id >= newest.id
    waited = queued_at + AFTER_WAIT <= now
    if not built and newest is not None and newest.status in ("queued", "running") and not waited:
        return "waiting"
    if not built:
        body.setdefault("notes", []).append("no new build succeeded after the review run: measured the latest")
    weight = (body.get("asked") or {}).get("weight", text_search.ASKED_WEIGHT)
    graph = await measure(engine, ctx, blobs, data_dir, weight, False)
    if graph.get("why"):
        raise MeasureFailed(graph["why"])
    gate = body.get("semantic") or {}
    mode = gate.get("previous") or "off"  # the mode in effect: the loop never changes it
    body["after"] = {
        "build_id": graph.get("build_id"),
        "holdout": _in_effect(graph, mode),
        "words": graph.get("words"),
        "meaning": graph.get("meaning"),
        "review_open": _review_open(graph),
    }
    regressed = model.regressions((body.get("before") or {}).get("holdout"), body["after"]["holdout"])
    body["regressions"] = regressed
    async with UnitOfWork(engine) as conn:
        body["pulls"] = await _pulls(conn, ctx.agent_run_id)
        body["handover"] = model.handover(body)  # the numbers the next kg_init run decides keep or revert from
        await repo.set_run(
            conn, ctx.id, state="done", body=body, after_build_id=graph.get("build_id"), ended_at=func.now()
        )
        await _report(conn, ctx, body, "done")
    return "done"


class MeasureFailed(Exception):
    """The graph could not be measured: why."""


async def _report(conn: AsyncConnection, ctx, body: dict, state: str) -> int | None:
    """Send the run's report to the owner of the night shift, and to whoever asked for it; the owner's notification's
    id, kept on the row with the report."""
    text = model.report_text(ctx.project, ctx.id, body, state)
    title = model.summary(ctx.project, body, state)
    to = [user for user in dict.fromkeys([ctx.owner_id, ctx.requested_by]) if user is not None]
    details = {
        "optimize_run_id": ctx.id,
        "state": state,
        "hit_before": ((body.get("before") or {}).get("holdout") or {}).get("hit@5"),
        "hit_after": ((body.get("after") or {}).get("holdout") or {}).get("hit@5"),
        "semantic": (body.get("semantic") or {}).get("decision"),
        "decisions": len((body.get("decisions") or {}).get("numbers") or []),
        "regressions": len(body.get("regressions") or []),
    }
    from evo_agents.hub.server.notifications.service.notifier import NOTIFIER

    first = None
    for user_id in to:
        found = await NOTIFIER.notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=NOTICE,
            title=title,
            body=text,
            project_id=ctx.project_id,
            run_id=ctx.agent_run_id,
            details=details,
            link=f"/p/{ctx.project}/kg/builds",
        )
        first = first or found
    await repo.set_run(conn, ctx.id, report=text, notification_id=first)
    return first


async def _fail(engine: AsyncEngine, ctx, error: str) -> None:
    async with UnitOfWork(engine) as conn:
        body = dict(ctx.body or {})
        body.setdefault("notes", []).append(f"failed: {error}")
        await repo.set_run(conn, ctx.id, state="failed", error=error[:ERROR_CHARS], body=body, ended_at=func.now())
        await _report(conn, ctx, body, "failed")


async def advance(
    engine: AsyncEngine,
    *,
    apply: Apply,
    blobs=None,
    data_dir=None,
    now: datetime | None = None,
    measure: Measure | None = None,
) -> dict:
    """Move each KG optimize run in flight one phase on, each on its own; how many did what. ``apply`` applies the
    fixes of the review queue (``kg_optimize._apply``)."""
    measure = measure or measure_graph
    async with UnitOfWork(engine) as conn:
        moment = now or await repo.now(conn)
        rows = await repo.active_contexts(conn)
    outcomes: dict[str, int] = {}
    for ctx in rows:
        try:
            if ctx.state == "measuring":
                word = await _measuring(engine, ctx, blobs, data_dir, measure)
            elif ctx.state == "running":
                word = await _running(engine, ctx, blobs, data_dir, moment, apply)
            else:
                word = await _after(engine, ctx, blobs, data_dir, moment, measure)
        except MeasureFailed as exc:
            await _fail(engine, ctx, str(exc))
            word = "failed"
        except Exception as exc:  # one run failing leaves the others their turn
            log.exception("a kg optimize run failed", extra={"project": ctx.project, "optimize_run_id": ctx.id})
            await _fail(engine, ctx, f"{type(exc).__name__}: {exc}")
            word = "failed"
        outcomes[word] = outcomes.get(word, 0) + 1
    return dict(sorted(outcomes.items()))


# The brief


async def brief_facts(conn: AsyncConnection, project_id: int, since: datetime | None = None) -> dict | None:
    """The newest KG optimize run of the project that ended since ``since`` (a day ago when None), for the morning
    brief: id, state, summary, report; None without one."""
    floor = since or (await repo.now(conn)) - timedelta(days=1)
    row = await repo.ended_since(conn, project_id, floor)
    if row is None or row.report is None:
        return None
    project = await repo.project_name(conn, project_id)
    summary = model.summary(project, row.body if isinstance(row.body, dict) else {}, row.state)
    return {"id": row.id, "state": row.state, "summary": summary, "report": row.report}


# Starting and listing


async def start(conn: AsyncConnection, user: Principal, project: str) -> tuple[OptimizeRun, bool]:
    """Start a KG optimize run of ``project`` for ``user`` (a writer): (the run, True), or (the run in flight, False).
    HTTPException 409 for a project without a knowledge config or a night shift."""
    access = await project_access(conn, user, project)
    if not holds(access.role, "writer"):
        raise HTTPException(403, f"optimizing the knowledge graph of project {access.name} needs the writer role on it")
    found = await repo.active_of(conn, access.project_id)
    if found is not None:
        return shown(found, access.name), False
    if not await repo.has_config(conn, access.project_id):
        raise HTTPException(409, f"project {access.name} has no knowledge config on the hub; nothing was started")
    schedule = await repo.night_shift_id(conn, access.project_id)
    if schedule is None:
        raise HTTPException(409, f"project {access.name} has no night shift: an admin writes its charter first")
    values = {"project_id": access.project_id, "via": "manual", "requested_by": user.user_id, "state": "measuring"}
    try:
        async with conn.begin_nested():
            row = await repo.insert_run(conn, **values, body={})
    except IntegrityError:  # another call started one in between
        found = await repo.active_of(conn, access.project_id)
        if found is None:
            raise
        return shown(found, access.name), False
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=KG_OPTIMIZE,
        target=f"{access.name} optimize:{row.id}",
        project_id=access.project_id,
    )
    log.info("kg optimize run asked for", extra={"project": access.name, "login": user.login, "id": row.id})
    return shown(row, access.name), True


async def listing(conn: AsyncConnection, user: Principal, project: str, limit: int = MAX_LISTED) -> OptimizeRuns:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the knowledge graph of project {access.name} needs a grant on it")
    found = [shown(row, access.name) for row in await repo.runs_of(conn, access.project_id, limit)]
    active = next((run for run in found if run.state in OPTIMIZE_ACTIVE), None)
    return OptimizeRuns(active=active, runs=found)


async def start_run(engine: AsyncEngine, user: Principal, project: str) -> tuple[OptimizeRun, bool]:
    """``start`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await start(conn, user, project)


async def list_runs(engine: AsyncEngine, user: Principal, project: str, limit: int = MAX_LISTED) -> OptimizeRuns:
    """``listing`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await listing(conn, user, project, limit)
