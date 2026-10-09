"""The Curator's morning brief: what the night shift of a project did, sent to the owner of its schedule at the
charter's ``brief_at``, in the charter's time zone.

``send_briefs`` is the job ``curator.brief``, every minute in the hub's worker. For each schedule of kind night_shift
whose charter's brief is due (``curator.brief_due``: from brief_at until BRIEF_GRACE later, on a local day the project
has no brief yet), in a transaction of its own, it counts the night the brief reports on (the night of
``curator.night_of``, the one in progress when brief_at falls inside the window) and sends a notification of kind notice
(``curator_brief``) to the schedule's owner, who gets it on the web and on every channel they turned on, Telegram
included, through the outbox (``notifications.notify``). The brief holds:

- the runs the night shift queued that night, by how they ended, and what they cost against the night's budget;
- the night's review run, with the findings and proposals it wrote;
- the merges into a default branch the night's runs reported (merge_default_branch notices, the hub's merges of the
  Curator's pull requests among them), and what waits for the owner to merge or approve it: the project's runs in
  review, and the Curator's pull requests (or merge requests) still open, each named by its Builder's run and its link;
- the decisions of the project that wait for the owner's answer, and the proposals that wait for an admin's, with how
  many of them are in the owner's Inbox;
- the worker on duty: its last heartbeat, and whether it is online, so a machine that stopped shows up in the morning.

curator_briefs keeps one row a project and local day, with what the brief said and its notification; a second pass of
the job, or a second hub worker, finds the row and sends nothing. ``curator.last_brief`` names the newest for GET
/v1/projects/{p}/curator (``last_brief``). A schedule whose owner lost every grant on the project gets no brief.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, runs, tables
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.curator import _due, _owner_access, curator_state, night_figures
from evo_agents.hub.server.notifications import notify, one_line
from evo_agents.hub.server.projects import ProjectAccess

log = logging.getLogger(__name__)

MAX_LISTED = 10  # runs, merges, runs in review, decisions and proposals a brief names; it counts all of them
QUESTION_CHARS = 160  # of a decision's question in the brief
MAX_BRIEF_BYTES = 64 * 1024  # curator_briefs.body as JSON, as schema 0015 bounds it


def curator_link(project: str) -> str:
    """The Curator's page of the project, where a brief links."""
    return f"/p/{project}/curator"


async def _runs_of_night(conn: AsyncConnection, schedule_id: int, night) -> list[dict]:
    r = tables.runs
    query = (
        select(r.c.id, r.c.kind, r.c.plan_id, r.c.title, r.c.state, r.c.usage)
        .where(r.c.schedule_id == schedule_id, r.c.schedule_night == night)
        .order_by(r.c.id)
    )
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "plan_id": row.plan_id,
            "title": row.title,
            "state": row.state,
            "cost_usd": round(curator.run_cost(row.usage), 6),
        }
        for row in (await conn.execute(query)).all()
    ]


async def _review(conn: AsyncConnection, run_ids: list[int]) -> dict | None:
    r, f, p = tables.runs, tables.findings, tables.proposals
    if not run_ids:
        return None
    query = (
        select(
            r.c.id,
            r.c.state,
            select(func.count()).where(f.c.run_id == r.c.id).scalar_subquery().label("findings"),
            select(func.count()).where(p.c.run_id == r.c.id).scalar_subquery().label("proposals"),
        )
        .where(r.c.id.in_(run_ids), r.c.kind == "review")
        .order_by(r.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else dict(row._mapping)


async def _merged(conn: AsyncConnection, run_ids: list[int]) -> list[dict]:
    """The merges into a default branch the night's runs reported, oldest first."""
    n = tables.notifications
    if not run_ids:
        return []
    query = (
        select(n.c.run_id, n.c.details)
        .where(n.c.run_id.in_(run_ids), n.c.notice_kind == "merge_default_branch")
        .order_by(n.c.id)
    )
    seen, merged = set(), []
    for row in (await conn.execute(query)).all():
        details = row.details if isinstance(row.details, dict) else {}
        key = (row.run_id, details.get("repo"), details.get("branch"))
        if key in seen:  # the owner and others each got the notice
            continue
        seen.add(key)
        commits = details.get("commits") if isinstance(details.get("commits"), list) else []
        merged.append(
            {
                "run_id": row.run_id,
                "repo": details.get("repo"),
                "branch": details.get("branch"),
                "commits": len(commits),
            }
        )
    return merged


WAITING_CHANGES = ("judge_pending", "judging", "judged", "open")  # a Curator change whose pull request is still open


async def _awaiting(conn: AsyncConnection, project_id: int) -> tuple[int, list[dict]]:
    """What waits for the owner: the project's runs in review, to approve, and the Curator's pull requests (merge
    requests on GitLab) still open, to merge; how many, and the first few, runs first. A pull request is named by its
    Builder's run, with ``pull_request`` its link (null for a merge request, whose link the hub does not know)."""
    r, c = tables.runs, tables.curator_changes
    where = (r.c.project_id == project_id, r.c.state == "review")
    total = (await conn.execute(select(func.count()).select_from(r).where(*where))).scalar_one()
    query = select(r.c.id, r.c.plan_id, r.c.title).where(*where).order_by(r.c.id).limit(MAX_LISTED)
    listed = [
        {"run_id": row.id, "plan_id": row.plan_id, "title": row.title} for row in (await conn.execute(query)).all()
    ]
    open_prs = (c.c.project_id == project_id, c.c.state.in_(WAITING_CHANGES))
    total += (await conn.execute(select(func.count()).select_from(c).where(*open_prs))).scalar_one()
    changes = select(
        c.c.builder_run_id, c.c.plan_id, c.c.repo, c.c.branch, c.c.pr_number, c.c.pr_url, c.c.tier, c.c.state
    )
    for row in (await conn.execute(changes.where(*open_prs).order_by(c.c.id).limit(MAX_LISTED))).all():
        what = f"pull request #{row.pr_number}" if row.pr_number else f"merge request of {row.branch}"
        listed.append(
            {
                "run_id": row.builder_run_id,
                "plan_id": row.plan_id,
                "title": f"{what} of {row.repo} (tier {row.tier}, {row.state.replace('_', ' ')})",
                "pull_request": row.pr_url,
            }
        )
    return total, listed[:MAX_LISTED]


async def _decisions(conn: AsyncConnection, project_id: int, owner_id: int) -> tuple[int, list[dict]]:
    """The project's open decisions of runs ``owner_id`` dispatched, the ones waiting for their answer."""
    d, r = tables.decisions, tables.runs
    where = (d.c.project_id == project_id, d.c.state == "open", r.c.dispatched_by == owner_id)
    joined = d.join(r, r.c.id == d.c.run_id)
    total = (await conn.execute(select(func.count()).select_from(joined).where(*where))).scalar_one()
    query = (
        select(d.c.id, d.c.category, d.c.question, d.c.run_id)
        .select_from(joined)
        .where(*where)
        .order_by(d.c.id)
        .limit(MAX_LISTED)
    )
    listed = [
        {
            "id": row.id,
            "category": row.category,
            "question": one_line(row.question, QUESTION_CHARS),
            "run_id": row.run_id,
        }
        for row in (await conn.execute(query)).all()
    ]
    return total, listed


async def _proposals(conn: AsyncConnection, access: ProjectAccess) -> tuple[int, int, list[dict]]:
    """The project's open proposals the owner may read: how many, how many are in their Inbox, and those with the most
    evidence."""
    p = tables.proposals
    query = (
        select(p.c.id, p.c.tier, p.c.title, p.c.evidence_count, p.c.inbox_at, p.c.label)
        .where(p.c.project_id == access.project_id, p.c.state == "open")
        .order_by(p.c.evidence_count.desc(), p.c.id)
    )
    through = plan_routes._sink(access, None)
    readable = [row for row in (await conn.execute(query)).all() if access.visible(row.label, through)]
    listed = [
        {"id": row.id, "tier": row.tier, "title": row.title, "evidence": row.evidence_count}
        for row in readable[:MAX_LISTED]
    ]
    return len(readable), sum(1 for row in readable if row.inbox_at is not None), listed


async def _worker(conn: AsyncConnection, worker_id: int, zone: str) -> dict:
    """The worker on duty: its last heartbeat, also in the charter's time zone ``zone``, and how it stands."""
    w = tables.workers
    fresh = w.c.last_heartbeat_at > func.now() - timedelta(seconds=runs.OFFLINE_AFTER_SECONDS)
    query = select(
        w.c.id,
        w.c.name,
        w.c.last_heartbeat_at,
        func.timezone(zone, w.c.last_heartbeat_at).label("heard_local"),
        func.coalesce(fresh, False).label("fresh"),
        w.c.drained_at.is_not(None).label("draining"),
        w.c.revoked_at.is_not(None).label("revoked"),
    ).where(w.c.id == worker_id)
    row = (await conn.execute(query)).one()
    return {
        "id": row.id,
        "name": row.name,
        "last_heartbeat_at": row.last_heartbeat_at.isoformat() if row.last_heartbeat_at else None,
        "last_heartbeat_local": row.heard_local.strftime("%Y-%m-%d %H:%M") if row.heard_local else None,
        "online": bool(row.fresh) and not row.revoked,
        "draining": bool(row.draining),
        "revoked": bool(row.revoked),
    }


def _worker_words(worker: dict, zone: str) -> str:
    """How the worker on duty stands, its last heartbeat in the charter's time zone."""
    seen = worker["last_heartbeat_local"]
    heard = "no heartbeat yet" if seen is None else f"last heartbeat {seen} {zone}"
    if worker["revoked"]:
        state = "revoked"
    elif not worker["online"]:
        state = "offline"
    else:
        state = "draining" if worker["draining"] else "online"
    return f"Worker {worker['name']}: {state}, {heard}."


def _count(number: int, word: str) -> str:
    return f"{number} {word}" + ("" if number == 1 else "s")


def compose(project: str, facts: dict, zone: str) -> tuple[str, str]:
    """(title, body in markdown) of a brief with ``facts``, its times in the charter's time zone ``zone``: a paragraph a
    subject, each with its list."""
    counted = facts["runs"]
    worker = facts["worker"]
    cost = f"{curator.money(facts['cost_usd'])} of {curator.money(facts['budget_usd'])}"
    decisions, proposals = facts["decisions"], facts["proposals"]
    waiting = f"{_count(decisions['open'], 'decision')} and {_count(proposals['open'], 'proposal')} waiting"
    title = f"Morning brief of {project}: {_count(counted['total'], 'run')}, {cost}, {waiting}"
    if not worker["online"]:
        title += f", worker {worker['name']} {'revoked' if worker['revoked'] else 'offline'}"
    window = facts["window"]
    ended = ", ".join(
        f"{counted[state]} {state}" for state in ("done", "failed", "cancelled", "active") if counted[state]
    )
    night = (
        f"**Night of {facts['night']}** (window {window['start']} to {window['end']}, {zone}"
        + (", still open" if facts["in_window"] else "")
        + f"): {_count(counted['total'], 'run')}"
        + (f", {ended}" if ended else "")
        + f"; cost {cost}."
        + (" The night shift is paused." if facts["paused"] else "")
    )
    blocks = [night]
    runs_listed = [
        f"- run #{run['id']} ({run['plan_id'] or run['kind']}): {run['state']}, {curator.money(run['cost_usd'])}"
        for run in counted["listed"]
    ]
    if runs_listed:
        blocks.append("\n".join(runs_listed))
    review = facts["review_run"]
    if review is not None:
        blocks.append(
            f"Review run #{review['id']}: {review['state']}, {_count(review['findings'], 'finding')}, "
            f"{_count(review['proposals'], 'proposal')}."
        )
    else:
        blocks.append("No review run this night.")
    merged = "; ".join(
        f"{item['repo']} {item['branch']} ({_count(item['commits'], 'commit')}, run #{item['run_id']})"
        for item in facts["merged"]
    )
    blocks.append(f"Merged into a default branch: {merged or 'nothing'}.")
    awaiting = facts["awaiting_merge"]
    in_review = [item for item in awaiting["listed"] if "pull_request" not in item]
    pulls = [item for item in awaiting["listed"] if "pull_request" in item]
    if in_review:
        shown = ", ".join(f"#{item['run_id']}" for item in in_review)
        count = awaiting["total"] - len(pulls)
        blocks.append(f"Waiting for your approval: {_count(count, 'run')} in review ({shown}).")
    if pulls:
        shown = "; ".join(f"{item['title']}: {item['pull_request'] or 'on GitLab'}" for item in pulls)
        blocks.append(f"The Curator's changes waiting for you to merge them: {shown}.")
    blocks.append(f"Decisions waiting for your answer: {decisions['open']}.")
    if decisions["listed"]:
        blocks.append(
            "\n".join(
                f"- decision #{item['id']} ({item['category']}, run #{item['run_id']}): {item['question']}"
                for item in decisions["listed"]
            )
        )
    blocks.append(
        f"Proposals waiting for an answer: {proposals['open']}, {proposals['in_inbox']} of them in your Inbox."
    )
    if proposals["listed"]:
        blocks.append(
            "\n".join(
                f"- proposal #{item['id']} (tier {item['tier']}): {item['title']}" for item in proposals["listed"]
            )
        )
    blocks.append(_worker_words(worker, zone))
    return title, "\n\n".join(blocks)


async def _facts(conn: AsyncConnection, due, access: ProjectAccess, night, inside: bool) -> dict:
    body = due.body
    listed = await _runs_of_night(conn, due.id, night)
    states = Counter(run["state"] for run in listed)
    run_ids = [run["id"] for run in listed]
    figures = await night_figures(conn, due.id, night)
    awaiting_total, awaiting = await _awaiting(conn, due.project_id)
    decisions_total, decisions = await _decisions(conn, due.project_id, due.owner_id)
    proposals_total, in_inbox, proposals = await _proposals(conn, access)
    paused = (
        await conn.execute(select(tables.schedules.c.paused_at.is_not(None)).where(tables.schedules.c.id == due.id))
    ).scalar_one()
    return {
        "night": night.isoformat(),
        "in_window": inside,
        "window": body["window"],
        "paused": paused,
        "state": curator_state(paused, figures.active_run_id, inside),
        "runs": {
            "total": len(listed),
            "done": states["done"],
            "failed": states["failed"] + states["lost"],
            "cancelled": states["cancelled"],
            "active": sum(states[state] for state in runs.ACTIVE_STATES),
            "listed": listed[:MAX_LISTED],
        },
        "cost_usd": round(figures.cost_usd, 6),
        "budget_usd": body["night_budget_usd"],
        "review_run": await _review(conn, run_ids),
        "merged": (await _merged(conn, run_ids))[:MAX_LISTED],
        "awaiting_merge": {"total": awaiting_total, "listed": awaiting},
        "decisions": {"open": decisions_total, "listed": decisions},
        "proposals": {"open": proposals_total, "in_inbox": in_inbox, "listed": proposals},
        "worker": await _worker(conn, due.worker_id, body["window"]["timezone"]),
    }


def _details(facts: dict) -> dict:
    """The facts a brief's notification carries for the web, the counts without the lists."""
    worker = facts["worker"]
    return {
        "night": facts["night"],
        "runs": facts["runs"]["total"],
        "cost_usd": facts["cost_usd"],
        "budget_usd": facts["budget_usd"],
        "merged": len(facts["merged"]),
        "awaiting_merge": facts["awaiting_merge"]["total"],
        "open_decisions": facts["decisions"]["open"],
        "open_proposals": facts["proposals"]["open"],
        "worker": worker["name"],
        "worker_online": worker["online"],
        "worker_last_heartbeat_at": worker["last_heartbeat_at"],
    }


async def _send(conn: AsyncConnection, due) -> str:
    """The brief of schedule ``due`` now, in the caller's transaction; a word for the job's summary."""
    body = due.body
    if not curator.brief_due(due.local_now, body["brief_at"]):
        return "not_due"
    day = due.local_now.date()
    b = tables.curator_briefs
    sent = select(b.c.id).where(b.c.project_id == due.project_id, b.c.day == day)
    if (await conn.execute(sent)).first() is not None:
        return "sent"
    access = await _owner_access(conn, due.owner_id, due.owner, due.project)
    if access is None or access.role is None:
        log.warning("morning brief skipped: its owner holds no grant", extra={"project": due.project})
        return "owner"
    window = body["window"]
    inside, night = curator.window_state(due.local_now, window["start"], window["end"])
    facts = await _facts(conn, due, access, night, inside)
    title, text = compose(due.project, facts, window["timezone"])
    stored = {"title": one_line(title), **facts}
    if len(json.dumps(stored, ensure_ascii=False).encode()) > MAX_BRIEF_BYTES:
        stored = {key: value for key, value in stored.items() if key not in ("decisions", "proposals")}
    inserted = (
        await conn.execute(
            pg_insert(b)
            .values(project_id=due.project_id, day=day, night=night, user_id=due.owner_id, body=stored)
            .on_conflict_do_nothing(constraint="curator_briefs_project_id_day_key")
            .returning(b.c.id)
        )
    ).scalar_one_or_none()
    if inserted is None:  # another pass sent it meanwhile
        return "sent"
    notification_id = await notify(
        conn,
        user_id=due.owner_id,
        kind="notice",
        notice_kind="curator_brief",
        project_id=due.project_id,
        title=title,
        body=text,
        details=_details(facts),
        link=curator_link(due.project),
    )
    await conn.execute(update(b).values(notification_id=notification_id).where(b.c.id == inserted))
    log.info(
        "morning brief sent",
        extra={"project": due.project, "day": day.isoformat(), "night": night.isoformat(), "brief_id": inserted},
    )
    return "brief"


async def send_briefs(engine: AsyncEngine, *, now: datetime | None = None) -> dict:
    """One pass of ``curator.brief`` over every night_shift schedule, each in a transaction of its own, at ``now``
    (the database's now when None); how many schedules ended in each outcome."""
    async with engine.begin() as conn:
        found = (await conn.execute(_due(now))).all()
    outcomes: Counter = Counter()
    for due in found:
        try:
            async with engine.begin() as conn:
                outcomes[await _send(conn, due)] += 1
        except Exception:  # one project failing leaves the others their brief
            log.exception("a morning brief failed", extra={"project": due.project, "schedule_id": due.id})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))
