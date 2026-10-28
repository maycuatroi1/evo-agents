"""The KG optimize loop of a project with a night shift (plan kg-retrieval-quality, criterion a7): each night inside
the charter's window, and whenever a writer asks (POST /v1/kg/{project}/optimize, ``evo-agents hub kg optimize``, the
button of the web's KG pages, the MCP tool kg_optimize), the hub measures the project's graph, decides what it can
decide itself, gives a review run to the worker on duty, and measures again after a new build. A run is a row of
kg_optimize_runs, which the job curator.collect moves on each minute (``advance``):

- ``measuring``: the holdout of the project's eval set asked of the latest build by words and by meaning, the tune
  split at each weight of the asked column (``evo_agents.hub.kg_optimize``), the open items of the review queue, the
  coverage of the build and what real sessions got from kg_search (kg_query_labels: click_rate, new labels). Then
  search by meaning is set on or back to index by the gate of criterion a12 (``kg_semantic``), the weight of the asked
  column that ranks the tune split best is kept on the row (``kg_asked`` reads it at the next build), and an asked-for
  run queues its review run (``collect.queue_optimize_review``): the kg_health lens alone, at any hour, under the
  charter's review cost cap. A night's run is the night's review run, which gets the kg_health lens beside the
  rotation's (``collect.queue_review``).
- ``running``: until the review run ends. Its agent proposes fixes of the KG code, which the Builder makes on a branch
  and the Judge checks before the auto-merge.
- ``after``: the fixes of the review queue are applied as graph decisions through the path of POST
  /v1/kg/{project}/review/apply (origin kg-optimize, the review run), a build is queued, and once one newer than the
  first measure succeeded (or AFTER_WAIT passed) the graph is measured again. A holdout worse than before beyond the
  eval's noise undoes the run: its decisions are undone, search by meaning goes back to its mode before, the weight
  goes back (the run ends ``reverted``, which ``kg_asked`` leaves out), and each change of the review run that merged
  gets a revert proposal (``outcomes.propose_revert``).
- At the end, ``done``, ``reverted`` or ``failed``, the owner of the night shift (and whoever asked) gets the report as
  a notification (notice kg_optimize), and the next morning brief has it.

The loop never turns on Jev's rerank (criterion a13). One run of a project is in flight at a time: a second call
returns the first.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_asked, kg_health_eval, kg_semantic, runs, tables
from evo_agents.hub import kg_optimize as model
from evo_agents.hub.access import has_role
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build import QueueBusy, kg_root, queue_build
from evo_agents.hub.kg_decisions import TRANSITIONS
from evo_agents.hub.kg_graph import ArtifactMismatch, GraphCache, GraphUnavailable, HubSession, open_graph
from evo_agents.hub.server import audit, kg_eval_sets, kg_health
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import built_graphs
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal
from evo_agents.hub.tables_kg import OPTIMIZE_ACTIVE
from evo_agents.kg import search as text_search
from evo_agents.kg import semantic
from evo_agents.kg.evaluate import TUNE

log = logging.getLogger(__name__)

LOOP = "kg-optimize"  # the origin of its decisions (kg_review.LOOP)
CACHE = "optimize"  # the directory of the worker's cache (``kg_root``) the measured graphs are fetched into
MAX_TUNE = 300  # tune questions asked at each weight
SESSION_DAYS = 7  # the days of real searches a first run counts
HOLD_REVIEW = timedelta(minutes=30)  # a night's review run waits this long for the first measure of its run
NIGHT_LINK_WAIT = timedelta(hours=12)  # a night's run waits this long for the night's review run
AFTER_WAIT = timedelta(hours=6)  # and this long for a build after the review run, then measures the latest
MAX_DECISIONS = 25  # graph decisions a run applies
MAX_LISTED = 10  # runs GET lists
ERROR_CHARS = 2000
NOTICE = "kg_optimize"  # runs.NOTICE_KINDS
KG_OPTIMIZE = "kg.optimize"  # the audit action of an asked-for run

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})


# Measuring


def _measure_graph(cache: GraphCache, blobs, project: str, access, sink: str, graphs, found_set, weight: float, tune):
    """The holdout of ``found_set`` asked of the latest graph by words and by meaning, and with ``tune`` the tune split
    at each weight of model.WEIGHTS. Blocking: run it in a thread."""
    question_set = found_set[0] if found_set else None
    holdout = kg_health_eval.holdout_questions(question_set, project)
    if not holdout:
        return {"why": "the project's eval set has no holdout question"}
    ceiling = access.rules.ceiling(access.max_level, sink)
    path, graph = cache.acquire(blobs, project, graphs)
    try:
        store = open_graph(path, project, graph)
        try:
            if store.latest_ready() is None:
                return {"why": f"build {graph.build_id} holds no ready graph"}
            session = HubSession(project, access.rules.policy, ceiling, sink, store, graph, graphs[0])

            def asked(questions, *, meaning: bool, at: float):
                store.asked_weight = at
                session.semantic = meaning
                found = kg_health_eval.score(questions, [], 0, session, plan_steps=0, sink=sink)
                return model.figures((found.get("methods") or {}).get("kg_search"))

            words = asked(holdout, meaning=False, at=weight)
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
            return {
                "build_id": graph.build_id,
                "content_hash": graph.content_hash,
                "words": words,
                "meaning": meaning,
                "why_meaning": why,
                "tuned": tuned,
            }
        finally:
            store.close()
    finally:
        cache.release(path)


async def measure_graph(engine: AsyncEngine, row, blobs, data_dir: Path | None, weight: float, tune: bool) -> dict:
    """The measure of ``row``'s project (project_id, project, owner_id, owner) as its owner reads it through the
    agents' sink: ``_measure_graph``'s, with ``review`` (kg_health.measure_review); {"why": ...} when it cannot."""
    async with engine.begin() as conn:
        graphs = await built_graphs(conn, row.project_id, kg_health.GRAPHS_TRIED)
        access, sink, why = await kg_health._owner_reads(conn, row, blobs, data_dir, graphs, agent=True)
        if why is not None:
            return {"why": why}
        found_set = await kg_eval_sets.latest_set(conn, row.project_id)
    cache = GraphCache(kg_root(data_dir) / CACHE, keep=1)
    try:
        found = await asyncio.to_thread(
            _measure_graph, cache, blobs, row.project, access, sink, graphs, found_set, weight, tune
        )
    except (GraphUnavailable, ArtifactMismatch) as exc:
        return {"why": str(exc)}
    review = await kg_health.measure_review(engine, row, blobs, data_dir, graphs)
    return {**found, "review": review}


Measure = Callable[[AsyncEngine, object, object, "Path | None", float, bool], Awaitable[dict]]


async def _sessions(conn: AsyncConnection, project_id: int, since: datetime) -> dict:
    """What real sessions and runs got from kg_search since ``since`` (kg_query_labels): searches, the share an agent
    opened a result of (click_rate), the share a grep followed, and the labels made since."""
    t = tables.kg_query_labels
    when = func.coalesce(t.c.searched_at, t.c.created_at)
    clicked = func.count().filter(func.jsonb_array_length(t.c.clicks) > 0)
    grepped = func.count().filter(t.c.grep_after)
    row = (
        await conn.execute(
            select(func.count().label("searches"), clicked.label("clicked"), grepped.label("grepped")).where(
                t.c.project_id == project_id, when >= since
            )
        )
    ).one()
    new = await conn.scalar(select(func.count()).where(t.c.project_id == project_id, t.c.created_at >= since))
    searches = row.searches or 0
    return {
        "since": since.isoformat(),
        "searches": searches,
        "click_rate": round(row.clicked / searches, 4) if searches else None,
        "grep_rate": round(row.grepped / searches, 4) if searches else None,
        "new_labels": new or 0,
    }


async def _coverage(conn: AsyncConnection, build_id: int | None) -> dict:
    if build_id is None:
        return {}
    b = tables.kg_builds
    found = await conn.scalar(select(b.c.coverage).where(b.c.id == build_id))
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


def _context():
    """The active runs with their project and its night shift's owner."""
    o, p, s, u = tables.kg_optimize_runs, tables.projects, tables.schedules, tables.users
    return (
        select(
            o,
            p.c.name.label("project"),
            s.c.id.label("schedule_id"),
            s.c.owner_id,
            u.c.login.label("owner"),
        )
        .join_from(o, p, p.c.id == o.c.project_id)
        .join(s, and_(s.c.project_id == o.c.project_id, s.c.kind == kg_health.NIGHT_SHIFT), isouter=True)
        .join(u, u.c.id == s.c.owner_id, isouter=True)
    )


async def _set(conn: AsyncConnection, run_id: int, **values) -> None:
    o = tables.kg_optimize_runs
    await conn.execute(update(o).where(o.c.id == run_id).values(**values, updated_at=func.now()))


async def _has_config(conn: AsyncConnection, project_id: int) -> bool:
    c = tables.kg_configs
    return (await conn.execute(select(c.c.project_id).where(c.c.project_id == project_id))).first() is not None


async def active_of(conn: AsyncConnection, project_id: int):
    o = tables.kg_optimize_runs
    query = select(o).where(o.c.project_id == project_id, o.c.state.in_(OPTIMIZE_ACTIVE))
    return (await conn.execute(query)).one_or_none()


async def of_agent_run(conn: AsyncConnection, run_id: int):
    """The KG optimize run whose review run ``run_id`` is, None when none is."""
    o = tables.kg_optimize_runs
    return (await conn.execute(select(o).where(o.c.agent_run_id == run_id).order_by(o.c.id.desc()).limit(1))).first()


async def night_run(conn: AsyncConnection, due, night: date):
    """The KG optimize run of ``due``'s project and ``night``, started when the project has a knowledge config and no
    other run is in flight: what ``collect.queue_review`` asks before it queues the night's review run."""
    o = tables.kg_optimize_runs
    found = (
        await conn.execute(select(o).where(o.c.project_id == due.project_id, o.c.via == "night", o.c.night == night))
    ).one_or_none()
    if found is not None:
        return found
    if not await _has_config(conn, due.project_id) or await active_of(conn, due.project_id) is not None:
        return None
    try:
        async with conn.begin_nested():
            values = {"project_id": due.project_id, "via": "night", "night": night, "state": "measuring", "body": {}}
            await conn.execute(insert(o).values(**values))
    except IntegrityError:  # another pass started it, or a run was asked for in between
        return None
    log.info("kg optimize run of the night started", extra={"project": due.project, "night": night.isoformat()})
    return (
        await conn.execute(select(o).where(o.c.project_id == due.project_id, o.c.via == "night", o.c.night == night))
    ).one()


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
    await _set(conn, found.id, agent_run_id=run_id)


# The phases


async def _gate(conn: AsyncConnection, ctx):
    """(due, word, gate) of the night shift of ``ctx``'s project for a run asked for now: at any hour, beside the
    schedule's other runs."""
    from evo_agents.hub.server.curator import _due, gate_of  # it imports collect, which imports this module

    due = (await conn.execute(_due(None).where(tables.schedules.c.project_id == ctx.project_id))).one_or_none()
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
    async with engine.begin() as conn:
        current = await kg_asked.asked_weight(conn, ctx.project_id)
        weight = text_search.ASKED_WEIGHT if current is None else current
    graph = await measure(engine, ctx, blobs, data_dir, weight, True)
    if graph.get("why"):
        raise MeasureFailed(graph["why"])
    async with engine.begin() as conn:
        o = tables.kg_optimize_runs
        last = await conn.scalar(
            select(o.c.created_at).where(o.c.project_id == ctx.project_id, o.c.id < ctx.id).order_by(o.c.id.desc())
        )
        since = last or (await conn.scalar(select(func.now()))) - timedelta(days=SESSION_DAYS)
        sessions = await _sessions(conn, ctx.project_id, since)
        settings = await kg_semantic.read(conn, ctx.project_id)
        gate = model.semantic_gate(graph.get("words"), graph.get("meaning"), settings.mode, graph.get("why_meaning"))
        gate["previous"] = settings.mode
        if gate["mode"] != settings.mode:
            await kg_semantic.write(conn, ctx.project_id, semantic.Settings(gate["mode"], settings.model), ctx.owner_id)
        asked = model.best_weight(graph.get("tuned") or {}, weight, sessions["new_labels"])
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
        await _set(
            conn,
            ctx.id,
            state="running",
            body=body,
            before_build_id=graph.get("build_id"),
            asked_weight=asked["weight"] if asked["changed"] else None,
            agent_run_id=agent_run_id,
        )
    return "measured"


def _shim(engine: AsyncEngine, blobs, data_dir: Path):
    """What the routes of the review queue read of ``request.app.state``, for the job."""
    state = SimpleNamespace(
        engine=engine,
        blobs=blobs,
        jobs=JobQueue.in_transaction(),
        kg_graphs=None if data_dir is None else GraphCache(kg_root(data_dir) / CACHE / "graphs", keep=1),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _owner(ctx) -> Principal:
    return Principal(user_id=ctx.owner_id, login=ctx.owner, admin=False, token_id=None, kind=MACHINE, token_hash="")  # type: ignore[arg-type]


async def _apply(engine: AsyncEngine, ctx, blobs, data_dir: Path, body: dict) -> None:
    """Apply the fixes of the open review items as decisions through POST /v1/kg/{project}/review/apply, as the owner
    of the night shift; what was decided in ``body["decisions"]``."""
    from evo_agents.hub.server.kg_review import ApplyBody, apply_review

    if ctx.owner_id is None:
        body["decisions"] = {"numbers": [], "why": "the project has no night shift owner to decide as"}
        return
    request = _shim(engine, blobs, data_dir)
    asked = ApplyBody(origin=LOOP, run_id=ctx.agent_run_id, max_decisions=MAX_DECISIONS)
    try:
        found = await apply_review(request, ctx.project, asked, _owner(ctx))
    except HTTPException as exc:
        body["decisions"] = {"numbers": [], "why": f"not applied: {exc.detail}"}
        return
    numbers = [d.number for item in found.items for d in item.decisions if d.number is not None]
    body["decisions"] = {"numbers": numbers, "rev": found.rev, "left": found.left, "why": "the review queue's fixes"}


async def _queue_build(conn: AsyncConnection, ctx) -> int | None:
    try:
        build_id, _ = await queue_build(JobQueue.in_transaction(), conn, ctx.project_id, ctx.project, None)
    except QueueBusy:
        return None
    return build_id


async def _running(engine: AsyncEngine, ctx, blobs, data_dir: Path, now: datetime) -> str:
    r, cf = tables.runs, tables.curator_figures
    async with engine.begin() as conn:
        agent_run_id = ctx.agent_run_id
        if agent_run_id is None and ctx.via == "night" and ctx.schedule_id is not None:
            agent_run_id = await conn.scalar(
                select(cf.c.run_id).where(cf.c.schedule_id == ctx.schedule_id, cf.c.night == ctx.night)
            )
            if agent_run_id is not None:
                await _set(conn, ctx.id, agent_run_id=agent_run_id)
        if agent_run_id is not None:
            state = await conn.scalar(select(r.c.state).where(r.c.id == agent_run_id))
            if state in runs.ACTIVE_STATES:
                return "running"
        elif ctx.via == "night" and ctx.created_at + NIGHT_LINK_WAIT > now:
            return "running"
    body = dict(ctx.body or {})
    if agent_run_id is None:
        body.setdefault("notes", []).append("no review run of the night")
    await _apply(engine, SimpleNamespace(**{**ctx._mapping, "agent_run_id": agent_run_id}), blobs, data_dir, body)
    async with engine.begin() as conn:
        body["after_queued_at"] = (await conn.scalar(select(func.now()))).isoformat()
        body["after_build_queued"] = await _queue_build(conn, ctx)
        await _set(conn, ctx.id, state="after", body=body)
    return "applied"


async def _pulls(conn: AsyncConnection, agent_run_id: int | None) -> list[dict]:
    """The changes of the review run's proposals: repo, branch, pull request, state, merged or not."""
    if agent_run_id is None:
        return []
    p, c = tables.proposals, tables.curator_changes
    rows = await conn.execute(
        select(c.c.id, c.c.proposal_id, c.c.repo, c.c.branch, c.c.pr_url, c.c.state, c.c.merge_sha, c.c.merged_at)
        .join_from(c, p, p.c.id == c.c.proposal_id)
        .where(p.c.run_id == agent_run_id)
        .order_by(c.c.id)
    )
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
        for row in rows
    ]


async def _undo(conn: AsyncConnection, ctx, body: dict, reason: str) -> list[int]:
    """Undo the run's decisions still active, as the owner; their numbers."""
    from evo_agents.hub.server.kg_decisions import _event

    numbers = (body.get("decisions") or {}).get("numbers") or []
    if not numbers or ctx.owner_id is None:
        return []
    d, p, e = tables.kg_decisions, tables.projects, tables.kg_decision_events
    await conn.execute(select(p.c.id).where(p.c.id == ctx.project_id).with_for_update())
    rev = await conn.scalar(select(func.coalesce(func.max(e.c.rev), 0)).where(e.c.project_id == ctx.project_id))
    needs, leaves = TRANSITIONS["undo"]
    rows = await conn.execute(
        select(d.c.id, d.c.number).where(d.c.project_id == ctx.project_id, d.c.number.in_(numbers), d.c.state == needs)
    )
    access = SimpleNamespace(project_id=ctx.project_id, name=ctx.project)
    undone = []
    for row in rows.all():
        rev += 1
        await conn.execute(update(d).where(d.c.id == row.id).values(state=leaves))
        await _event(
            conn, None, _owner(ctx), access, row, "undo", rev=rev, reason=reason, build_id=None,
            origin=LOOP, run_id=ctx.agent_run_id, build=False,
        )  # fmt: skip
        undone.append(row.number)
    return undone


async def _reverts(conn: AsyncConnection, ctx, body: dict, pulls: list[dict], reason: str) -> list[int]:
    """Propose reverting each merged change of the review run; the proposals' ids."""
    from evo_agents.hub.server.outcomes import _owner as owner_of
    from evo_agents.hub.server.outcomes import propose_revert

    merged = [item["change_id"] for item in pulls if item["state"] == "merged" and item.get("merge_sha")]
    if not merged:
        return []
    c, p, ch = tables.curator_changes, tables.proposals, tables.charters
    charter = (
        await conn.scalar(
            select(ch.c.body).where(ch.c.project_id == ctx.project_id).order_by(ch.c.revision.desc()).limit(1)
        )
    ) or {}
    owner = await owner_of(conn, ctx.project_id)
    before = (body.get("before") or {}).get("holdout") or {}
    after = (body.get("after") or {}).get("holdout") or {}
    found = {
        "reason": f"the KG optimize run #{ctx.id} made the holdout worse: {reason}",
        "since": ctx.created_at.isoformat(),
        "until": (await conn.scalar(select(func.now()))).isoformat(),
        "metrics": [
            {
                "key": f"kg_holdout:{key}",
                "what": f"holdout {key}",
                "before": before.get(key),
                "after": after.get(key),
                "change": "worse",
            }
            for key in ("hit@5", "tokens_per_hit")
            if before.get(key) is not None and after.get(key) is not None
        ],  # fmt: skip
    }
    ids = []
    for change_id in merged:
        change = (await conn.execute(select(c).where(c.c.id == change_id))).one()
        proposal = (await conn.execute(select(p).where(p.c.id == change.proposal_id))).one()
        ids.append(await propose_revert(conn, change, proposal, found, {}, charter, owner, ctx.project))
    return ids


async def _after(engine: AsyncEngine, ctx, blobs, data_dir, now: datetime, measure: Measure) -> str:
    body = dict(ctx.body or {})
    b = tables.kg_builds
    queued_at = datetime.fromisoformat(body["after_queued_at"]) if body.get("after_queued_at") else ctx.updated_at
    async with engine.begin() as conn:
        newest = (
            await conn.execute(
                select(b.c.id, b.c.status)
                .where(b.c.project_id == ctx.project_id, b.c.queued_at >= queued_at)
                .order_by(b.c.id.desc())
                .limit(1)
            )
        ).first()
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
    mode = gate.get("mode") or "off"
    body["after"] = {
        "build_id": graph.get("build_id"),
        "holdout": _in_effect(graph, mode),
        "words": graph.get("words"),
        "meaning": graph.get("meaning"),
        "review_open": _review_open(graph),
    }
    regressed = model.regressions((body.get("before") or {}).get("holdout"), body["after"]["holdout"])
    body["regressions"] = regressed
    async with engine.begin() as conn:
        pulls = await _pulls(conn, ctx.agent_run_id)
        state = "done"
        if regressed:
            state, reason = "reverted", "; ".join(regressed)
            undone = {"decisions": await _undo(conn, ctx, body, reason)}
            undone["reverts"] = await _reverts(conn, ctx, body, pulls, reason)
            undone["asked_weight"] = (body.get("asked") or {}).get("previous")
            previous = gate.get("previous")
            if previous and previous != mode:
                settings = await kg_semantic.read(conn, ctx.project_id)
                await kg_semantic.write(conn, ctx.project_id, semantic.Settings(previous, settings.model), ctx.owner_id)
            undone["semantic"] = previous or mode
            body["undone"] = undone
            await _queue_build(conn, ctx)  # the build after the undo
            for item in pulls:
                if item["change_id"] and item["state"] == "merged" and undone["reverts"]:
                    item["state"] = "merged, revert proposed"
        body["pulls"] = pulls
        await _set(conn, ctx.id, state=state, body=body, after_build_id=graph.get("build_id"), ended_at=func.now())
        await _report(conn, ctx, body, state)
    return state


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
    from evo_agents.hub.server.notifications import notify

    first = None
    for user_id in to:
        found = await notify(
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
    await _set(conn, ctx.id, report=text, notification_id=first)
    return first


async def _fail(engine: AsyncEngine, ctx, error: str) -> None:
    async with engine.begin() as conn:
        body = dict(ctx.body or {})
        body.setdefault("notes", []).append(f"failed: {error}")
        await _set(conn, ctx.id, state="failed", error=error[:ERROR_CHARS], body=body, ended_at=func.now())
        await _report(conn, ctx, body, "failed")


async def advance(
    engine: AsyncEngine, *, blobs=None, data_dir=None, now: datetime | None = None, measure: Measure | None = None
) -> dict:
    """Move each KG optimize run in flight one phase on (see the module), each on its own; how many did what."""
    measure = measure or measure_graph
    o = tables.kg_optimize_runs
    async with engine.begin() as conn:
        moment = now or await conn.scalar(select(func.now()))
        rows = (await conn.execute(_context().where(o.c.state.in_(OPTIMIZE_ACTIVE)).order_by(o.c.id))).all()
    outcomes: dict[str, int] = {}
    for ctx in rows:
        try:
            if ctx.state == "measuring":
                word = await _measuring(engine, ctx, blobs, data_dir, measure)
            elif ctx.state == "running":
                word = await _running(engine, ctx, blobs, data_dir, moment)
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
    o = tables.kg_optimize_runs
    floor = since or (await conn.scalar(select(func.now()))) - timedelta(days=1)
    row = (
        await conn.execute(
            select(o.c.id, o.c.state, o.c.body, o.c.report)
            .where(o.c.project_id == project_id, o.c.ended_at.is_not(None), o.c.ended_at >= floor)
            .order_by(o.c.ended_at.desc())
            .limit(1)
        )
    ).first()
    if row is None or row.report is None:
        return None
    project = await conn.scalar(select(tables.projects.c.name).where(tables.projects.c.id == project_id))
    summary = model.summary(project, row.body if isinstance(row.body, dict) else {}, row.state)
    return {"id": row.id, "state": row.state, "summary": summary, "report": row.report}


# The routes


class OptimizeRun(BaseModel):
    id: int
    project: str
    via: Literal["night", "manual"]
    state: Literal["measuring", "running", "after", "done", "reverted", "failed"]
    night: date | None
    agent_run_id: int | None = Field(description="the review run given to the worker on duty")
    before_build_id: int | None
    after_build_id: int | None
    asked_weight: float | None = Field(description="the weight of the asked column the run left in effect")
    summary: str
    report: str | None = Field(description="the report the owner got, markdown; null until the run ends")
    error: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    body: dict = Field(description="what the run measured, decided and undid")


class OptimizeRuns(BaseModel):
    active: OptimizeRun | None
    runs: list[OptimizeRun]


def _shown(row, project: str) -> OptimizeRun:
    body = row.body if isinstance(row.body, dict) else {}
    return OptimizeRun(
        id=row.id,
        project=project,
        via=row.via,
        state=row.state,
        night=row.night,
        agent_run_id=row.agent_run_id,
        before_build_id=row.before_build_id,
        after_build_id=row.after_build_id,
        asked_weight=row.asked_weight,
        summary=model.summary(project, body, row.state),
        report=row.report,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        ended_at=row.ended_at,
        body=body,
    )


async def start(conn: AsyncConnection, user: Principal, project: str) -> tuple[OptimizeRun, bool]:
    """Start a KG optimize run of ``project`` for ``user`` (a writer): (the run, True), or (the run in flight, False).
    HTTPException 409 for a project without a knowledge config or a night shift."""
    access = await project_access(conn, user, project)
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"optimizing the knowledge graph of project {access.name} needs the writer role on it")
    found = await active_of(conn, access.project_id)
    if found is not None:
        return _shown(found, access.name), False
    if not await _has_config(conn, access.project_id):
        raise HTTPException(409, f"project {access.name} has no knowledge config on the hub; nothing was started")
    s = tables.schedules
    schedule = await conn.scalar(
        select(s.c.id).where(s.c.project_id == access.project_id, s.c.kind == kg_health.NIGHT_SHIFT)
    )
    if schedule is None:
        raise HTTPException(409, f"project {access.name} has no night shift: an admin writes its charter first")
    o = tables.kg_optimize_runs
    values = {"project_id": access.project_id, "via": "manual", "requested_by": user.user_id, "state": "measuring"}
    try:
        async with conn.begin_nested():
            row = (await conn.execute(insert(o).values(**values, body={}).returning(o))).one()
    except IntegrityError:  # another call started one in between
        found = await active_of(conn, access.project_id)
        if found is None:
            raise
        return _shown(found, access.name), False
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=KG_OPTIMIZE,
        target=f"{access.name} optimize:{row.id}",
        project_id=access.project_id,
    )
    log.info("kg optimize run asked for", extra={"project": access.name, "login": user.login, "id": row.id})
    return _shown(row, access.name), True


async def listing(conn: AsyncConnection, user: Principal, project: str, limit: int = MAX_LISTED) -> OptimizeRuns:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the knowledge graph of project {access.name} needs a grant on it")
    o = tables.kg_optimize_runs
    rows = (
        await conn.execute(select(o).where(o.c.project_id == access.project_id).order_by(o.c.id.desc()).limit(limit))
    ).all()
    shown = [_shown(row, access.name) for row in rows]
    active = next((run for run in shown if run.state in OPTIMIZE_ACTIVE), None)
    return OptimizeRuns(active=active, runs=shown)


START_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}


@router.post(
    "/{project}/optimize",
    response_model=OptimizeRun,
    status_code=201,
    responses={200: {"model": OptimizeRun, "description": "a run is in flight already: that run"}, **START_REFUSALS},
)
async def start_optimize(request: Request, project: ProjectName, user: CurrentUser):
    """Start a KG optimize run of the project now, at any hour, under the charter's review cost cap: 201 with the
    new run, or 200 with the run in flight, as one runs at a time."""
    from fastapi.responses import JSONResponse

    async with request.app.state.engine.begin() as conn:
        found, started = await start(conn, user, project)
    if started:
        return found
    return JSONResponse(found.model_dump(mode="json"), status_code=200)


@router.get(
    "/{project}/optimize",
    response_model=OptimizeRuns,
    responses={code: {"model": ErrorBody} for code in (403, 404)},
)
async def list_optimize(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=50, description="the newest runs listed")] = MAX_LISTED,
) -> OptimizeRuns:
    """The project's KG optimize runs, newest first, with the one in flight and each run's report."""
    async with request.app.state.engine.begin() as conn:
        return await listing(conn, user, project, limit)


async def mcp_answer(request: Request, project: str, user, arguments: dict, *, sink: str) -> tuple[str, dict]:
    """kg_optimize: start a KG optimize run (``action`` start, the default) or read the runs (status)."""
    del sink  # the answer shows figures and decisions, no graph data
    action = arguments.get("action") or "start"
    if action not in ("start", "status"):
        raise HTTPException(422, "action is start or status")
    async with request.app.state.engine.begin() as conn:
        if action == "status":
            found = await listing(conn, user, project, 5)
            text = (
                f"KG optimize run #{found.active.id} of project {project} is {found.active.state}"
                if found.active
                else f"no KG optimize run of project {project} is in flight"
            )
            return text, found.model_dump(mode="json")
        run, started = await start(conn, user, project)
    word = "started" if started else f"in flight already ({run.state})"
    text = f"KG optimize run #{run.id} of project {project} {word}: the hub measures it within a minute"
    return text, {"run": run.model_dump(mode="json"), "started": started}
