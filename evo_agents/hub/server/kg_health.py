"""The health of each charter's knowledge graph, measured on the hub without any model, and the part of the night's
figures it gives (``kg_health``). ``evo_agents.hub.kg_coverage`` holds the model, ``evo_agents.hub.kg_build`` keeps
what each build covers and the files of its git sources, and ``evo_agents.hub.kg_health_eval`` scores retrieval.

``measure`` runs in each pass of the job ``curator.collect`` (``evo_agents.hub.server.collect``): every project with a
night shift (a schedule of kind night_shift, its curator feature on) and a knowledge config on the hub whose last
measure is HEALTH_EVERY old, or RETRY_AFTER old when that one failed, is measured once, and the measure is kept in
kg_health (schema 0055) with the build it read. A measure holds, for each git source of the project's knowledge
config:

- ``commit``, the commit the source read in the latest successful build (kg_source_files; the commit the build's
  coverage names as ``read``);
- what the forge says now, read with the workers' GitHub App and ``contents: read`` (``evo_agents.hub.server.pulls``):
  the repo's default branch and the commit it points at (``head``), how many of its commits the commit read lacks
  (``behind``) and how long the oldest of them has waited (``lag_hours``, 0 when none does), and the files of the
  branch against those the build holds (``kg_coverage.compare_files``): ``missing``, ``stale`` and ``deleted``, each
  counted with a few paths, and the ``code:`` evidence a finding can cite for them; ``off``, those paths whole (up to
  ``kg_coverage.MAX_OFF_PATHS``), which the tier rules read and the figures leave out. A source with no successful
  build, a repo off GitHub, a hub without the App, an App not installed, a commit GitHub does not know, a tree GitHub
  cut: the entry says why (``why``) and keeps what was read;

and, once for the project, ``eval``: the retrieval evaluation of the latest build's graph (``kg_health_eval``), of the
holdout part of the project's question set (``evo_agents.hub.server.kg_eval_sets``) and the plans the schedule's owner
may read, asked as that owner reads the graph through the agent sink (``kg_health_eval.AGENT_SINK``) when the policy
declares it, else the project's hub sink. The same questions of the same graph keep the score of the measure before, so
a quiet day asks nothing again. Each measure whose eval has a score pushes the signals EVAL_SIGNALS,
kg.retrieval.hit_at_5 and kg.retrieval.tokens_per_file, as that owner.

It also holds ``review``: per rule, the items of the latest build's review queue (``evo_agents.hub.kg_review``) open
to that owner through the hub sink, those a writer dismissed aside, with the build they were counted in; or why there
are none (a build without clusters has no queue).

``figures`` counts the night's ``kg_health`` over [since, until): the latest measure before ``until`` (its time, its
build, its sources, eval and review); ``coverage``, what the latest successful build before ``until`` covers
(``kg_coverage.build_coverage``); ``builds``, the builds that finished in the span, how many succeeded, failed or
changed nothing (a build whose content an earlier build had, ``artifact_reused_from``), with their coverage over time,
the latest first; and ``retrieval``, what agents got from the graph (``kg_coverage.retrieval``) in the sessions of the
figures and the runs that ended in the span, of a plan the owner may read or review runs. None for a project the hub
holds neither a graph nor a measure of.

GET /v1/projects/{p}/curator/kg-health lists the graph's health over time for a reader whose grant reaches the
project's default label through the sink (X-Evo-Sink, the hub sink by default), as the night's figures are read: the
latest measures, newest first, and the successful builds with what each covered and whether it changed anything.
``evo-agents hub kg health`` prints it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, exists, func, insert, literal, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_coverage, kg_health_eval, kg_labels, kg_review, tables
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.kg_build import kg_root
from evo_agents.hub.kg_graph import ArtifactMismatch, GraphCache, GraphUnavailable, HubSession, open_graph
from evo_agents.hub.server import hub_settings, kg_eval_sets, pulls
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.kg import built_graphs
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.service.views import visible_plans
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

NIGHT_SHIFT = "night_shift"
HEALTH_EVERY = timedelta(hours=6)  # a project's graph is measured again this long after its last measure
RETRY_AFTER = timedelta(hours=1)  # and this long after a measure that failed
ERROR_CHARS = 2000
GRAPHS_TRIED = 3  # successful builds the evaluation may fall back on when the blob store does not answer
MAX_SERIES = 10  # builds of the span the figures list
HISTORY = (1, 200, 30)  # the fewest, most and default measures and builds the route lists
CACHE = "health"  # the directory of the worker's cache (``kg_root``) the evaluated graphs are fetched into
NO_APP = "the hub has no GitHub App to read default branches with (EVO_HUB_GITHUB_APP_*)"
OFF_GITHUB = "the hub reads the default branches of repos on GitHub only"
NO_REPO = "the source reads a path, not a repo of the project with an origin"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


# Which projects


def _due(now: datetime | None):
    """A row per project to measure now: its id, name, and the owner of its night shift's first schedule."""
    s, p, u, h, cfg = tables.schedules, tables.projects, tables.users, tables.kg_health, tables.kg_configs
    moment = func.now() if now is None else literal(now)
    wait = case((h.c.error.is_(None), literal(HEALTH_EVERY)), else_=literal(RETRY_AFTER))
    recent = exists().where(h.c.project_id == p.c.id, h.c.measured_at > moment - wait)
    return (
        select(p.c.id.label("project_id"), p.c.name.label("project"), s.c.owner_id, u.c.login.label("owner"))
        .ext(distinct_on(p.c.id))
        .join_from(s, p, p.c.id == s.c.project_id)
        .join(u, u.c.id == s.c.owner_id)
        .where(s.c.kind == NIGHT_SHIFT, hub_settings.feature_on("curator", s.c.project_id))
        .where(exists().where(cfg.c.project_id == p.c.id), ~recent)
        .order_by(p.c.id, s.c.id)
    )


async def owner_access(conn: AsyncConnection, owner_id: int, login: str, project: str) -> ProjectAccess | None:
    """The access of the owner of a project's night shift, as a request of theirs would get it; None without one."""
    member = Principal(user_id=owner_id, login=login, admin=False, token_id=0, kind=MACHINE, token_hash="")
    try:
        return await project_access(conn, member, project)
    except HTTPException:
        return None


# One git source


def _source_entry(source: dict, held) -> dict:
    return {
        "source": source["id"],
        "repo": source.get("repo"),
        "ref": source.get("ref"),
        "commit": held.read_commit if held is not None else None,
        "build_id": held.build_id if held is not None else None,
        "default_branch": None,
        "head": None,
        "behind": None,
        "lag_hours": None,
        "status": None,
        "tree_files": None,
        "graph_files": held.file_count if held is not None else None,
        "missing": None,
        "stale": None,
        "deleted": None,
        "samples": None,
        "evidence": [],
        "why": None,
    }


async def _forge(app, token: str, owner: str, name: str, commit: str | None) -> dict:
    """The default branch of owner/name, its head, its files and how it stands to ``commit``, as GitHub says."""
    repo = await pulls.repository(app, token, owner, name)
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise GitHubUnavailable(f"GitHub names no default branch of {owner}/{name}")
    head = await pulls.branch_head(app, token, owner, name, branch)
    found = {"branch": branch, "head": head, "tree": await pulls.tree(app, token, owner, name, head["sha"])}
    if commit:
        try:
            found["compared"] = await pulls.compare(app, token, owner, name, commit, head["sha"])
        except GitHubRefused as exc:  # a commit GitHub does not know: pushed nowhere, or gone after a force-push
            found["compare_why"] = str(exc)
    return found


async def measure_source(app, source: dict, held, origin: str | None, now: datetime) -> dict:
    """The entry of git source ``source`` in a measure (see the module), ``held`` its kg_source_files row (None when
    no successful build holds it) and ``origin`` the origin of its repo."""
    entry = _source_entry(source, held)
    found = github_repo(origin) if isinstance(origin, str) and origin else None
    if held is None:
        entry["why"] = "no successful build of the graph holds this source yet"
    elif not source.get("repo"):
        entry["why"] = NO_REPO
    elif app is None:
        entry["why"] = NO_APP
    elif found is None:
        entry["why"] = OFF_GITHUB
    if entry["why"] is not None:
        return entry
    owner, name = found
    try:
        read = await pulls.with_token(
            app, owner, name, "tree", lambda token: _forge(app, token, owner, name, held.read_commit)
        )
    except (GitHubRefused, GitHubUnavailable) as exc:
        entry["why"] = str(exc)
        return entry
    entry.update(default_branch=read["branch"], head=read["head"]["sha"])
    compared = read.get("compared")
    if compared is not None:
        entry.update(behind=compared["ahead_by"], status=compared["status"])
        if compared["ahead_by"] == 0:
            entry["lag_hours"] = 0.0
        else:
            entry["lag_hours"] = kg_coverage.lag_hours(compared["oldest"], now)
    why = [read["compare_why"]] if read.get("compare_why") else []
    if not held.read_commit:
        why.append("the build names no commit this source read")
    if held.truncated:
        why.append(f"the build keeps the first {kg_coverage.MAX_FILES} files of this source only")
    elif read["tree"] is None:
        why.append(f"GitHub cut the tree of {owner}/{name} at {read['head']['sha'][:12]}: its files are not compared")
    else:
        files = held.files if isinstance(held.files, dict) else {}
        entry.update(kg_coverage.compare_files(source, read["tree"], files))
        entry["evidence"] = kg_coverage.file_evidence(source.get("repo"), entry["samples"])
        entry["off"] = kg_coverage.off_paths(source, read["tree"], files)
    entry["why"] = "; ".join(why) or None
    return entry


# The evaluation


def _evaluate(
    cache: GraphCache, blobs, project: str, access: ProjectAccess, sink: str, graphs, plans, previous, found_set=None
):
    """The eval of a measure (see the module), ``found_set`` the project's latest question set and its digest, if any.
    Blocking: run it in a thread."""
    ceiling = access.rules.ceiling(access.max_level, sink)
    path, graph = cache.acquire(blobs, project, graphs)
    try:
        store = open_graph(path, project, graph)
        try:
            b = store.latest_ready()
            if b is None:
                return {"why": f"build {graph.build_id} holds no ready graph"}
            commits = kg_health_eval.GraphCommits(store, b)
            question_set, set_digest = found_set if found_set else (None, None)
            holdout = kg_health_eval.holdout_questions(question_set, project)
            held = {q.id for q in question_set.questions} if question_set else set()
            steps, skipped, available, sources = kg_health_eval.plan_questions(project, plans, commits, held)
            questions = holdout + steps
            digest = kg_health_eval.questions_digest(questions)
            if (
                isinstance(previous, dict)
                and previous.get("content_hash") == graph.content_hash
                and previous.get("digest") == digest
                and previous.get("sink") == sink
                and previous.get("methods") is not None
            ):
                return {**previous, "build_id": graph.build_id}
            session = HubSession(project, access.rules.policy, ceiling, sink, store, graph, graphs[0])
            about = None
            if question_set is not None:
                about = {"name": question_set.name, "version": question_set.version, "digest": set_digest}
                about["holdout"] = len(holdout)
            scored = kg_health_eval.score(
                questions,
                skipped,
                available,
                session,
                plan_steps=len(steps),
                sources=sources,
                question_set=about,
                sink=sink,
            )
            return {**scored, "build_id": graph.build_id, "content_hash": graph.content_hash}
        finally:
            store.close()
    finally:
        cache.release(path)


async def _plans(conn: AsyncConnection, access: ProjectAccess) -> list[tuple[str, dict]]:
    p = tables.plans
    readable = await visible_plans(conn, access, None)
    if not readable:
        return []
    rows = await conn.execute(
        select(p.c.plan_id, p.c.body)
        .where(p.c.project_id == access.project_id, p.c.plan_id.in_(readable))
        .order_by(p.c.plan_id)
    )
    return [(row.plan_id, row.body) for row in rows if isinstance(row.body, dict)]


async def _previous_eval(conn: AsyncConnection, project_id: int):
    h = tables.kg_health
    query = (
        select(h.c.body["eval"])
        .where(h.c.project_id == project_id, h.c.error.is_(None))
        .order_by(h.c.measured_at.desc(), h.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).scalar_one_or_none()


async def _owner_reads(conn: AsyncConnection, row, blobs, data_dir: Path | None, graphs, agent: bool = False) -> tuple:
    """(the access of ``row``'s owner, the project's hub sink, None), or (None, None, why the graph cannot be read
    as the owner reads it). With ``agent``, the sink is kg_health_eval.AGENT_SINK when the project's policy declares
    it, as the agents that search the graph read it."""
    if not graphs:
        return None, None, "the project has no graph on the hub yet"
    if blobs is None or data_dir is None:
        return None, None, "this process has no blob store and cache to read the graph from"
    access = await owner_access(conn, row.owner_id, row.owner, row.project)
    if access is None:
        return None, None, f"{row.owner}, who owns the night shift, holds no grant on the project"
    sink = access.rules.hub_sink.sink if access.rules.hub_sink else None
    if agent and access.rules.ceiling(access.max_level, kg_health_eval.AGENT_SINK) is not None:
        sink = kg_health_eval.AGENT_SINK
    if sink is None:
        return None, None, "the project declares no hub sink to read its graph through"
    return access, sink, None


async def measure_eval(engine: AsyncEngine, row, blobs, data_dir: Path | None, graphs) -> dict:
    """The eval of the measure of ``row``'s project (a row of ``_due``)."""
    async with engine.begin() as conn:
        access, sink, why = await _owner_reads(conn, row, blobs, data_dir, graphs, agent=True)
        if why is not None:
            return {"why": why}
        plans = await _plans(conn, access)
        previous = await _previous_eval(conn, row.project_id)
        found_set = await kg_eval_sets.latest_set(conn, row.project_id)
    cache = GraphCache(kg_root(data_dir) / CACHE, keep=1)
    try:
        return await asyncio.to_thread(
            _evaluate, cache, blobs, row.project, access, sink, graphs, plans, previous, found_set
        )
    except (GraphUnavailable, ArtifactMismatch) as exc:
        return {"why": str(exc)}
    except Exception as exc:  # the measure of the sources stands without a score
        log.exception("the retrieval evaluation of a graph failed", extra={"project": row.project})
        return {"why": f"the evaluation failed: {type(exc).__name__}"}


# The review queue


def _review(cache: GraphCache, blobs, project: str, access: ProjectAccess, sink: str, graphs, dismissed: set[str]):
    """The review of a measure (see the module). Blocking: run it in a thread."""
    ceiling = access.rules.ceiling(access.max_level, sink)
    path, graph = cache.acquire(blobs, project, graphs)
    try:
        store = open_graph(path, project, graph)
        try:
            session = HubSession(project, access.rules.policy, ceiling, sink, store, graph, graphs[0])
            if session.error:
                return {"why": session.error}
            found, b = session._store()
            if found.clusters(b) is None and not found.review_items(b):
                return {"why": kg_review.NO_REVIEW, "build_id": graph.build_id}
            counts = kg_review.open_counts(found, b, session.visible, dismissed)
            return {"build_id": graph.build_id, "open": counts}
        finally:
            store.close()
    finally:
        cache.release(path)


async def measure_review(engine: AsyncEngine, row, blobs, data_dir: Path | None, graphs) -> dict:
    """The open items per rule of the review queue of ``row``'s project, as its owner reads them through the hub
    sink, the dismissed ones aside (``evo_agents.hub.kg_review``)."""
    d = tables.kg_review_dismissals
    async with engine.begin() as conn:
        access, sink, why = await _owner_reads(conn, row, blobs, data_dir, graphs)
        if why is not None:
            return {"why": why}
        dismissed = set((await conn.execute(select(d.c.fingerprint).where(d.c.project_id == row.project_id))).scalars())
    cache = GraphCache(kg_root(data_dir) / CACHE, keep=1)
    try:
        return await asyncio.to_thread(_review, cache, blobs, row.project, access, sink, graphs, dismissed)
    except (GraphUnavailable, ArtifactMismatch) as exc:
        return {"why": str(exc)}
    except Exception as exc:  # the measure stands without the queue's counts
        log.exception("counting the review queue of a graph failed", extra={"project": row.project})
        return {"why": f"counting the review queue failed: {type(exc).__name__}"}


# The measure


EVAL_SIGNALS = (
    (
        "kg.retrieval.hit_at_5",
        "percent",
        "Of the retrieval questions the hub asks its latest graph (the holdout part of the project's question set and "
        "the done plan steps), the percent whose answer kg_search puts in its first 5 results (kg_health.eval).",
        kg_health_eval.hit_percent,
    ),
    (
        "kg.retrieval.tokens_per_file",
        "tokens",
        "The tokens kg_search spends per answer file it finds in its first 5 results (its mean tokens over Recall@5) "
        "on the retrieval questions the hub asks its latest graph (kg_health.eval).",
        kg_health_eval.tokens_per_file,
    ),
)


async def push_eval_signals(conn: AsyncConnection, project_id: int, owner_id: int | None, measure_id: int, found):
    """Push EVAL_SIGNALS of the measure ``measure_id``'s eval as the night shift's owner (``kg_labels.push_number``),
    once per measure; an eval without a score pushes nothing. The keys pushed."""
    pushed = []
    for key, unit, words, value_of in EVAL_SIGNALS:
        value = value_of(found)
        if value is None or owner_id is None:
            continue
        external_id = f"kg_health:{key}:{measure_id}"
        if await kg_labels.push_number(conn, project_id, owner_id, key, unit, words, value, external_id):
            pushed.append(key)
    return pushed


async def measure_project(engine: AsyncEngine, row, *, github_app, blobs, data_dir, now: datetime | None) -> tuple:
    """(the measure of ``row``'s project, the build it read or None)."""
    cfg, held, pr = tables.kg_configs, tables.kg_source_files, tables.project_repos
    async with engine.begin() as conn:
        knowledge = (
            await conn.execute(select(cfg.c.knowledge).where(cfg.c.project_id == row.project_id))
        ).scalar_one_or_none()
        graphs = await built_graphs(conn, row.project_id, GRAPHS_TRIED)
        files = {
            found.source: found for found in await conn.execute(select(held).where(held.c.project_id == row.project_id))
        }
        origins = dict(
            (await conn.execute(select(pr.c.name, pr.c.origin).where(pr.c.project_id == row.project_id))).all()
        )
    moment = _now(now)
    sources = [
        await measure_source(github_app, source, files.get(source["id"]), origins.get(source.get("repo")), moment)
        for source in kg_coverage.git_sources(knowledge)
    ]
    latest = graphs[0] if graphs else None
    build = (
        {"id": latest.build_id, "content_hash": latest.content_hash, "finished_at": latest.finished_at.isoformat()}
        if latest
        else None
    )
    body = {
        "build": build,
        "sources": sources,
        "eval": await measure_eval(engine, row, blobs, data_dir, graphs),
        "review": await measure_review(engine, row, blobs, data_dir, graphs),
    }
    return body, latest.build_id if latest else None


async def measure(engine: AsyncEngine, *, github_app=None, blobs=None, data_dir=None, now=None) -> dict:
    """Measure each project that is due (see the module), each on its own: how many were measured, and how many
    failed."""
    async with engine.begin() as conn:
        due = (await conn.execute(_due(now))).all()
    outcomes: Counter = Counter()
    h = tables.kg_health
    for row in due:
        error = None
        try:
            body, build_id = await measure_project(
                engine, row, github_app=github_app, blobs=blobs, data_dir=data_dir, now=now
            )
        except Exception as exc:  # one project failing leaves the others their measure
            log.exception("measuring a knowledge graph failed", extra={"project": row.project})
            body, build_id, error = {}, None, (f"{type(exc).__name__}: {exc}")[:ERROR_CHARS]
        values = {"project_id": row.project_id, "build_id": build_id, "body": body, "error": error}
        async with engine.begin() as conn:
            stamp = {"measured_at": now} if now is not None else {}
            measure_id = (await conn.execute(insert(h).values(**values, **stamp).returning(h.c.id))).scalar_one()
            await push_eval_signals(conn, row.project_id, row.owner_id, measure_id, body.get("eval"))
        outcomes["failed" if error else "measured"] += 1
        log.info("knowledge graph measured", extra={"project": row.project, "failed": error is not None})
    return dict(sorted(outcomes.items()))


# The night's figures


def _span_builds(project_id: int, since: datetime, until: datetime):
    b = tables.kg_builds
    return (
        select(b.c.id, b.c.status, b.c.finished_at, b.c.artifact_reused_from, b.c.coverage)
        .where(b.c.project_id == project_id, b.c.finished_at >= since, b.c.finished_at < until)
        .order_by(b.c.id.desc())
    )


def _builds(rows) -> dict:
    states = Counter(row.status for row in rows)
    succeeded = [row for row in rows if row.status == "succeeded"]
    unchanged = sum(1 for row in succeeded if row.artifact_reused_from is not None)
    series = []
    for row in succeeded[:MAX_SERIES]:
        coverage = row.coverage if isinstance(row.coverage, dict) else {}
        python = coverage.get("python") if isinstance(coverage.get("python"), dict) else {}
        series.append(
            {
                "id": row.id,
                "finished_at": row.finished_at.isoformat(),
                "changed": row.artifact_reused_from is None,
                **{key: coverage.get(key) for key in ("resolved_ratio", "ambiguous_ratio", "dangling_ratio")},
                "uncalled_method_ratio": python.get("uncalled_method_ratio"),
            }
        )
    return {
        "builds": len(rows),
        "succeeded": len(succeeded),
        "failed": states.get("failed", 0),
        "unchanged": unchanged,
        "unchanged_rate": kg_coverage.ratio(unchanged, len(succeeded)),
        "series": series,
    }


def _run_kg(project_id: int, plans: list[str], since: datetime, until: datetime):
    f, r = tables.session_facets, tables.runs
    return (
        select(f.c.run_id, f.c.body["kg"].label("kg"))
        .join_from(f, r, r.c.id == f.c.run_id)
        .where(f.c.project_id == project_id, f.c.kind == "run", f.c.body.is_not(None))
        .where(r.c.kind != "local", r.c.plan_id.in_(plans) | (r.c.kind == "review"))
        .where(r.c.finished_at >= since, r.c.finished_at < until)
        .order_by(f.c.run_id)
    )


async def figures(
    conn: AsyncConnection, access: ProjectAccess, plans: list[str], since: datetime, until: datetime, sessions
) -> dict | None:
    """The night's ``kg_health`` (see the module); ``sessions`` are the sessions of the figures
    (``facet_figures.Session``)."""
    h, b = tables.kg_health, tables.kg_builds
    measured = (
        await conn.execute(
            select(h.c.measured_at, h.c.body)
            .where(h.c.project_id == access.project_id, h.c.error.is_(None), h.c.measured_at < until)
            .order_by(h.c.measured_at.desc(), h.c.id.desc())
            .limit(1)
        )
    ).one_or_none()
    latest = (
        await conn.execute(
            select(b.c.id, b.c.finished_at, b.c.coverage)
            .where(b.c.project_id == access.project_id, b.c.status == "succeeded", b.c.coverage.is_not(None))
            .where(b.c.finished_at < until)
            .order_by(b.c.id.desc())
            .limit(1)
        )
    ).one_or_none()
    rows = (await conn.execute(_span_builds(access.project_id, since, until))).all()
    if measured is None and latest is None and not rows:
        return None
    bodies = [
        (f"session:{session.session}" if session.run is None else f"run:{session.run}:1", session.lists.get("kg"))
        for session in sessions
    ]
    bodies += [
        (f"run:{row.run_id}:1", row.kg) for row in await conn.execute(_run_kg(access.project_id, plans, since, until))
    ]
    body = measured.body if measured is not None and isinstance(measured.body, dict) else {}
    coverage = (
        {"build_id": latest.id, "finished_at": latest.finished_at.isoformat(), **latest.coverage} if latest else None
    )
    sources = [
        {key: value for key, value in entry.items() if key != "off"} if isinstance(entry, dict) else entry
        for entry in body.get("sources") or []
    ]  # the paths a source's files are off by are for the tier rules; the figures keep their counts and samples
    return {
        "measured_at": measured.measured_at.isoformat() if measured is not None else None,
        "build": body.get("build"),
        "sources": sources,
        "eval": body.get("eval"),
        "review": body.get("review"),
        "coverage": coverage,
        "builds": _builds(rows),
        "retrieval": kg_coverage.retrieval(bodies),
    }


# The route


class KgMeasure(BaseModel):
    measured_at: datetime
    build_id: int | None = Field(description="the build whose graph the measure read")
    error: str | None = Field(description="why the measure failed; null when it did not")
    build: dict | None = Field(description="that build: id, content_hash, finished_at")
    sources: list[dict] = Field(
        description="each git source: the commit it read, its default branch and head, the commits behind and the hours "
        "the oldest waited, the files missing, stale and deleted with samples and evidence, and why for what was not read"
    )
    eval: dict | None = Field(description="the retrieval evaluation of the build's graph, or why there is none")


class KgBuildCoverage(BaseModel):
    id: int
    finished_at: datetime
    changed: bool = Field(description="false when an earlier build had the same content")
    coverage: dict | None = Field(description="what the build covers (kg_builds.coverage); null before schema 0055")


class KgHealthView(BaseModel):
    project: str
    measures: list[KgMeasure] = Field(description="newest first")
    builds: list[KgBuildCoverage] = Field(description="the successful builds, newest first")


@router.get("/{project}/curator/kg-health", response_model=KgHealthView, responses=READ_REFUSALS)
async def show_kg_health(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=HISTORY[0], le=HISTORY[1], description="measures and builds to list")] = HISTORY[2],
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> KgHealthView:
    """The health of the project's knowledge graph over time: its latest measures and its successful builds with what
    each covers, for a reader whose grant reaches the project's default label through the sink (the hub sink by
    default), as the night's figures are."""
    h, b = tables.kg_health, tables.kg_builds
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the Curator of project {access.name} needs a grant on it")
        if not access.visible(access.rules.default_label, plan_routes._sink(access, sink)):
            raise HTTPException(404, f"project {project} has no measure of its knowledge graph you can read")
        measures = await conn.execute(
            select(h.c.measured_at, h.c.build_id, h.c.error, h.c.body)
            .where(h.c.project_id == access.project_id)
            .order_by(h.c.measured_at.desc(), h.c.id.desc())
            .limit(limit)
        )
        builds = await conn.execute(
            select(b.c.id, b.c.finished_at, b.c.artifact_reused_from, b.c.coverage)
            .where(b.c.project_id == access.project_id, b.c.status == "succeeded")
            .order_by(b.c.id.desc())
            .limit(limit)
        )
        found = [
            KgMeasure(
                measured_at=row.measured_at,
                build_id=row.build_id,
                error=row.error,
                build=row.body.get("build"),
                sources=row.body.get("sources") or [],
                eval=row.body.get("eval"),
            )
            for row in measures
        ]
        covered = [
            KgBuildCoverage(
                id=row.id,
                finished_at=row.finished_at,
                changed=row.artifact_reused_from is None,
                coverage=row.coverage,
            )
            for row in builds
        ]
    return KgHealthView(project=access.name, measures=found, builds=covered)
