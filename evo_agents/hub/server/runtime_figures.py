"""The night's ``runtime`` and ``harness`` (``evo_agents.hub.runtime_metrics``), which ``evo_agents.hub.server.collect``
adds to the figures of a span, counted without any model from what the reader may read:

- the sessions the figures count (``facet_figures.sessions_of``), each with the parts of its facet the runtime and the
  harness read (``facet_figures.RUNTIME_PARTS``), its evidence ``session:ID`` (``run:ID:1`` for a local run);
- the runs that ended in the span, as the figures count them (of a plan the reader may read or a review run, never a
  local run, which a member's session is): their own usage (tokens, cost, peak context, the first request's cache) and
  the parts of their facet when the job hub.analyze_facets made one (the context, the compactions, the time of tools);
- the pull requests that merged in the span: the repos of the plans the reader may read whose status is ``merged`` and
  whose ``merged_at`` falls in the span, and the Curator's changes that merged in it, each (plan, repo) once, with the
  cost of the runs of their plans that ended before the span's end;
- the names of the project's repos, which the always-loaded files are grouped by;
- the spool the digests of the span's sessions say (``runtime_metrics.SPOOL_KEYS``), a digest the reader may read.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import facets, runtime_metrics, tables
from evo_agents.hub.runtime_metrics import Item
from evo_agents.hub.server.facet_figures import Session
from evo_agents.hub.server.projects import ProjectAccess

RUN_PARTS = ("context", "compactions", "compaction", "tools", "hooks")  # of a run's facet
MAX_PR_CHARS = 500


def _session_item(session: Session) -> Item:
    evidence = f"session:{session.session}" if session.run is None else f"run:{session.run}:1"
    name = session.session or session.key
    return Item(evidence, name, session.lists, messages=session.messages, model=session.model)


async def _runs(conn: AsyncConnection, ended: tuple) -> list[Item]:
    r, f = tables.runs, tables.session_facets
    parts = [f.c.body[name].label(name) for name in RUN_PARTS]
    rows = await conn.execute(
        select(r.c.id, r.c.kind, r.c.plan_id, r.c.usage, *parts)
        .select_from(r.outerjoin(f, (f.c.run_id == r.c.id) & (f.c.kind == "run")))
        .where(*ended)
        .order_by(r.c.id)
    )
    return [
        Item(
            f"run:{row.id}:1",
            f"run:{row.id}",
            {name: getattr(row, name) for name in RUN_PARTS},
            kind=row.kind,
            plan_id=row.plan_id,
            usage=row.usage if isinstance(row.usage, dict) else None,
        )
        for row in rows
    ]


def _in_span(value, since: datetime, until: datetime) -> datetime | None:
    found = value if isinstance(value, datetime) else facets.timestamp(value)
    return found if found is not None and since <= found < until else None


def _pr(value) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return str(value)[:MAX_PR_CHARS] if value else None


async def _merged(conn: AsyncConnection, project_id: int, plans: list[str], since: datetime, until: datetime) -> dict:
    p, c, r = tables.plans, tables.curator_changes, tables.runs
    found: dict[tuple, dict] = {}
    rows = await conn.execute(
        select(p.c.plan_id, p.c.body["repos"].label("repos"))
        .where(p.c.project_id == project_id, p.c.plan_id.in_(plans))
        .order_by(p.c.plan_id)
    )
    for row in rows:
        for entry in row.repos if isinstance(row.repos, list) else []:
            if not isinstance(entry, dict) or entry.get("status") != "merged" or not entry.get("repo"):
                continue
            at = _in_span(entry.get("merged_at"), since, until)
            if at is not None:
                key = (row.plan_id, str(entry["repo"]))
                found[key] = {"plan_id": row.plan_id, "repo": key[1], "pr": _pr(entry.get("pr")), "merged_at": at}
    changes = await conn.execute(
        select(c.c.plan_id, c.c.repo, c.c.pr_url, c.c.pr_number, c.c.merged_at)
        .where(c.c.project_id == project_id, c.c.plan_id.in_(plans), c.c.repo.is_not(None))
        .where(c.c.merged_at >= since, c.c.merged_at < until)
        .order_by(c.c.id)
    )
    for row in changes:
        found.setdefault(
            (row.plan_id, row.repo),
            {
                "plan_id": row.plan_id,
                "repo": row.repo,
                "pr": _pr(row.pr_url or row.pr_number),
                "merged_at": row.merged_at,
            },
        )
    merged = [{**entry, "merged_at": entry["merged_at"].isoformat()} for entry in found.values()]
    costs: dict[str, dict] = defaultdict(lambda: {"runs": 0, "priced": 0, "cost_usd": 0.0})
    named = sorted({entry["plan_id"] for entry in merged})
    if named:
        ran = await conn.execute(
            select(r.c.plan_id, r.c.usage)
            .where(r.c.project_id == project_id, r.c.plan_id.in_(named), r.c.kind != "local")
            .where(r.c.finished_at.is_not(None), r.c.finished_at < until)
            .order_by(r.c.id)
        )
        for row in ran:
            cost, entry = runtime_metrics.run_cost(row.usage), costs[row.plan_id]
            entry["runs"] += 1
            entry["priced"] += 1 if cost is not None else 0
            entry["cost_usd"] += cost or 0.0
    return runtime_metrics.merged_prs(merged, costs)


async def _repos(conn: AsyncConnection, project_id: int) -> list[str]:
    pr = tables.project_repos
    return list(
        (await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id).order_by(pr.c.name))).scalars()
    )


async def _spool(conn: AsyncConnection, access: ProjectAccess, since: datetime, until: datetime) -> dict | None:
    """The sums of the digests' SPOOL_KEYS of the span the reader may read, with how many said them; None for none."""
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    if through is None:
        return None
    d = tables.session_digests
    keys = runtime_metrics.SPOOL_KEYS
    rows = await conn.execute(
        select(d.c.label, *[d.c.body[key].label(key) for key in keys])
        .where(d.c.project_id == access.project_id, d.c.updated_at >= since, d.c.updated_at < until)
        .order_by(d.c.session_id)
    )
    sums, said = dict.fromkeys(keys, 0), 0
    for row in rows:
        values = [facets.count(getattr(row, key)) for key in keys]
        if all(value is None for value in values) or not access.visible(row.label, through):
            continue
        said += 1
        for key, value in zip(keys, values, strict=True):
            sums[key] += value or 0
    return {"digests": said, **sums} if said else None


async def figures(
    conn: AsyncConnection,
    access: ProjectAccess,
    ended: tuple,
    plans: list[str],
    since: datetime,
    until: datetime,
    sessions: list[Session],
) -> tuple[dict, dict]:
    """(the night's ``runtime``, its ``harness``) over [since, until): ``ended`` are the conditions of the runs the
    figures count (``collect``), ``sessions`` the sessions they count."""
    items = [_session_item(session) for session in sessions]
    runs = await _runs(conn, ended)
    merged = await _merged(conn, access.project_id, plans, since, until)
    found = runtime_metrics.runtime(items, runs, merged)
    repos = await _repos(conn, access.project_id)
    return found, runtime_metrics.harness(items, repos, await _spool(conn, access, since, until))
