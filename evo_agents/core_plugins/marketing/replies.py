"""The replies under the posts of a project account, read by the job ``advance`` (criterion a16 of plan
autonomous-oss-growth): each new one is kept once in marketing_replies, and the Loop's rule ``growth`` (``loop``) makes
a reply work item of @marketer for it, within the project's ``replies_per_day``.

Only the posts of the last REPLY_DAYS days on a project account are read, and only on the channels whose adapter reads
replies (Reddit comments, GitHub Discussion comments; ``adapters.apis``). A personal account's posts are never read:
the agent never replies on a personal account. The account's own replies are skipped. A read that fails changes
nothing: the next pass reads again.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import adapters, store, tables
from evo_agents.core_plugins.marketing.settings import Config
from evo_agents.core_plugins.marketing.tables import MAX_REPLY_TEXT

REPLY_DAYS = 7
MAX_POSTS = 50  # posts read in one pass


async def _posts(conn: AsyncConnection, project_id: int, since: datetime) -> list:
    p, c = tables.marketing_posts.c, tables.marketing_channels.c
    found = await conn.execute(
        select(p.id, p.channel, p.external_id, c.account)
        .join_from(tables.marketing_posts, tables.marketing_channels, c.id == p.channel_id)
        .where(
            p.project_id == project_id,
            p.state == "posted",
            p.dry_run.is_(False),
            p.external_id.is_not(None),
            p.posted_at >= since,
            c.account_kind == "project",
        )
        .order_by(p.posted_at.desc())
        .limit(MAX_POSTS)
    )
    return list(found)


async def _read(adapter, external_id: str, token: str) -> list[dict]:
    try:
        return await adapter.replies(external_id, token)
    except adapters.AdapterError:
        return []


async def collect(conn: AsyncConnection, cfg: Config, project_id: int, transport, now: datetime) -> int:
    """Read the replies under the project accounts' recent posts; how many were new."""
    kept = 0
    for post in await _posts(conn, project_id, now - timedelta(days=REPLY_DAYS)):
        token = cfg.token(post.channel)
        adapter = adapters.for_channel(post.channel, transport)
        if not token or not hasattr(adapter, "replies"):
            continue
        for found in await _read(adapter, post.external_id, token):
            author, body = found.get("author"), str(found.get("body") or "").strip()
            if not body or (author and str(author).lower() == post.account.lower()):
                continue
            kept += await store.add_reply(
                conn,
                project_id,
                post.id,
                external_id=str(found["external_id"]),
                author=author,
                url=found.get("url"),
                body=body[:MAX_REPLY_TEXT],
            )
    return kept
