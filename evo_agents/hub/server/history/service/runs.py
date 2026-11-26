"""The night's history run: queueing it (``queue_history``), its claim (``history_claim``), the batches its worker
reads (``next_batch``) and what it reports (``report``), its end (``history_ended``), the clusters of the facets
(``regroup``, ``clusters_of``, ``figures``) and where it stands (``status``, ``clusters``). See the package's
docstring."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from collections import defaultdict
from datetime import datetime, timedelta

import psycopg
from sqlalchemy import null
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, excerpts, facet_job, history, runs
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.history import repository
from evo_agents.hub.server.history.models import (
    CANDIDATES,
    LONG_WAIT,
    FacetCluster,
    FacetClusterList,
    FacetReport,
    HistoryBatch,
    HistoryReport,
    HistoryReported,
    HistoryRunView,
    HistoryStatus,
    LlmCoverage,
    NameReport,
)
from evo_agents.hub.server.history.service.readers import Reader, owner_access, reader_of
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.run_state import RunStep, notify_queued, write_event
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.security import Principal

log = logging.getLogger("evo_agents.hub.server.history")

HISTORY = "curator.history"  # the night shift queued the night's history run: "<project> run:<id> night=<date>"
SOURCES = {"claude-code": "Claude Code session", "codex": "Codex session"}


# The items


async def coverage(conn: AsyncConnection, reader: Reader) -> dict:
    """How far the facets of the model of the project are, as ``reader`` may read its sessions and runs."""
    rows = [row for row in await repository.eligible_facets(conn, reader.access.project_id) if reader.reads(row)]
    analyzed = sum(1 for row in rows if row.current)
    failed = sum(1 for row in rows if not row.current and row.llm_attempts)
    return {
        "version": history.LLM_VERSION,
        "items": len(rows),
        "analyzed": analyzed,
        "waiting": len(rows) - analyzed,
        "failed": failed,
    }


async def waiting_items(conn: AsyncConnection, reader: Reader, now: datetime, limit: int) -> list:
    """The items that wait for a facet of the model now, the latest first, at most ``limit`` of them."""
    scanned = limit * 20 + 200  # an owner who may read few of them waits for the next batch
    found = []
    for row in await repository.waiting_facets(conn, reader.access.project_id, now, scanned):
        if reader.reads(row):
            found.append(row)
            if len(found) >= limit:
                break
    return found


def _source(row) -> str:
    if row.kind == "run":
        return f"a run of kind {row.run_kind} on {row.runtime or 'a runtime'}"
    return SOURCES.get(row.runtime or "", "session")


async def _session_lines(engine: AsyncEngine, store, row) -> excerpts.Cutter:
    async with engine.connect() as conn:
        parts = await repository.session_parts(conn, row.project_id, row.session_id)
    if not parts:
        raise facet_job.Unavailable("the session's own transcript is no longer on the hub")
    return await asyncio.to_thread(lambda: excerpts.session_excerpt(facet_job.file_lines(store, parts, None)))


async def _run_lines(engine: AsyncEngine, store, row) -> excerpts.Cutter:
    builder = excerpts.RunEntries()
    if row.log_sha256 and store is not None:
        try:
            await asyncio.to_thread(facet_job.read_log, store, row.log_sha256, builder, None)
            return builder.close()
        except facet_job.Unavailable:
            if await asyncio.to_thread(store.has_blob, row.log_sha256):
                raise  # the log is there but cannot be read now: try again later
            builder = excerpts.RunEntries()
    await facet_job.stored_events(engine, row.run_id, builder)
    return builder.close()


async def excerpt_of(engine: AsyncEngine, store, row) -> dict:
    """The batch item of ``row``: its excerpt and what it is; Unavailable when its content cannot be read now."""
    if row.kind == "session" and store is None:
        raise facet_job.Unavailable("the hub has no blob store")
    cutter = await (_session_lines if row.kind == "session" else _run_lines)(engine, store, row)
    return {
        "item": history.item_key(row.kind, row.session_id, row.run_id),
        "kind": row.kind,
        "source": _source(row),
        "fingerprint": row.fingerprint,
        "lines": cutter.count,
        "anchors": cutter.anchors,
        "cut": cutter.cut,
        "excerpt": cutter.text(),
    }


def _wait_of(attempts: int, now: datetime) -> datetime:
    return now + (LONG_WAIT if attempts >= history.MAX_ATTEMPTS else timedelta(hours=history.RETRY_HOURS))


async def item_failed(conn: AsyncConnection, facet_id: int, error: str, now: datetime) -> None:
    """Record a failed attempt at the facet of the model of facet ``facet_id``: it waits (``_wait_of``)."""
    attempts = await repository.attempts_of(conn, facet_id) + 1
    values = {
        "llm_attempts": attempts,
        "llm_error": error[: history.MAX_ERROR_CHARS],
        "llm_retry_at": _wait_of(attempts, now),
    }
    await repository.set_facet(conn, facet_id, values)


# The clusters


def _evidence(row) -> str:
    return f"run:{row.run_id}:1" if row.kind == "run" else f"session:{row.session_id}"


def _groups(rows, reader: Reader) -> dict[str, dict]:
    groups: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "runs": 0, "members": set(), "ex": [], "samples": []})
    for row in rows:
        if not reader.reads(row) or not isinstance(row.llm, dict):
            continue
        group = groups[history.cluster_key(row.llm)]
        group["sessions"] += 1
        group["runs"] += row.kind == "run"
        if row.user_id is not None:
            group["members"].add(row.user_id)
        if len(group["ex"]) < history.CLUSTER_EXAMPLES:
            group["ex"].append({"evidence": _evidence(row), "at": row.at.isoformat() if row.at else None})
        if len(group["samples"]) < history.MAX_GROUP_SAMPLES:
            group["samples"].append(
                {"summary": row.llm.get("summary") or "", "friction": row.llm.get("friction") or []}
            )
    return groups


async def regroup(conn: AsyncConnection, reader: Reader) -> dict[str, dict]:
    """Count the project's groups of facets of the model again, as ``reader`` reads them, into facet_clusters; each
    group by its key, with some of its facets (``samples``). A group no facet falls into any more counts nothing."""
    project_id = reader.access.project_id
    groups = _groups(await repository.llm_facets(conn, project_id), reader)
    counted = {
        key: {
            "sessions": group["sessions"],
            "runs": group["runs"],
            "member_ids": sorted(group["members"]),
            "examples": group["ex"],
        }
        for key, group in groups.items()
    }
    await repository.store_counts(conn, project_id, counted, await repository.now(conn))
    return dict(groups)


async def clusters_of(conn: AsyncConnection, project_id: int) -> tuple[list[dict], int, datetime | None]:
    """(the clusters of MIN_CLUSTER_SESSIONS sessions or more as the hub last counted them, how many smaller ones,
    when it counted them)."""
    rows = await repository.cluster_rows(conn, project_id)
    kept, hidden = history.shown(history.merged(row._mapping for row in rows))
    counted = max((row.counted_at for row in rows if row.counted_at is not None), default=None)
    return kept, hidden, counted


async def figures(conn: AsyncConnection, access: ProjectAccess) -> dict:
    """What the night's figures hold of the facets of the model, as the charter's owner (``access``) reads them: the
    clusters, counted again, and how far the facets are."""
    reader = await reader_of(conn, access)
    await regroup(conn, reader)
    kept, _, _ = await clusters_of(conn, access.project_id)
    return {"clusters": kept, "llm": await coverage(conn, reader)}


# Queueing


async def _has_work(conn: AsyncConnection, reader: Reader, now: datetime) -> bool:
    if await waiting_items(conn, reader, now, 1):
        return True
    return await repository.has_unnamed(conn, reader.access.project_id)


def _values(due, gate, runtime: str, model: str | None, budget: dict) -> dict:
    return {
        "kind": "history",
        "project_id": due.project_id,
        "plan_id": None,
        "title": f"History of the night of {gate.night.isoformat()}",
        "plan_revision": None,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        **gate.duty.columns(),
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": model,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "max_attempts": 1,
        "repos": null(),  # SQL NULL, not a JSON null: a history run has no repo
        "schedule_id": due.id,
        "schedule_night": gate.night,
        "budget": budget,
    }


async def _queued(conn: AsyncConnection, due, gate, run_id: int, model: str | None, budget: dict) -> None:
    await repository.add_history_run(conn, run_id, due.project_id)
    caps = f"cost cap {curator.money(budget['max_usd'])}, {budget['max_seconds'] // 60} minutes of agent time"
    text = (
        f"Queued by the night shift of project {due.project} (charter revision {due.revision}) as the reading of its "
        f"history for the night of {gate.night.isoformat()}, as {due.owner} on {gate.duty.where}: the model "
        f"{model or 'the runtime picks'} writes a facet of each session and run that has none, in batches; {caps}."
    )
    await write_event(conn, run_id, {"text": text, "schedule_id": due.id, "night": gate.night.isoformat(), **budget})
    await notify_queued(conn, run_id)
    night = gate.night.isoformat()
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=HISTORY,
        target=f"{due.project} run:{run_id} night={night}",
        project_id=due.project_id,
    )
    log.info("history run queued", extra={"project": due.project, "run_id": run_id, "night": night})


async def queue_history(conn: AsyncConnection, due, gate) -> int | None:
    """Queue the night's history run of schedule ``due`` (see the package's docstring); its id, or None. Called under
    the schedule's row lock, once the night's review run was queued."""
    cap = history.budget_usd(due.body)
    usd = min(gate.budget["max_usd"], cap)
    if usd < curator.MIN_RUN_USD or not await repository.reviewed(conn, due.project_id, gate.night):
        return None
    if await repository.had_history_run(conn, due.id, gate.night):
        return None
    runtime = (due.body.get("reviewer") or {}).get("runtime") or curator.DEFAULT_RUNTIME
    problems = gate.duty.unfit(due.project, "history", [], runtime)
    now = await repository.now(conn)
    if problems or not await _has_work(conn, await reader_of(conn, gate.access), now):
        if problems:
            log.info("no history run tonight", extra={"project": due.project, "why": "; ".join(problems)})
        return None
    reported = gate.duty.runtime(runtime)
    model = history.model_for(due.body, runtime, (reported or {}).get("models"))
    budget = {**gate.budget, "max_usd": round(usd, 4)}
    try:
        run_id = await insert_run(conn, _values(due, gate, runtime, model, budget))
    except IntegrityError as exc:  # another history run of the project is active
        if isinstance(exc.orig, psycopg.errors.UniqueViolation):
            return None
        raise
    await _queued(conn, due, gate, run_id, model, budget)
    return run_id


# The claim, and the run's side


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def history_claim(conn: AsyncConnection, run_id: int) -> dict:
    """What the claim of history run ``run_id`` adds for its worker: how it reads the batches, and a new key of the
    run (``history_key``), whose SHA-256 alone the hub keeps."""
    key = secrets.token_urlsafe(32)
    await repository.keep_key(conn, run_id, _key_digest(key))
    spec = {
        "batch_items": history.BATCH_ITEMS,
        "batch_chars": history.BATCH_CHARS,
        "batch_turns": history.BATCH_TURNS,
        "naming_share": history.NAMING_SHARE,
    }
    return {"history": spec, "history_key": key}


async def _held(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """The row of history run ``run_id``, which the worker of ``user`` holds, locked; 404 for any other run, 403
    without the run's own key."""
    worker_id = (await worker_of(conn, user))[0]
    run = await repository.run_locked(conn, run_id)
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise NotFound(NOT_HELD.format(id=run_id))
    if run.kind != "history":
        raise NotFound(f"run {run_id} is not a history run: only a history run reads the history in batches")
    row = await repository.history_run_locked(conn, run_id)
    if not key or row is None or row.key_digest is None or not hmac.compare_digest(_key_digest(key), row.key_digest):
        raise Forbidden(
            f"run {run_id} is a history run: its batches take the run's own key ({history.HISTORY_KEY_HEADER}), which "
            "its claim handed the daemon; the worker token alone reads and writes none"
        )
    return run


async def _owner_reader(conn: AsyncConnection, run) -> Reader:
    access = await owner_access(conn, run.dispatched_by, run.project_id)
    if access is None:
        raise Conflict("the member the history run was dispatched as no longer holds a grant on its project")
    return await reader_of(conn, access)


async def _to_name(conn: AsyncConnection, reader: Reader) -> tuple[list[dict], list[dict]]:
    """(the groups that have no name yet, the most sessions first, with some of their facets; the groups named)."""
    groups = await regroup(conn, reader)
    rows = await repository.cluster_rows(conn, reader.access.project_id)
    unnamed = sorted(
        (row for row in rows if row.name is None and row.key in groups), key=lambda row: (-row.sessions, row.key)
    )
    shown = [
        {
            "key": row.key,
            "label": history.label_of(row.key),
            "sessions": row.sessions,
            "samples": groups[row.key]["samples"],
        }
        for row in unnamed[: history.MAX_NAMED]
    ]
    named = [{"key": row.key, "name": row.name, "description": row.description} for row in rows if row.name]
    return shown, named


async def _batch(engine: AsyncEngine, store, rows: list) -> tuple[list[dict], list[tuple[int, str]]]:
    items, failed, chars = [], [], 0
    for row in rows:
        if len(items) >= history.BATCH_ITEMS:
            break
        try:
            item = await excerpt_of(engine, store, row)
        except facet_job.Unavailable as exc:
            failed.append((row.id, f"its content cannot be read now: {exc}"))
            continue
        if items and chars + len(item["excerpt"]) > history.BATCH_CHARS:
            break
        items.append(item)
        chars += len(item["excerpt"])
    return items, failed


async def next_batch(
    engine: AsyncEngine, store, user: Principal, run_id: int, key: str | None, clusters: bool
) -> HistoryBatch:
    """The next batch of history run ``run_id``, which the worker of ``user`` holds, with the run's ``key``: the
    waiting items with their excerpts, read from ``store`` (the blob store, or None) out of the transaction; an item
    whose content cannot be read now waits as a failed attempt does."""
    groups, named = [], []
    async with UnitOfWork(engine) as conn:
        run = await _held(conn, user, run_id, key)
        reader = await _owner_reader(conn, run)
        now = await repository.now(conn)
        rows = await waiting_items(conn, reader, now, CANDIDATES)
        waiting = (await coverage(conn, reader))["waiting"]
        if clusters:
            groups, named = await _to_name(conn, reader)
    items, failed = await _batch(engine, store, rows)
    if failed:
        async with UnitOfWork(engine) as conn:
            for facet_id, error in failed:
                await item_failed(conn, facet_id, error, now)
    log.info("history batch", extra={"run_id": run_id, "items": len(items), "failed": len(failed), "waiting": waiting})
    return HistoryBatch.model_validate({"items": items, "waiting": waiting, "groups": groups, "named": named})


async def _keep_facet(conn: AsyncConnection, run, run_id: int, entry: FacetReport, now: datetime) -> str:
    """What becomes of one reported facet: stored, stale or failed."""
    kind, ident = history.parse_item(entry.item)
    found = await repository.facet_of_item(conn, run.project_id, kind, ident)
    if found is None or found.fingerprint != entry.fingerprint:
        return "stale"
    if entry.error is not None:
        await item_failed(conn, found.id, entry.error, now)
        return "failed"
    try:
        facet = history.clean_facet(entry.facet)
    except ValueError as exc:
        await item_failed(conn, found.id, f"the facet does not hold: {exc}", now)
        return "failed"
    values = {"llm": facet, "llm_version": history.LLM_VERSION, "llm_fingerprint": found.fingerprint, "llm_at": now}
    values |= {"llm_run_id": run_id, "llm_attempts": 0, "llm_error": None, "llm_retry_at": None}
    await repository.set_facet(conn, found.id, values)
    return "stored"


async def _keep_name(conn: AsyncConnection, run, run_id: int, entry: NameReport) -> bool:
    keys = {row.key for row in await repository.cluster_rows(conn, run.project_id)}
    same = entry.same_as if entry.same_as in keys and entry.same_as != entry.key else None
    values = {"name": entry.name, "description": entry.description, "merged_into": same, "named_run_id": run_id}
    return await repository.name_group(conn, run.project_id, entry.key, values)


async def report(
    engine: AsyncEngine, user: Principal, run_id: int, key: str | None, body: HistoryReport
) -> HistoryReported:
    """Keep the facets the model wrote and the names it gave, of history run ``run_id``, which the worker of ``user``
    holds, with the run's ``key``."""
    counts = {"stored": 0, "stale": 0, "failed": 0, "named": 0}
    async with UnitOfWork(engine) as conn:
        run = await _held(conn, user, run_id, key)
        now = await repository.now(conn)
        for entry in body.facets:
            counts[await _keep_facet(conn, run, run_id, entry, now)] += 1
        for entry in body.names:
            counts["named"] += await _keep_name(conn, run, run_id, entry)
        await repository.add_counts(conn, run_id, counts)
        text = (
            f"The hub kept {counts['stored']} facet(s) of the model and {counts['named']} name(s); "
            f"{counts['failed']} item(s) got none and wait, {counts['stale']} changed since they were read."
        )
        await write_event(conn, run_id, {"text": text, **counts})
        waiting = (await coverage(conn, await _owner_reader(conn, run)))["waiting"]
    log.info("history reported", extra={"run_id": run_id, **counts})
    return HistoryReported(**counts, waiting=waiting)


async def history_ended(conn: AsyncConnection, found: RunStep, new: str, *, reason: str) -> None:
    """A history run that failed sends its owner the notice run_failed; what it wrote stays."""
    if new != "failed" or found.project_id is None:
        return
    from evo_agents.hub.server.notifications.service.notifier import NOTIFIER

    why = found.error or reason
    await NOTIFIER.notify(
        conn,
        user_id=found.dispatcher_id,
        kind="notice",
        notice_kind="run_failed",
        project_id=found.project_id,
        run_id=found.run_id,
        title=f"History run #{found.run_id} failed",
        body=why,
        details={"run_kind": "history", "error": why},
        link=NOTIFIER.run_link(found.project, found.run_id),
    )


# Reading


def _run_view(row) -> HistoryRunView:
    cost = curator.run_cost(row.usage) if isinstance(row.usage, dict) else None
    fields = {name: getattr(row, name) for name in HistoryRunView.model_fields if name != "cost_usd"}
    return HistoryRunView(**fields, cost_usd=cost)


async def status(conn: AsyncConnection, access: ProjectAccess) -> HistoryStatus:
    """Where the project's history run stands: its cap and model, the facets of the model, its latest runs."""
    charter = await repository.newest_charter_body(conn, access.project_id)
    llm = await coverage(conn, await reader_of(conn, access))
    ran = [_run_view(row) for row in await repository.history_runs(conn, access.project_id)]
    kept, hidden, _ = await clusters_of(conn, access.project_id)
    settings = (charter or {}).get("history") or {}
    return HistoryStatus(
        project=access.name,
        budget_usd=None if charter is None else history.budget_usd(charter),
        runtime=None if charter is None else (charter.get("reviewer") or {}).get("runtime") or curator.DEFAULT_RUNTIME,
        model=settings.get("model"),
        llm=LlmCoverage(**llm),
        runs=ran,
        clusters=len(kept),
        hidden=hidden,
    )


async def clusters(conn: AsyncConnection, access: ProjectAccess, limit: int) -> FacetClusterList:
    """The clusters of the project's facets of MIN_CLUSTER_SESSIONS sessions or more, as the hub last counted them."""
    kept, hidden, counted = await clusters_of(conn, access.project_id)
    return FacetClusterList(
        project=access.name,
        min_sessions=history.MIN_CLUSTER_SESSIONS,
        clusters=[FacetCluster(**cluster) for cluster in kept[:limit]],
        hidden=hidden,
        counted_at=counted,
    )
