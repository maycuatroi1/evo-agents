"""The night's history run on the hub: queueing it, its claim, the batches its worker reads and what it reports, the
clusters of the facets, and reading them. ``evo_agents.hub.history`` holds the model and ``evo_agents.hub.excerpts``
what the model reads of a transcript.

The items are the facets of session_facets (``evo_agents.hub.facet_job``) of a session whose own transcript the hub
holds, and of a run that ended (not a local run, whose session's transcript is the item, and not a history run), that
the schedule's owner may read: a session by its label through the project's hub sink, a run when it is of a plan the
owner may read or a review run. An item waits while it has no facet of the model of LLM_VERSION written from its
content as it is now (``llm_fingerprint`` is its facet's fingerprint), unless its last attempt failed and its
``llm_retry_at`` has not come.

The night shift (``curator._fire``) queues, right after the night's review run and before anything else, the night's
history run (``queue_history``): one a night, once the night's review run was queued, while an item waits or a group of
facets has no name yet, with a cap of its own (``history.budget_usd`` of the charter, under the next run's caps; none
at 0), on the reviewer's runtime with the charter's ``history.model``, else the cheapest model the worker on duty
reports for it (``history.model_for``). It is a run of the Curator on no plan and no repo, pinned to the worker on duty,
as the schedule's owner, audited as curator.history; its claim hands the daemon the run's own key, whose SHA-256 alone
the hub keeps in history_runs (``history_claim``).

POST /v1/worker/runs/{id}/history/next, with that key (``history.HISTORY_KEY_HEADER``), hands the worker the next
batch: up to ``history.BATCH_ITEMS`` waiting items, the latest first, each with its excerpt (``excerpts``: a session's
own transcript read from the blob store, a run's log or its events, cut around its errors and corrections) and the
fingerprint it was read at; an item whose content cannot be read now waits as a failed attempt does. With ``clusters``
the hub groups the facets again first (``regroup``) and adds the groups that have no name yet, with some of their
facets, and the groups named before. POST .../history/report takes the facets the model wrote and the names it gave:
a facet of content that changed since it was handed out is left (``stale``), an item the model gave none waits
``history.RETRY_HOURS`` (a week after ``history.MAX_ATTEMPTS`` attempts), and a group keeps the first name it got.

The clusters (``regroup``) are counted again each night when curator.collect counts the night's figures, from the
facets of the model the owner may read: per group, its sessions and runs, its members, its latest sessions; the
night's figures hold, under ``clusters``, those of ``history.MIN_CLUSTER_SESSIONS`` sessions or more, groups the model
joined counted as one, and under ``facets.llm`` how far the facets of the model are (``coverage``).

GET /v1/projects/{p}/curator/history (reader) says where the project's history run stands: its cap and model, how far
the facets of the model are, the latest history runs and what each wrote; GET .../curator/history/clusters (reader) the
clusters of the night's figures. Both answer only a caller whose sink clears the project's default label, as the
night's figures do.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated

import psycopg
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, exists, func, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, excerpts, facet_job, history, runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.run_state import RunStep, notify_queued, write_event
from evo_agents.hub.server.runs.models import NOT_HELD, RunId
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run, unfit
from evo_agents.hub.server.runs.service.views import visible_plans
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal

log = logging.getLogger(__name__)

HISTORY = "curator.history"  # the night shift queued the night's history run: "<project> run:<id> night=<date>"
CANDIDATES = history.BATCH_ITEMS * 4  # waiting items one batch looks at, the latest first
MAX_RUNS_LISTED = 10
MAX_REPORTED = 50
LONG_WAIT = timedelta(days=7)  # an item the model gave no facet MAX_ATTEMPTS times waits this long
SOURCES = {"claude-code": "Claude Code session", "codex": "Codex session"}

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}

HistoryKey = Annotated[
    str | None,
    Header(
        alias=history.HISTORY_KEY_HEADER,
        max_length=200,
        description="the history run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]
Item = Annotated[str, Field(pattern=history.ITEM.pattern, description="session:ID or run:ID")]
GroupKey = Annotated[str, Field(pattern=history.KEY.pattern, description="<task_type>/<failure mode or none>")]


# Models


class HistoryNext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clusters: bool = Field(False, description="group the facets again and add the groups that have no name yet")


class HistoryItem(BaseModel):
    item: str = Field(description="session:ID or run:ID")
    kind: str
    source: str = Field(description="what it is, in words")
    fingerprint: str = Field(description="the content it was read at; the report names it back")
    lines: int = Field(description="the entries of its transcript")
    anchors: int = Field(description="its errors and corrections")
    cut: bool = Field(description="whether the excerpt leaves part of it out")
    excerpt: str


class GroupSample(BaseModel):
    summary: str
    friction: list[str]


class HistoryGroup(BaseModel):
    key: str
    label: str = Field(description="its kind of task and failure mode, in words")
    sessions: int
    samples: list[GroupSample]


class NamedGroup(BaseModel):
    key: str
    name: str
    description: str | None


class HistoryBatch(BaseModel):
    items: list[HistoryItem] = Field(description="the next batch, the latest first; none once nothing waits")
    waiting: int = Field(description="the items without a facet of the model, this batch's included")
    groups: list[HistoryGroup] = Field(description="with clusters: the groups of facets that have no name yet")
    named: list[NamedGroup] = Field(description="with clusters: the groups named before")


class FacetReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: Item
    fingerprint: str = Field(min_length=1, max_length=200)
    facet: dict | None = Field(None, description="the facet the model wrote (history.clean_facet)")
    error: str | None = Field(None, min_length=1, max_length=history.MAX_ERROR_CHARS, description="why there is none")

    @model_validator(mode="after")
    def _one(self):
        if (self.facet is None) == (self.error is None):
            raise ValueError("a reported item has a facet or an error, not both")
        return self


class NameReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: GroupKey
    name: str = Field(min_length=1, max_length=history.MAX_NAME_CHARS, pattern=r"^[^\x00-\x1f\x7f]+$")
    description: str | None = Field(None, min_length=1, max_length=history.MAX_DESCRIPTION_CHARS)
    same_as: GroupKey | None = Field(None, description="the group it is the same as, which it counts in")


class HistoryReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facets: list[FacetReport] = Field(default_factory=list, max_length=MAX_REPORTED)
    names: list[NameReport] = Field(default_factory=list, max_length=history.MAX_NAMED)


class HistoryReported(BaseModel):
    stored: int = Field(description="facets kept")
    stale: int = Field(description="facets of content that changed since, or of an item gone, left")
    failed: int = Field(description="items the model gave none, which wait")
    named: int = Field(description="groups that got their name")
    waiting: int = Field(description="the items without a facet of the model now")


class LlmCoverage(BaseModel):
    version: int
    items: int = Field(description="sessions and runs the model reads, as the reader may read them")
    analyzed: int = Field(description="those with a facet of the model of their content now")
    waiting: int
    failed: int = Field(description="those whose last attempt failed")


class HistoryRunView(BaseModel):
    id: int
    night: date | None
    state: str
    model: str | None
    cost_usd: float | None
    analyzed: int
    failed: int
    stale: int
    named: int
    queued_at: datetime
    finished_at: datetime | None


class HistoryStatus(BaseModel):
    project: str
    budget_usd: float | None = Field(description="the night's cap of the history run; null without a charter")
    runtime: str | None = Field(description="the reviewer's runtime it runs on")
    model: str | None = Field(description="the charter's history.model; null: the cheapest the worker reports")
    llm: LlmCoverage
    runs: list[HistoryRunView] = Field(description="the latest history runs, newest first")
    clusters: int = Field(description="clusters in the night's figures")
    hidden: int = Field(description="smaller clusters, which stay out of them")


class FacetCluster(BaseModel):
    key: str
    name: str
    named: bool = Field(description="whether the model named it; else its name is its key in words")
    description: str | None
    task_type: str
    failure_mode: str
    sessions: int = Field(description="its sessions, a run counting as one")
    runs: int
    members: int
    representative: str | None = Field(description="its latest session, as evidence names it")
    examples: list[str]
    keys: list[str] = Field(description="the groups it holds: its own and those the model joined to it")


class FacetClusterList(BaseModel):
    project: str
    min_sessions: int
    clusters: list[FacetCluster]
    hidden: int = Field(description="clusters of fewer sessions, left out")
    counted_at: datetime | None


# Who reads


async def owner_access(conn: AsyncConnection, user_id: int, project_id: int) -> ProjectAccess | None:
    """The access of member ``user_id`` to project ``project_id``, as a request of theirs would get it; None without
    one."""
    u, p = tables.users, tables.projects
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    name = (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()
    member = Principal(user_id=user_id, login=login, admin=False, token_id=0, kind=MACHINE, token_hash="")
    try:
        return await project_access(conn, member, name)
    except HTTPException:
        return None


@dataclass(frozen=True)
class Reader:
    """Whose facets count: an access, the hub sink it reads through and the plans whose runs it may read."""

    access: ProjectAccess
    plans: frozenset

    @property
    def through(self) -> str | None:
        return self.access.rules.hub_sink.sink if self.access.rules.hub_sink else None

    def reads(self, row) -> bool:
        if row.kind == "session":
            return self.through is not None and self.access.visible(row.label, self.through)
        return row.run_kind == "review" or row.plan_id in self.plans


async def reader_of(conn: AsyncConnection, access: ProjectAccess) -> Reader:
    return Reader(access, frozenset(await visible_plans(conn, access, None)))


# The items


def _current(f):
    """A facet of the model of LLM_VERSION written from the content as it is now; never NULL."""
    fresh = f.c.llm_fingerprint.is_not_distinct_from(f.c.fingerprint)
    return and_(f.c.llm.is_not(None), f.c.llm_version == history.LLM_VERSION, fresh)


def _eligible(f, r):
    """A facet the model reads: of a session whose own transcript the hub holds, or of a run that is not local."""
    t = tables.session_transcripts
    live = exists().where(
        t.c.project_id == f.c.project_id,
        t.c.session_id == f.c.session_id,
        t.c.file == excerpts.MAIN,
        t.c.deleted_at.is_(None),
        t.c.parts > 0,
    )
    session = and_(f.c.kind == "session", f.c.source == "transcript", live)
    run = and_(f.c.kind == "run", r.c.kind.not_in(("local", "history", "probe")))
    return and_(f.c.body.is_not(None), or_(session, run))


def _facets(project_id: int, *where):
    f, r = tables.session_facets, tables.runs
    columns = (f.c.id, f.c.project_id, f.c.kind, f.c.session_id, f.c.run_id, f.c.label, f.c.user_id, f.c.at)
    return (
        select(*columns, f.c.fingerprint, f.c.runtime, f.c.llm_attempts, r.c.kind.label("run_kind"), r.c.plan_id)
        .add_columns(r.c.log_sha256)
        .add_columns(_current(f).label("current"))
        .select_from(f.outerjoin(r, r.c.id == f.c.run_id))
        .where(f.c.project_id == project_id, _eligible(f, r), *where)
    )


async def coverage(conn: AsyncConnection, reader: Reader) -> dict:
    """How far the facets of the model of the project are, as ``reader`` may read its sessions and runs."""
    rows = [row for row in await conn.execute(_facets(reader.access.project_id)) if reader.reads(row)]
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
    f = tables.session_facets
    due = or_(f.c.llm_retry_at.is_(None), f.c.llm_retry_at <= now)
    query = _facets(reader.access.project_id, ~_current(f), due).order_by(f.c.at.desc().nulls_last(), f.c.id.desc())
    found = []
    query = query.limit(limit * 20 + 200)  # an owner who may read few of them waits for the next batch
    for row in await conn.execute(query):
        if reader.reads(row):
            found.append(row)
            if len(found) >= limit:
                break
    return found


def _source(row) -> str:
    if row.kind == "run":
        return f"a run of kind {row.run_kind} on {row.runtime or 'a runtime'}"
    return SOURCES.get(row.runtime or "", "session")


async def _session_lines(engine, store, row) -> excerpts.Cutter:
    t, p = tables.session_transcripts, tables.transcript_parts
    query = (
        select(p.c.first_line, p.c.sha256.label("blob"), p.c.size)
        .join_from(t, p, p.c.transcript_id == t.c.id)
        .where(t.c.project_id == row.project_id, t.c.session_id == row.session_id, t.c.file == excerpts.MAIN)
        .where(t.c.deleted_at.is_(None))
        .order_by(p.c.seq)
    )
    async with engine.connect() as conn:
        parts = (await conn.execute(query)).all()
    if not parts:
        raise facet_job.Unavailable("the session's own transcript is no longer on the hub")
    return await asyncio.to_thread(lambda: excerpts.session_excerpt(facet_job.file_lines(store, parts, None)))


async def _run_lines(engine, store, row) -> excerpts.Cutter:
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


async def excerpt_of(engine, store, row) -> dict:
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
    f = tables.session_facets
    attempts = (await conn.execute(select(f.c.llm_attempts).where(f.c.id == facet_id))).scalar_one() + 1
    values = {
        "llm_attempts": attempts,
        "llm_error": error[: history.MAX_ERROR_CHARS],
        "llm_retry_at": _wait_of(attempts, now),
    }
    await conn.execute(update(f).values(**values).where(f.c.id == facet_id))


# The clusters


def _evidence(row) -> str:
    return f"run:{row.run_id}:1" if row.kind == "run" else f"session:{row.session_id}"


def _llm_facets(project_id: int):
    f, r = tables.session_facets, tables.runs
    columns = (f.c.kind, f.c.session_id, f.c.run_id, f.c.label, f.c.user_id, f.c.at, f.c.llm)
    return (
        select(*columns, r.c.kind.label("run_kind"), r.c.plan_id)
        .select_from(f.outerjoin(r, r.c.id == f.c.run_id))
        .where(f.c.project_id == project_id, _current(f))
        .order_by(f.c.at.desc().nulls_last(), f.c.id.desc())
    )


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
    project_id, c = reader.access.project_id, tables.facet_clusters
    groups = _groups(await conn.execute(_llm_facets(project_id)), reader)
    now = (await conn.execute(select(func.now()))).scalar_one()
    for key, group in sorted(groups.items()):
        values = {
            "sessions": group["sessions"],
            "runs": group["runs"],
            "member_ids": sorted(group["members"]),
            "examples": group["ex"],
            "counted_at": now,
        }
        statement = pg_insert(c).values(project_id=project_id, key=key, **values)
        changes = {name: statement.excluded[name] for name in values}
        await conn.execute(
            statement.on_conflict_do_update(
                index_elements=[c.c.project_id, c.c.key], set_={**changes, "updated_at": func.now()}
            )
        )
    gone = {"sessions": 0, "runs": 0, "member_ids": [], "examples": [], "counted_at": now, "updated_at": func.now()}
    await conn.execute(
        update(c).values(**gone).where(c.c.project_id == project_id, c.c.key.not_in(list(groups) or [""]))
    )
    return dict(groups)


async def cluster_rows(conn: AsyncConnection, project_id: int) -> list:
    c = tables.facet_clusters
    return (await conn.execute(select(c).where(c.c.project_id == project_id).order_by(c.c.key))).all()


async def clusters_of(conn: AsyncConnection, project_id: int) -> tuple[list[dict], int, datetime | None]:
    """(the clusters of MIN_CLUSTER_SESSIONS sessions or more as the hub last counted them, how many smaller ones,
    when it counted them)."""
    rows = await cluster_rows(conn, project_id)
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
    c = tables.facet_clusters
    unnamed = exists().where(c.c.project_id == reader.access.project_id, c.c.name.is_(None), c.c.sessions > 0)
    return bool((await conn.execute(select(unnamed))).scalar())


async def _reviewed(conn: AsyncConnection, project_id: int, night: date) -> bool:
    cf = tables.curator_figures
    query = select(cf.c.run_id).where(cf.c.project_id == project_id, cf.c.night == night)
    return (await conn.execute(query)).scalar_one_or_none() is not None


async def _had_one(conn: AsyncConnection, schedule_id: int, night: date) -> bool:
    r = tables.runs
    one = exists().where(r.c.schedule_id == schedule_id, r.c.schedule_night == night, r.c.kind == "history")
    return bool((await conn.execute(select(one))).scalar())


def _values(due, gate, runtime: str, model: str | None, budget: dict) -> dict:
    return {
        "kind": "history",
        "project_id": due.project_id,
        "plan_id": None,
        "title": f"History of the night of {gate.night.isoformat()}",
        "plan_revision": None,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        "pinned_worker_id": gate.worker.id,
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
    await conn.execute(pg_insert(tables.history_runs).values(run_id=run_id, project_id=due.project_id))
    caps = f"cost cap {curator.money(budget['max_usd'])}, {budget['max_seconds'] // 60} minutes of agent time"
    text = (
        f"Queued by the night shift of project {due.project} (charter revision {due.revision}) as the reading of its "
        f"history for the night of {gate.night.isoformat()}, as {due.owner} on worker {gate.worker.name}: the model "
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
    """Queue the night's history run of schedule ``due`` (see the module's docstring); its id, or None. Called under
    the schedule's row lock, once the night's review run was queued."""
    cap = history.budget_usd(due.body)
    usd = min(gate.budget["max_usd"], cap)
    if usd < curator.MIN_RUN_USD or not await _reviewed(conn, due.project_id, gate.night):
        return None
    if await _had_one(conn, due.id, gate.night):
        return None
    runtime = (due.body.get("reviewer") or {}).get("runtime") or curator.DEFAULT_RUNTIME
    problems = unfit(gate.worker, due.project, "history", [], runtime)
    now = (await conn.execute(select(func.now()))).scalar_one()
    if problems or not await _has_work(conn, await reader_of(conn, gate.access), now):
        if problems:
            log.info("no history run tonight", extra={"project": due.project, "why": "; ".join(problems)})
        return None
    reported = gate.worker.runtimes.get(runtime) if isinstance(gate.worker.runtimes, dict) else None
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
    r, h = tables.runs, tables.history_runs
    project_id = (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()
    statement = pg_insert(h).values(run_id=run_id, project_id=project_id, key_digest=_key_digest(key))
    await conn.execute(
        statement.on_conflict_do_update(
            index_elements=[h.c.run_id], set_={"key_digest": statement.excluded.key_digest, "updated_at": func.now()}
        )
    )
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
    r, h = tables.runs, tables.history_runs
    columns = (r.c.state, r.c.worker_id, r.c.kind, r.c.project_id, r.c.dispatched_by)
    run = (await conn.execute(select(*columns).where(r.c.id == run_id).with_for_update())).one_or_none()
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if run.kind != "history":
        raise HTTPException(404, f"run {run_id} is not a history run: only a history run reads the history in batches")
    row = (await conn.execute(select(h).where(h.c.run_id == run_id).with_for_update())).one_or_none()
    if not key or row is None or row.key_digest is None or not hmac.compare_digest(_key_digest(key), row.key_digest):
        raise HTTPException(
            403,
            f"run {run_id} is a history run: its batches take the run's own key ({history.HISTORY_KEY_HEADER}), which "
            "its claim handed the daemon; the worker token alone reads and writes none",
        )
    return run


async def _owner_reader(conn: AsyncConnection, run) -> Reader:
    access = await owner_access(conn, run.dispatched_by, run.project_id)
    if access is None:
        raise HTTPException(409, "the member the history run was dispatched as no longer holds a grant on its project")
    return await reader_of(conn, access)


async def _to_name(conn: AsyncConnection, reader: Reader) -> tuple[list[dict], list[dict]]:
    """(the groups that have no name yet, the most sessions first, with some of their facets; the groups named)."""
    groups = await regroup(conn, reader)
    rows = await cluster_rows(conn, reader.access.project_id)
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


async def _batch(engine, store, rows: list, now: datetime) -> tuple[list[dict], list[tuple[int, str]]]:
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


@worker_router.post("/runs/{run_id}/history/next", response_model=HistoryBatch, responses=REFUSALS)
async def next_batch(
    request: Request, run_id: RunId, body: HistoryNext, user: CurrentUser, history_key: HistoryKey = None
) -> HistoryBatch:
    """The next batch of the history run this worker holds, with the run's key (see the module's docstring)."""
    state = request.app.state
    groups, named = [], []
    async with state.engine.begin() as conn:
        run = await _held(conn, user, run_id, history_key)
        reader = await _owner_reader(conn, run)
        now = (await conn.execute(select(func.now()))).scalar_one()
        rows = await waiting_items(conn, reader, now, CANDIDATES)
        waiting = (await coverage(conn, reader))["waiting"]
        if body.clusters:
            groups, named = await _to_name(conn, reader)
    items, failed = await _batch(state.engine, getattr(state, "blobs", None), rows, now)
    if failed:
        async with state.engine.begin() as conn:
            for facet_id, error in failed:
                await item_failed(conn, facet_id, error, now)
    log.info("history batch", extra={"run_id": run_id, "items": len(items), "failed": len(failed), "waiting": waiting})
    return HistoryBatch(items=items, waiting=waiting, groups=groups, named=named)


async def _keep_facet(conn: AsyncConnection, run, run_id: int, entry: FacetReport, now: datetime) -> str:
    """What becomes of one reported facet: stored, stale or failed."""
    f = tables.session_facets
    kind, ident = history.parse_item(entry.item)
    key = f.c.run_id == ident if kind == "run" else f.c.session_id == ident
    found = (
        await conn.execute(
            select(f.c.id, f.c.fingerprint)
            .where(f.c.project_id == run.project_id, f.c.kind == kind, key)
            .with_for_update()
        )
    ).one_or_none()
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
    await conn.execute(update(f).values(**values).where(f.c.id == found.id))
    return "stored"


async def _keep_name(conn: AsyncConnection, run, run_id: int, entry: NameReport) -> bool:
    c = tables.facet_clusters
    keys = set((await conn.execute(select(c.c.key).where(c.c.project_id == run.project_id))).scalars())
    same = entry.same_as if entry.same_as in keys and entry.same_as != entry.key else None
    values = {"name": entry.name, "description": entry.description, "merged_into": same}
    values |= {"named_run_id": run_id, "named_at": func.now(), "updated_at": func.now()}
    named = await conn.execute(
        update(c)
        .values(**values)
        .where(c.c.project_id == run.project_id, c.c.key == entry.key, c.c.name.is_(None))
        .returning(c.c.id)
    )
    return named.first() is not None


@worker_router.post("/runs/{run_id}/history/report", response_model=HistoryReported, responses=REFUSALS)
async def report_history(
    request: Request, run_id: RunId, body: HistoryReport, user: CurrentUser, history_key: HistoryKey = None
) -> HistoryReported:
    """The facets the model wrote and the names it gave, of the history run this worker holds, with the run's key."""
    counts = {"stored": 0, "stale": 0, "failed": 0, "named": 0}
    async with request.app.state.engine.begin() as conn:
        run = await _held(conn, user, run_id, history_key)
        now = (await conn.execute(select(func.now()))).scalar_one()
        for entry in body.facets:
            counts[await _keep_facet(conn, run, run_id, entry, now)] += 1
        for entry in body.names:
            counts["named"] += await _keep_name(conn, run, run_id, entry)
        h = tables.history_runs
        sums = {"analyzed": h.c.analyzed + counts["stored"], "failed": h.c.failed + counts["failed"]}
        sums |= {"stale": h.c.stale + counts["stale"], "named": h.c.named + counts["named"]}
        await conn.execute(update(h).values(**sums, updated_at=func.now()).where(h.c.run_id == run_id))
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
    from evo_agents.hub.server.notifications import notify, run_link

    why = found.error or reason
    await notify(
        conn,
        user_id=found.dispatcher_id,
        kind="notice",
        notice_kind="run_failed",
        project_id=found.project_id,
        run_id=found.run_id,
        title=f"History run #{found.run_id} failed",
        body=why,
        details={"run_kind": "history", "error": why},
        link=run_link(found.project, found.run_id),
    )


# Reading


async def reader_access(conn: AsyncConnection, user: Principal, project: str, sink: str | None) -> ProjectAccess:
    """The caller's access to ``project`` when it reads the Curator's history of it: a grant, and the project's default
    label visible through ``sink`` (the project's hub sink when None); 404 otherwise. The routes and the MCP tool
    history_clusters read the clusters through it."""
    access = await project_access(conn, user, project)
    need_reader(access)
    if not access.visible(access.rules.default_label, plan_routes._sink(access, sink)):
        raise HTTPException(404, f"project {project} has no history of the Curator you may read through this sink")
    return access


def _history_runs(project_id: int):
    r, h = tables.runs, tables.history_runs
    columns = (r.c.id, r.c.schedule_night.label("night"), r.c.state, r.c.model, r.c.usage, r.c.queued_at)
    return (
        select(*columns, r.c.finished_at, h.c.analyzed, h.c.failed, h.c.stale, h.c.named)
        .join_from(h, r, r.c.id == h.c.run_id)
        .where(h.c.project_id == project_id)
        .order_by(r.c.id.desc())
        .limit(MAX_RUNS_LISTED)
    )


def _run_view(row) -> HistoryRunView:
    cost = curator.run_cost(row.usage) if isinstance(row.usage, dict) else None
    fields = {name: getattr(row, name) for name in HistoryRunView.model_fields if name != "cost_usd"}
    return HistoryRunView(**fields, cost_usd=cost)


@router.get("/{project}/curator/history", response_model=HistoryStatus, responses=REFUSALS)
async def history_status(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> HistoryStatus:
    """Where the project's history run stands: its cap and model, the facets of the model, its latest runs."""
    async with request.app.state.engine.begin() as conn:
        access = await reader_access(conn, user, project, sink)
        ch = tables.charters
        charter = (
            await conn.execute(
                select(ch.c.body).where(ch.c.project_id == access.project_id).order_by(ch.c.revision.desc()).limit(1)
            )
        ).scalar_one_or_none()
        llm = await coverage(conn, await reader_of(conn, access))
        ran = [_run_view(row) for row in await conn.execute(_history_runs(access.project_id))]
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


@router.get("/{project}/curator/history/clusters", response_model=FacetClusterList, responses=REFUSALS)
async def list_clusters(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> FacetClusterList:
    """The clusters of the project's facets of MIN_CLUSTER_SESSIONS sessions or more, as the hub last counted them."""
    async with request.app.state.engine.begin() as conn:
        access = await reader_access(conn, user, project, sink)
        kept, hidden, counted = await clusters_of(conn, access.project_id)
    return FacetClusterList(
        project=access.name,
        min_sessions=history.MIN_CLUSTER_SESSIONS,
        clusters=[FacetCluster(**cluster) for cluster in kept[:limit]],
        hidden=hidden,
        counted_at=counted,
    )
