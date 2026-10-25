"""The Growth section of a project's weekly bulletin (plan autonomous-oss-growth, criterion a17): each active Goal's
signal against its target, the posts that went out in the week, the breakers open now, the month's cost against its
cap and the week's, and the proposals for the blog project the config names (``proposals_project``).

``read`` makes it for the route ``GET /v1/x/marketing/<project>/weekly`` and for the bulletin; ``send`` tells the
project's admins once a week, from Monday 08:00 UTC, in the Inbox (and on Telegram where they linked it), with a link
to the web page. A week that was told is not told again: the notice names its ISO week in ``details``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import budget, store, tables
from evo_agents.core_plugins.marketing.models import (
    MarketingBudget,
    MarketingGoalSignal,
    MarketingWeekBreaker,
    MarketingWeekly,
    MarketingWeekPost,
    MarketingWeekProposal,
)
from evo_agents.core_plugins.marketing.settings import Config
from evo_agents.hub.server import core_plugin_api as hub

WEEK = timedelta(days=7)
SEND_HOUR = 8  # Monday, UTC
NOTICE = "plugin_notice"
MAX_ROWS = 200


def week_of(now: datetime) -> str:
    """The ISO week of ``now`` in UTC: 2026-W43."""
    year, week, _ = now.astimezone(UTC).isocalendar()
    return f"{year:04d}-W{week:02d}"


async def _goals(conn: AsyncConnection, project: str, now: datetime) -> list[MarketingGoalSignal]:
    return [
        MarketingGoalSignal(
            code=goal.code,
            statement=goal.statement,
            metric=goal.metric_text,
            value=goal.value,
            target=goal.target,
            reached=goal.reached,
        )
        for goal in await hub.measure_goals(conn, store.handle(), project, now)
    ]


async def read(conn: AsyncConnection, cfg: Config, project: str, project_id: int, now: datetime) -> MarketingWeekly:
    """The week up to ``now`` of ``project``."""
    since = now - WEEK
    p, c = tables.marketing_posts.c, tables.marketing_channels.c
    posted = await conn.execute(
        select(p.id, p.channel, p.account, p.posted_url, p.posted_at, p.cost_usd)
        .where(p.project_id == project_id, p.state == "posted", p.dry_run.is_(False), p.posted_at >= since)
        .order_by(p.posted_at.desc(), p.id.desc())
        .limit(MAX_ROWS)
    )
    states = await conn.execute(
        select(p.state, func.count()).where(p.project_id == project_id, p.created_at >= since).group_by(p.state)
    )
    cost = await conn.scalar(
        select(func.coalesce(func.sum(p.cost_usd), 0)).where(p.project_id == project_id, p.posted_at >= since)
    )
    personal = await conn.execute(
        select(p.id, p.channel, p.account, p.state, p.created_at)
        .join_from(tables.marketing_posts, tables.marketing_channels, c.id == p.channel_id)
        .where(p.project_id == project_id, c.account_kind == "personal", p.created_at >= since)
        .order_by(p.created_at.desc(), p.id.desc())
        .limit(MAX_ROWS)
    )
    rows = await store.channels(conn, project_id)
    stopped = await store.killed(conn, project_id)
    month = budget.month_of(now)
    reserved, spent = await budget.status(conn, project_id, month)
    return MarketingWeekly(
        project=project,
        since=since,
        until=now,
        goals=await _goals(conn, project, now),
        posted=[MarketingWeekPost(**{**row._mapping, "cost_usd": float(row.cost_usd)}) for row in posted],
        counts={state: count for state, count in states},
        breakers=[
            MarketingWeekBreaker(
                channel_id=r.id, channel=r.channel, account=r.account, reason=r.paused_reason, paused_at=r.paused_at
            )
            for r in rows
            if r.paused_at is not None
        ],
        killed=stopped is not None,
        reason=stopped.reason if stopped else None,
        budget=MarketingBudget(
            month=month, budget_usd=cfg.budget_usd, reserved_usd=float(reserved), spent_usd=float(spent)
        ),
        week_cost_usd=float(cost or 0),
        proposals=[MarketingWeekProposal(**row._mapping) for row in personal],
        proposals_project=cfg.proposals_project or None,
    )


def text(week: MarketingWeekly) -> str:
    """The Growth section as the bulletin's body: one line for each part."""
    lines = ["Growth"]
    for goal in week.goals:
        value = "no value" if goal.value is None else f"{goal.value:g}"
        state = {True: "reached", False: "not reached", None: "not measured"}[goal.reached]
        lines.append(f"- {goal.code} {goal.metric}: {value}, {state}")
    lines.append(f"- Posts that went out: {len(week.posted)}")
    lines += [f"  - {post.channel}/{post.account} {post.posted_url or ''}".rstrip() for post in week.posted[:10]]
    lines.append(f"- Breakers open: {len(week.breakers)}")
    lines += [f"  - {b.channel}/{b.account}: {b.reason or 'no reason'}" for b in week.breakers]
    if week.killed:
        lines.append(f"- Kill switch set: {week.reason or 'no reason'}")
    cap = week.budget
    lines.append(
        f"- Cost: {week.week_cost_usd:.2f} USD this week; {cap.spent_usd:.2f} spent and {cap.reserved_usd:.2f} "
        f"reserved of {cap.budget_usd:.2f} USD in {cap.month}"
    )
    blog = week.proposals_project or "the blog project (config proposals_project is not set)"
    lines.append(f"- Proposals for {blog}: {len(week.proposals)}")
    return "\n".join(lines)


async def send(conn: AsyncConnection, cfg: Config, project: str, project_id: int, now: datetime) -> bool:
    """Tell the project's admins the Growth section of the week, once from Monday 08:00 UTC; whether this call told."""
    utc = now.astimezone(UTC)
    if utc.weekday() != 0 or utc.hour < SEND_HOUR:
        return False
    week = week_of(utc)
    if await hub.admins_notified(conn, store.handle(), project, "weekly", week):
        return False
    found = await read(conn, cfg, project, project_id, utc)
    body = text(found)
    await hub.notify_admins(
        conn,
        store.handle(),
        project,
        title=f"Growth weekly of {project}, {week}",
        body=body,
        link=f"/p/{project}/x/marketing/weekly",
        details={"weekly": week},
    )
    return True
