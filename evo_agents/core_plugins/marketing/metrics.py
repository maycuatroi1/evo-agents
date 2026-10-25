"""The numbers of a post at +24h and +7d after it went out (``docs/marketing-plugin.md``), read once from the platform and
kept in marketing_metrics, then pushed to Signal (``push``).

Only X and Reddit have an adapter that reads them (likes, reposts, replies and impressions; score, comments and whether
the platform removed the post). Facebook and TikTok are posted to and never read (non-goal of plan autonomous-oss-growth),
GitHub and LinkedIn give nothing the plugin reads. A removed post or a score of zero or less feeds the breaker
(``breaker.after_metrics``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import adapters, breaker, registry, store, tables
from evo_agents.core_plugins.marketing.settings import Config

SPANS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}


async def _kept(conn: AsyncConnection, post_id: int) -> set[str]:
    m = tables.marketing_metrics.c
    return {row.span for row in await conn.execute(select(m.span).where(m.post_id == post_id))}


async def _read(adapter, post, token: str) -> dict[str, float]:
    try:
        return await adapter.metrics(post.external_id, token)
    except adapters.AdapterError:
        return {}


async def collect(conn: AsyncConnection, cfg: Config, project: str, project_id: int, transport, now: datetime) -> int:
    """Read the numbers of every post that went out and reached +24h or +7d without them; how many numbers were kept."""
    kept = 0
    for post in await store.posts(conn, project_id, state="posted"):
        token = cfg.token(post.channel)
        adapter = adapters.for_channel(post.channel, transport)
        if (
            post.dry_run
            or not post.external_id
            or post.posted_at is None
            or not token
            or not hasattr(adapter, "metrics")
        ):
            continue
        have = await _kept(conn, post.id)
        due = [span for span, wait in SPANS.items() if span not in have and now - post.posted_at >= wait]
        if not due:
            continue
        found = await _read(adapter, post, token)
        for span in due:
            for metric, value in found.items():
                statement = pg_insert(tables.marketing_metrics).values(
                    post_id=post.id, span=span, metric=metric, value=value
                )
                await conn.execute(statement.on_conflict_do_nothing(constraint="marketing_metrics_once_key"))
                kept += 1
        channel = await store.channel(conn, project_id, post.channel_id)
        await breaker.after_metrics(conn, project_id, project, channel, registry.spec(post.channel).scored)
    return kept
