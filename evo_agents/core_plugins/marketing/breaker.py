"""The circuit breaker of each channel and the kill switch of the project (``docs/marketing-plugin.md``).

A breaker opens by itself and closes only by hand: no half-open state, no retry that tries the platform again. It opens
at once for a captcha, a checkpoint, a lock or a warning of the platform and for one report or complaint; after two
failed posts in a row; after two posts the platform removed in 7 days; when two of the last three posts of a scored
channel (Reddit) ended with a score of zero or less; and after three Judge failures in a row. A channel whose breaker
is open posts nothing: its posts become drafts.

The kill switch stops every channel of the project. Any member with a grant sets it; only an admin clears it. It is set
by itself when three channels opened their breaker within 24 hours and when the month's spend passed the budget.
Every opening, closing and kill writes the audit and tells the project's admins in the Inbox (and Telegram, for the
admins who linked it): the notice kind is ``plugin_notice``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import store, tables
from evo_agents.hub.server import core_plugin_api as hub

AUTO_KILL_CHANNELS = 3  # channels opened within AUTO_KILL_WINDOW
AUTO_KILL_WINDOW = timedelta(hours=24)
FAILURES_IN_A_ROW = 2
REMOVED_IN_WEEK = 2
JUDGE_FAILURES_IN_A_ROW = 3
NOTICE = "plugin_notice"


async def tell(conn: AsyncConnection, project_id: int, project: str, title: str, body: str) -> None:
    """The project's admins read ``title`` in the Inbox, and on Telegram where they linked it."""
    await hub.notify_admins(conn, store.handle(), project, title=title, body=body)


async def _audit(conn: AsyncConnection, project: str, action: str, target: str, by) -> None:
    await hub.record_audit(conn, store.handle(), project, action, target, actor=by)


async def trip(
    conn: AsyncConnection, project_id: int, project: str, channel, reason: str, *, by: int | None = None
) -> bool:
    """Open the breaker of ``channel`` (a row of marketing_channels); whether this call opened it. Three channels open in
    24 hours set the kill switch."""
    if not await store.pause_channel(conn, channel.id, reason):
        return False
    label = f"{channel.channel}/{channel.account}"
    await _audit(conn, project, "marketing.breaker.open", f"channel:{channel.id}", by)
    await tell(conn, project_id, project, f"Marketing: breaker open on {label}", reason)
    since = datetime.now(UTC) - AUTO_KILL_WINDOW
    if await store.open_channels(conn, project_id, since) >= AUTO_KILL_CHANNELS:
        await kill(
            conn, project_id, project, None, f"{AUTO_KILL_CHANNELS} channels opened their breaker within 24 hours"
        )
    return True


async def kill(conn: AsyncConnection, project_id: int, project: str, by: hub.Session | None, reason: str) -> bool:
    """Set the kill switch of the project, by the member of session ``by`` or by the plugin itself (None); whether it
    changed."""
    if not await store.set_killed(conn, project_id, True, reason, None if by is None else by.user_id):
        return False
    await _audit(conn, project, "marketing.kill", f"project:{project_id}", by)
    await tell(conn, project_id, project, "Marketing: kill switch set", reason)
    return True


async def unkill(conn: AsyncConnection, project_id: int, project: str, by: hub.Session) -> bool:
    if not await store.set_killed(conn, project_id, False, None, None):
        return False
    await _audit(conn, project, "marketing.resume", f"project:{project_id}", by)
    await tell(conn, project_id, project, "Marketing: kill switch cleared", "An admin resumed the project's channels.")
    return True


async def close(conn: AsyncConnection, project: str, channel, by: hub.Session) -> bool:
    """Close the breaker of ``channel`` by hand; whether it was open."""
    if channel.paused_at is None:
        return False
    await store.update_channel(conn, channel.id, paused_at=None, paused_reason=None)
    await _audit(conn, project, "marketing.breaker.close", f"channel:{channel.id}", by)
    return True


# What opens a breaker


async def after_failure(conn: AsyncConnection, project_id: int, project: str, channel, kind: str, message: str) -> bool:
    """A post that did not go out: ``locked`` opens the breaker, ``rejected`` and ``failed`` open it on the second in a
    row. Whether the breaker opened."""
    if kind == "locked":
        return await trip(conn, project_id, project, channel, f"the platform locked or warned the account: {message}")
    last = await store.last_posts(conn, channel.id, ("posted", "failed"), FAILURES_IN_A_ROW)
    if len(last) == FAILURES_IN_A_ROW and all(p.state == "failed" for p in last):
        return await trip(conn, project_id, project, channel, f"{FAILURES_IN_A_ROW} failed posts in a row: {message}")
    return False


async def after_judge(conn: AsyncConnection, project_id: int, project: str, channel) -> bool:
    """Three posts in a row that the Judge failed open the breaker."""
    last = await store.last_posts(conn, channel.id, ("draft", "withdrawn", "posted", "failed"), JUDGE_FAILURES_IN_A_ROW)
    judged = [p.judge for p in last if p.judge]
    if len(last) == JUDGE_FAILURES_IN_A_ROW and len(judged) == len(last) and all(not j.get("ok") for j in judged):
        return await trip(
            conn, project_id, project, channel, f"the Judge failed {JUDGE_FAILURES_IN_A_ROW} posts in a row"
        )
    return False


async def after_metrics(conn: AsyncConnection, project_id: int, project: str, channel, scored: bool) -> bool:
    """Read the numbers kept for the channel: two removed posts in a week, and on a scored channel a score of zero or
    less on two of the last three posts. Whether the breaker opened."""
    m, p = tables.marketing_metrics.c, tables.marketing_posts.c
    week = datetime.now(UTC) - timedelta(days=7)
    removed = await conn.execute(
        select(p.id)
        .join_from(tables.marketing_posts, tables.marketing_metrics, m.post_id == p.id)
        .where(p.channel_id == channel.id, m.metric == "removed", m.value > 0, p.posted_at >= week)
    )
    if len(removed.all()) >= REMOVED_IN_WEEK:
        return await trip(
            conn, project_id, project, channel, f"{REMOVED_IN_WEEK} posts removed by the platform in 7 days"
        )
    if not scored:
        return False
    last = await store.last_posts(conn, channel.id, ("posted",), 3)
    scores = await conn.execute(
        select(m.post_id, m.value).where(
            m.post_id.in_([x.id for x in last] or [0]), m.metric == "score", m.span == "24h"
        )
    )
    held = {row.post_id: row.value for row in scores}
    bad = sum(1 for x in last if x.id in held and held[x.id] <= 0)
    if len(last) == 3 and bad >= 2:
        return await trip(conn, project_id, project, channel, "the score of 2 of the last 3 posts is zero or less")
    return False
