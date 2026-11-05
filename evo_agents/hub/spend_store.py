"""The hub's ledger of what each run spent (``run_spend``), as ``evo_agents.hub.run_spend`` reads its usage reports.

``record`` takes a run's ``usage_update`` events in (with the init messages that name Claude Code's model), or its
``usage`` when it has none kept, from where the latest earlier run of each of its sessions left that session
(``bases``), and replaces the run's rows in the caller's transaction, stamping ``runs.spend_at``. ``store_events``
records a run whose batch of events holds a usage report, so the figures follow a run that runs, waits or is parked;
a state report that carries the run's usage records it too. The job hub.record_spend (``record_pending``) takes in the
runs no record reached yet, oldest first, so a run's earlier runs of its session are in before it.

``spend_of`` reads the rows of some runs back as ``run_spend.summary`` adds them up, for Run's ``spend``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import run_spend, tables

log = logging.getLogger(__name__)

PENDING_BATCH = 200  # runs a transaction of record_pending takes in
PENDING_SECONDS = 50.0  # how long one job of record_pending goes on, so a tick of the minute is not overrun
TOKEN_COLUMNS = {key: f"{key}_tokens" for key in run_spend.FIGURES}


def _events(run_id: int):
    """The events of run ``run_id`` that ``run_spend.reports_of`` reads: its usage reports and its init messages."""
    e = tables.run_events
    init = and_(e.c.kind.in_(run_spend.SYSTEM_KINDS), e.c.body["raw"]["subtype"].astext == "init")
    return (
        select(e.c.kind, e.c.body).where(e.c.run_id == run_id, or_(e.c.kind == "usage_update", init)).order_by(e.c.seq)
    )


async def bases(conn: AsyncConnection, run_id: int, sessions: Iterable[str]) -> dict[str, dict]:
    """Where the latest run before ``run_id`` of each of ``sessions`` left it: the gauge and each model's ends, as
    ``run_spend.ledger`` takes them."""
    named = sorted({session for session in sessions if session})
    if not named:
        return {}
    s = tables.run_spend
    latest = (
        select(s.c.session, func.max(s.c.run_id).label("run_id"))
        .where(s.c.session.in_(named), s.c.session != "", s.c.run_id < run_id)
        .group_by(s.c.session)
        .subquery()
    )
    query = select(s.c.session, s.c.model, s.c.ends).join(
        latest, and_(s.c.session == latest.c.session, s.c.run_id == latest.c.run_id)
    )
    found: dict[str, dict] = {}
    for row in (await conn.execute(query)).all():
        entry = found.setdefault(row.session, {"gauge": None, "models": {}})
        ends = row.ends if isinstance(row.ends, dict) else {}
        entry["models"][row.model] = ends
        if ends.get("gauge") is not None:
            entry["gauge"] = ends["gauge"]
    return found


def _row(run_id: int, spent: run_spend.Spend) -> dict:
    tokens = {column: int(spent.tokens.get(key, 0)) for key, column in TOKEN_COLUMNS.items()}
    return {
        "run_id": run_id,
        "session": spent.session,
        "model": spent.model,
        **tokens,
        "reported_usd": spent.reported_usd,
        "estimated_usd": spent.estimated_usd,
        "reports": spent.reports,
        "ends": spent.ends,
    }


async def record(conn: AsyncConnection, run_id: int) -> list[run_spend.Spend]:
    """Write what run ``run_id`` spent (see the module), in the caller's transaction; the rows written."""
    r, s = tables.runs, tables.run_spend
    query = select(r.c.runtime, r.c.session_id, r.c.model_used, r.c.model, r.c.usage).where(r.c.id == run_id)
    run = (await conn.execute(query)).one_or_none()
    if run is None:
        return []
    session = run.session_id or run_spend.UNKNOWN
    model = run_spend.model_key(run.model_used or run.model)
    events = [(row.kind, row.body) for row in (await conn.execute(_events(run_id))).all()]
    reports = run_spend.reports_of(run.runtime, events, session, model)
    if not reports:
        reported = run_spend.usage_report(run.runtime, run.usage, session, model)
        reports = [reported] if reported is not None else []
    spent = run_spend.ledger(reports, await bases(conn, run_id, (report.session for report in reports)))
    await conn.execute(delete(s).where(s.c.run_id == run_id))
    if spent:
        await conn.execute(insert(s), [_row(run_id, item) for item in spent])
    await conn.execute(update(r).values(spend_at=func.now()).where(r.c.id == run_id))
    return spent


async def record_safely(conn: AsyncConnection, run_id: int) -> None:
    """``record`` in a savepoint: a run whose reports cannot be read keeps its rows, and the caller's events, report
    or move go through all the same (the job takes the run in again once its stamp is cleared)."""
    try:
        async with conn.begin_nested():
            await record(conn, run_id)
    except Exception:
        log.exception("run spend not recorded", extra={"run_id": run_id})


async def record_pending(engine: AsyncEngine) -> dict:
    """Record the runs no record reached yet (``spend_at`` NULL), oldest first, PENDING_BATCH a transaction, for at
    most PENDING_SECONDS; how many it took in and how many are left."""
    r = tables.runs
    started, done = time.monotonic(), 0
    pending = select(r.c.id).where(r.c.spend_at.is_(None)).order_by(r.c.id).limit(PENDING_BATCH)
    while time.monotonic() - started < PENDING_SECONDS:
        async with engine.begin() as conn:
            ids = list((await conn.execute(pending)).scalars())
            for run_id in ids:
                try:
                    async with conn.begin_nested():
                        await record(conn, run_id)
                except Exception:  # a run its reports cannot be read of is stamped, so the next ones go on
                    log.exception("run spend not recorded", extra={"run_id": run_id})
                    await conn.execute(update(r).values(spend_at=func.now()).where(r.c.id == run_id))
        done += len(ids)
        if len(ids) < PENDING_BATCH:
            break
    async with engine.connect() as conn:
        left = (await conn.execute(select(func.count()).where(r.c.spend_at.is_(None)))).scalar_one()
    if done:
        log.info("run spend recorded", extra={"runs": done, "left": left})
    return {"recorded": done, "left": left}


def _model_spend(rows) -> list[run_spend.Spend]:
    spent = []
    for row in rows:
        tokens = {key: getattr(row, column) for key, column in TOKEN_COLUMNS.items() if getattr(row, column)}
        spent.append(
            run_spend.Spend(row.session, row.model, tokens, row.reported_usd, row.estimated_usd, row.reports, {})
        )
    return spent


async def spend_of(conn: AsyncConnection, run_ids: Iterable[int]) -> dict[int, dict]:
    """The spend of each of ``run_ids`` that has rows, as ``run_spend.summary`` adds them up, with its sessions."""
    ids = sorted(set(run_ids))
    if not ids:
        return {}
    s = tables.run_spend
    rows = (await conn.execute(select(s).where(s.c.run_id.in_(ids)).order_by(s.c.run_id))).all()
    by_run: dict[int, list] = {}
    for row in rows:
        by_run.setdefault(row.run_id, []).append(row)
    found = {}
    for run_id, of_run in by_run.items():
        summary = run_spend.summary(_model_spend(of_run))
        summary["sessions"] = len({row.session for row in of_run})
        summary["updated_at"] = max(row.updated_at for row in of_run)
        found[run_id] = summary
    return found
