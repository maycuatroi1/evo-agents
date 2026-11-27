"""The health of a project's knowledge graph (``evo_agents.hub.server.kg_health`` says what a measure holds): which
projects are due, the measure of each git source against the forge, the eval and the review of the graph as the night
shift's owner reads it, the signals the eval pushes, the night's figures, and the history the route lists. Each step
of the job opens its own transactions with UnitOfWork; the queries are ``kg.repository.health``'s."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_coverage, kg_health_eval, kg_labels, kg_review, kg_snapshots
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.heap import give_back
from evo_agents.hub.kg_graph import GraphUnavailable, HubSession, reading
from evo_agents.hub.kg_snapshots import Held
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.forge.models.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.kg.models.health import (
    ERROR_CHARS,
    EVAL_SIGNALS,
    GRAPHS_TRIED,
    MAX_SERIES,
    NO_APP,
    NO_REPO,
    OFF_GITHUB,
    KgBuildCoverage,
    KgHealthView,
    KgMeasure,
)
from evo_agents.hub.server.kg.repository import health as repository
from evo_agents.hub.server.kg.repository.graph import built_graphs
from evo_agents.hub.server.kg.service.eval_sets import latest_set
from evo_agents.hub.server.kg_audience import agent_audience
from evo_agents.hub.server.merge.service import pulls
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.service.views import visible_plans
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger("evo_agents.hub.server.kg_health")  # the name its log lines had before the layers


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


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
    if entry["why"] is not None or held is None or app is None or found is None:  # held, app and found: for the types
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


def audience_digest(audience: frozenset[str]) -> str:
    """A digest of an audience, which an eval keeps so that one read with another audience is asked again."""
    return hashlib.sha256("\n".join(sorted(audience)).encode()).hexdigest()


def snapshot_of(loaded: Held | None, graphs, ceiling):
    """A view of ``loaded`` for ``kg_graph.reading`` as ``ceiling``'s audience reads it, with the one of ``graphs`` it
    was made of; None without it or when it is of none of them (no graph is read then)."""
    graph = next((g for g in graphs if loaded is not None and g.build_id == loaded.build_id), None)
    if loaded is None or graph is None:
        return None
    return loaded.view(ceiling.audience if ceiling is not None else frozenset()), graph


def evaluate(
    project: str,
    access: ProjectAccess,
    sink: str,
    graphs,
    plans,
    previous,
    found_set=None,
    audience: frozenset[str] = frozenset(),
    loaded: Held | None = None,
):
    """The eval of a measure (see the module), ``found_set`` the project's latest question set and its digest, if any,
    read with ``audience``, the owner's (``kg_audience.agent_audience``), as the agents' kg_search reads the graph:
    from ``loaded``, the snapshot of the hub's tables, when it is of one of ``graphs``.
    Blocking: run it in a thread."""
    ceiling = access.rules.ceiling(access.max_level, sink, audience)
    heard = audience_digest(audience)
    with reading(project, snapshot_of(loaded, graphs, ceiling)) as (store, graph):
        b = store.latest_ready()
        if b is None:
            return {"why": f"build {graph.build_id} holds no ready graph"}
        commits = kg_health_eval.GraphCommits(store)
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
            and previous.get("audience") == heard
            and previous.get("methods") is not None
            and (previous.get("holdout") is not None or not holdout)
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
            holdout=[q.id for q in holdout],
        )
        return {**scored, "audience": heard, "build_id": graph.build_id, "content_hash": graph.content_hash}


async def _plans(conn: AsyncConnection, access: ProjectAccess) -> list[tuple[str, dict]]:
    readable = await visible_plans(conn, access, None)
    if not readable:
        return []
    return await repository.plan_bodies(conn, access.project_id, readable)


async def owner_reads(conn: AsyncConnection, row, graphs, agent: bool = False) -> tuple:
    """(the access of ``row``'s owner, the project's hub sink, None), or (None, None, why the graph cannot be read
    as the owner reads it). With ``agent``, the sink is kg_health_eval.AGENT_SINK when the project's policy declares
    it, as the agents that search the graph read it."""
    if not graphs:
        return None, None, "the project has no graph on the hub yet"
    access = await owner_access(conn, row.owner_id, row.owner, row.project)
    if access is None:
        return None, None, f"{row.owner}, who owns the night shift, holds no grant on the project"
    sink = access.rules.hub_sink.sink if access.rules.hub_sink else None
    if agent and access.rules.ceiling(access.max_level, kg_health_eval.AGENT_SINK) is not None:
        sink = kg_health_eval.AGENT_SINK
    if sink is None:
        return None, None, "the project declares no hub sink to read its graph through"
    return access, sink, None


async def load_snapshot(engine: AsyncEngine, row, graphs) -> tuple[Held | None, dict | None]:
    """(the snapshot of ``row``'s project the eval and the review of its measure read, None), or (None, why the
    reads stand without it): none is loaded for a project without a graph on the hub, nor when its owner may read
    the graph through no sink (the agents' sink reads at least what the hub sink does, so neither read needs it)."""
    if not graphs:
        return None, None
    async with engine.connect() as conn:
        if (await owner_reads(conn, row, graphs, agent=True))[2] is not None:
            return None, None
    try:
        return await kg_snapshots.load(engine, row.project_id), None
    except Exception as exc:  # the measure of the sources stands without a score and a queue
        log.exception("loading the snapshot of a graph failed", extra={"project": row.project})
        return None, {"why": f"loading the graph failed: {type(exc).__name__}"}


async def measure_eval(
    engine: AsyncEngine, row, blobs, data_dir: Path | None, graphs, loaded: Held | None, failed: dict | None = None
) -> dict:
    """The eval of the measure of ``row``'s project (a row of ``repository.due``) on ``loaded`` (``load_snapshot``),
    or ``failed``, why it could not be loaded."""
    async with UnitOfWork(engine) as conn:
        access, sink, why = await owner_reads(conn, row, graphs, agent=True)
        if why is not None:
            return {"why": why}
        audience = await agent_audience(conn, row.owner_id, None, None)
        plans = await _plans(conn, access)
        previous = await repository.previous_eval(conn, row.project_id)
        found_set = await latest_set(conn, row.project_id)
    if failed is not None:
        return failed
    try:
        return await asyncio.to_thread(
            evaluate,
            row.project,
            access,
            sink,
            graphs,
            plans,
            previous,
            found_set,
            audience=audience,
            loaded=loaded,
        )
    except GraphUnavailable as exc:
        return {"why": str(exc)}
    except Exception as exc:  # the measure of the sources stands without a score
        log.exception("the retrieval evaluation of a graph failed", extra={"project": row.project})
        return {"why": f"the evaluation failed: {type(exc).__name__}"}


# The review queue


def review(
    project: str,
    access: ProjectAccess,
    sink: str,
    graphs,
    dismissed: set[str],
    loaded: Held | None = None,
):
    """The review of a measure (see the module), from ``loaded``, the snapshot of the hub's tables, when it is of one
    of ``graphs``, else from the graph file. Blocking: run it in a thread."""
    ceiling = access.rules.ceiling(access.max_level, sink)
    with reading(project, snapshot_of(loaded, graphs, ceiling)) as (store, graph):
        session = HubSession(project, access.rules.policy, ceiling, sink, store, graph, graphs[0])
        if session.error:
            return {"why": session.error}
        found = session.store()
        if found.clusters() is None and not found.review_items():
            return {"why": kg_review.NO_REVIEW, "build_id": graph.build_id}
        counts = kg_review.open_counts(found, session.visibility, dismissed)
        return {"build_id": graph.build_id, "open": counts}


async def measure_review(
    engine: AsyncEngine, row, blobs, data_dir: Path | None, graphs, loaded: Held | None, failed: dict | None = None
) -> dict:
    """The open items per rule of the review queue of ``row``'s project, as its owner reads them through the hub
    sink, the dismissed ones aside (``evo_agents.hub.kg_review``), on ``loaded``, or ``failed``."""
    async with UnitOfWork(engine) as conn:
        access, sink, why = await owner_reads(conn, row, graphs)
        if why is not None:
            return {"why": why}
        dismissed = await repository.dismissed(conn, row.project_id)
    if failed is not None:
        return failed
    try:
        return await asyncio.to_thread(review, row.project, access, sink, graphs, dismissed, loaded=loaded)
    except GraphUnavailable as exc:
        return {"why": str(exc)}
    except Exception as exc:  # the measure stands without the queue's counts
        log.exception("counting the review queue of a graph failed", extra={"project": row.project})
        return {"why": f"counting the review queue failed: {type(exc).__name__}"}


# The measure


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
    async with UnitOfWork(engine) as conn:
        knowledge = await repository.knowledge(conn, row.project_id)
        graphs = await built_graphs(conn, row.project_id, GRAPHS_TRIED)
        files = await repository.source_files(conn, row.project_id)
        origins = await repository.origins(conn, row.project_id)
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
    loaded, failed = await load_snapshot(engine, row, graphs)
    body = {
        "build": build,
        "sources": sources,
        "eval": await measure_eval(engine, row, blobs, data_dir, graphs, loaded, failed),
        "review": await measure_review(engine, row, blobs, data_dir, graphs, loaded, failed),
    }
    return body, latest.build_id if latest else None


async def measure(engine: AsyncEngine, *, github_app=None, blobs=None, data_dir=None, now=None) -> dict:
    """Measure each project that is due (see the module), each on its own: how many were measured, and how many
    failed."""
    async with UnitOfWork(engine) as conn:
        due = await repository.due(conn, now)
    outcomes: Counter = Counter()
    for row in due:
        error = None
        try:
            body, build_id = await measure_project(
                engine, row, github_app=github_app, blobs=blobs, data_dir=data_dir, now=now
            )
        except Exception as exc:  # one project failing leaves the others their measure
            log.exception("measuring a knowledge graph failed", extra={"project": row.project})
            body, build_id, error = {}, None, (f"{type(exc).__name__}: {exc}")[:ERROR_CHARS]
        give_back()  # the project's snapshot is gone: what it took goes back before the next one is made
        values = {"project_id": row.project_id, "build_id": build_id, "body": body, "error": error}
        async with UnitOfWork(engine) as conn:
            stamp = {"measured_at": now} if now is not None else {}
            measure_id = await repository.add_measure(conn, {**values, **stamp})
            await push_eval_signals(conn, row.project_id, row.owner_id, measure_id, body.get("eval"))
        outcomes["failed" if error else "measured"] += 1
        log.info("knowledge graph measured", extra={"project": row.project, "failed": error is not None})
    return dict(sorted(outcomes.items()))


# The night's figures


def _builds(rows) -> dict:
    states = Counter(row.status for row in rows)
    succeeded = [row for row in rows if row.status == "succeeded"]
    unchanged = sum(1 for row in succeeded if row.unchanged)
    series = []
    for row in succeeded[:MAX_SERIES]:
        coverage = row.coverage if isinstance(row.coverage, dict) else {}
        python = coverage.get("python")
        python = python if isinstance(python, dict) else {}
        series.append(
            {
                "id": row.id,
                "finished_at": row.finished_at.isoformat(),
                "changed": not row.unchanged,
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


async def figures(
    conn: AsyncConnection, access: ProjectAccess, plans: list[str], since: datetime, until: datetime, sessions
) -> dict | None:
    """The night's ``kg_health`` (see the module); ``sessions`` are the sessions of the figures
    (``facet_figures.Session``)."""
    measured = await repository.latest_measure(conn, access.project_id, until)
    latest = await repository.latest_covered(conn, access.project_id, until)
    rows = await repository.span_builds(conn, access.project_id, since, until)
    if measured is None and latest is None and not rows:
        return None
    bodies = [
        (f"session:{session.session}" if session.run is None else f"run:{session.run}:1", session.lists.get("kg"))
        for session in sessions
    ]
    bodies += [
        (f"run:{row.run_id}:1", row.kg) for row in await repository.run_kg(conn, access.project_id, plans, since, until)
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


async def history(engine: AsyncEngine, user: Principal, project: str, limit: int, sink: str | None) -> KgHealthView:
    """The latest ``limit`` measures and successful builds of the project's graph, for a reader whose grant reaches
    its default label through ``sink``: 403 without a grant, 404 when the sink does not reach it."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the Curator of project {access.name} needs a grant on it")
        if not plan_service.sees(access, access.rules.default_label, sink):
            raise HTTPException(404, f"project {project} has no measure of its knowledge graph you can read")
        measures = await repository.measures(conn, access.project_id, limit)
        builds = await repository.succeeded_builds(conn, access.project_id, limit)
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
        KgBuildCoverage(id=row.id, finished_at=row.finished_at, changed=not row.unchanged, coverage=row.coverage)
        for row in builds
    ]
    return KgHealthView(project=access.name, measures=found, builds=covered)
