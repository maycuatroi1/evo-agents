"""The queries of the draft pull requests of run branches (``service.draft_pulls``): the copies the runs keep
(runs.pulls), and the runs of a draft's trace."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.merge.models.draft_pulls import KINDS, MAX_PULLS, SETTLE_MINUTES, WATCH_DAYS, Draft


async def keep(conn: AsyncConnection, run_id: int, entry: dict) -> None:
    """Keep ``entry`` in runs.pulls of run ``run_id``, in place of the one of its repo."""
    r = tables.runs
    found = (await conn.execute(select(r.c.pulls).where(r.c.id == run_id).with_for_update())).scalar_one() or []
    found = [item for item in found if isinstance(item, dict) and item.get("repo") != entry["repo"]] + [entry]
    await conn.execute(update(r).where(r.c.id == run_id).values(pulls=found[-MAX_PULLS:]))


async def mark(conn: AsyncConnection, project_id: int, repo: str, number: int, **noted) -> None:
    """Note ``noted`` (a state, when it was read, ...) on every copy of pull request ``number`` of ``repo`` that the
    runs of project ``project_id`` keep."""
    r = tables.runs
    held = r.c.pulls.contains([{"repo": repo, "number": number}])
    rows = (await conn.execute(select(r.c.id, r.c.pulls).where(r.c.project_id == project_id, held))).all()
    for row in rows:
        items = [
            {**item, **noted}
            if isinstance(item, dict) and (item.get("repo"), item.get("number")) == (repo, number)
            else item
            for item in row.pulls or []
        ]
        await conn.execute(update(r).where(r.c.id == row.id).values(pulls=items))


async def open_drafts(conn: AsyncConnection) -> list:
    """The runs of KINDS queued in WATCH_DAYS that keep a draft open, newest first: (id, project_id, branch, pulls)."""
    r = tables.runs
    query = select(r.c.id, r.c.project_id, r.c.branch, r.c.pulls).where(
        r.c.kind.in_(KINDS),
        r.c.pulls.contains([{"state": "open"}]),
        r.c.queued_at > func.now() - timedelta(days=WATCH_DAYS),
    )
    return list(await conn.execute(query.order_by(r.c.id.desc())))


async def trace_runs(conn: AsyncConnection, draft: Draft):
    """The ids of the runs of the trace of the draft's run, or of its session (its project and branch) for a run of
    no trace, as a subquery."""
    s, r = tables.system_spans, tables.runs
    trace = await conn.scalar(select(s.c.trace_id).where(s.c.run_id == draft.run_id).limit(1))
    if trace is not None:
        return select(s.c.run_id).where(s.c.trace_id == trace, s.c.run_id.is_not(None))
    return select(r.c.id).where(r.c.project_id == draft.project_id, r.c.branch == draft.branch)


async def settled(conn: AsyncConnection, draft: Draft) -> bool:
    """Whether the trace of the draft's run ended: none of its runs is still to end, and the last ended SETTLE_MINUTES
    ago."""
    r = tables.runs
    ids = await trace_runs(conn, draft)
    going = await conn.scalar(select(func.count()).where(r.c.id.in_(ids), r.c.state.not_in(runs.TERMINAL_STATES)))
    if going:
        return False
    since = func.now() - timedelta(minutes=SETTLE_MINUTES)
    last = select(func.max(func.coalesce(r.c.finished_at, r.c.queued_at))).where(r.c.id.in_(ids)).scalar_subquery()
    return bool(await conn.scalar(select(last < since)))


async def trace_texts(conn: AsyncConnection, draft: Draft) -> list[str]:
    """The messages, first first, and the evidence of the runs of the trace of the draft's run."""
    m, r = tables.chat_messages, tables.runs
    ids = await trace_runs(conn, draft)
    texts = list((await conn.execute(select(m.c.body).where(m.c.run_id.in_(ids)).order_by(m.c.id))).scalars())
    texts += [text for text in (await conn.execute(select(r.c.evidence).where(r.c.id.in_(ids)))).scalars() if text]
    return texts
