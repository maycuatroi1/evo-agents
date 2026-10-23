"""What the plugin pushes to the project's Signal (``docs/marketing-plugin.md``).

The project numbers: ``marketing.posts_week`` (posts that went out in the last 7 days), ``marketing.breaker_open``
(channels whose breaker is open), ``marketing.spend_usd_month`` (what this month cost) and ``marketing.judge_pass_rate``
(the share of the posts the Judge scored in the last 30 days that it passed). The numbers of one post, at +24h and +7d
after it went out: ``marketing.<metric>_24h`` and ``marketing.<metric>_7d`` (``metrics``).

The hub takes a signal of a key an admin declared (``evo-agents hub signal key add``): the plugin declares none, and a
key that is not declared is counted in ``rejected`` of the job's answer, nothing more. The numbers are written as the
ingest key ``marketing`` of the project, which the plugin makes once with a token nobody sees (an admin who revokes the
key stops the pushes). A project number is sent once a day for each value it takes (``external_id``); a post's number is
sent once. A signal carries no text of a post: only numbers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import budget
from evo_agents.hub import signals, tables
from evo_agents.hub.server.signals import SignalIn

KEY_NAME = "marketing"  # the name of the project's ingest key
SOURCE = "marketing"
DEFAULT_DAYS = 90


def key_of(name: str) -> str:
    return f"marketing.{name}"


async def ingest_pusher(conn: AsyncConnection, project_id: int) -> signals.Pusher | None:
    """The pusher of the key ``marketing`` of the project, made on first use; None when no admin can own the key or the
    admins revoked the one it made and made no other."""
    k, g = tables.signal_ingest_keys, tables.grants
    rules = await signals.project_rules(conn, project_id)
    found = (
        await conn.execute(
            select(k.c.id, k.c.max_level, k.c.revoked_at).where(k.c.project_id == project_id, k.c.name == KEY_NAME)
        )
    ).all()
    live = next((row for row in found if row.revoked_at is None), None)
    if live is None and found:
        return None
    if live is None:
        owner = await conn.scalar(
            select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id).limit(1)
        )
        if owner is None:
            return None
        token = signals.new_ingest_token()  # nobody reads it: the plugin writes in this process
        made = await conn.execute(
            insert(k)
            .values(
                project_id=project_id,
                name=KEY_NAME,
                max_level=rules.default_label.get("level"),
                token_hash=signals.hash_token(token),
                created_by=owner,
            )
            .returning(k.c.id, k.c.max_level, k.c.revoked_at)
        )
        live = made.one()
    return signals.Pusher(project_id, rules, "ingest", live.id, KEY_NAME, live.max_level)


async def project_numbers(conn: AsyncConnection, project_id: int, now: datetime) -> dict[str, float]:
    p, c = tables.marketing_posts.c, tables.marketing_channels.c
    week = await conn.scalar(
        select(func.count()).where(
            p.project_id == project_id,
            p.state == "posted",
            p.dry_run.is_(False),
            p.posted_at >= now - timedelta(days=7),
        )
    )
    opened = await conn.scalar(select(func.count()).where(c.project_id == project_id, c.paused_at.is_not(None)))
    _, spent = await budget.status(conn, project_id, budget.month_of(now))
    judged = (
        await conn.execute(
            select(p.judge["ok"].astext.label("ok")).where(
                p.project_id == project_id, p.judge.is_not(None), p.created_at >= now - timedelta(days=30)
            )
        )
    ).all()
    numbers = {"posts_week": float(week), "breaker_open": float(opened), "spend_usd_month": float(spent)}
    if judged:
        numbers["judge_pass_rate"] = sum(1 for row in judged if row.ok == "true") / len(judged)
    return numbers


async def push_project(conn: AsyncConnection, project_id: int, days: int = DEFAULT_DAYS) -> dict:
    """Push the project numbers and the numbers of posts not pushed yet; what the hub stored, found twice and refused."""
    pusher = await ingest_pusher(conn, project_id)
    if pusher is None:
        return {"pushed": False, "reason": "no ingest key marketing"}
    now = datetime.now(UTC)
    today = now.date().isoformat()
    items = [
        SignalIn(key=key_of(name), value=value, source=SOURCE, external_id=f"marketing:{name}:{today}:{value:g}")
        for name, value in (await project_numbers(conn, project_id, now)).items()
    ]
    m, p = tables.marketing_metrics, tables.marketing_posts.c
    rows = (
        await conn.execute(
            select(m.c.id, m.c.post_id, m.c.span, m.c.metric, m.c.value)
            .join_from(m, tables.marketing_posts, p.id == m.c.post_id)
            .where(p.project_id == project_id, m.c.pushed_at.is_(None))
        )
    ).all()
    items += [
        SignalIn(
            key=key_of(f"{r.metric}_{r.span}"),
            value=float(r.value),
            source=SOURCE,
            external_id=f"marketing:post:{r.post_id}:{r.span}:{r.metric}",
        )
        for r in rows
    ]
    done = await signals.push(conn, pusher, items, days)
    if rows and not done.rejected:  # a number whose key no admin declared yet stays for the next pass
        await conn.execute(update(m).where(m.c.id.in_([r.id for r in rows])).values(pushed_at=func.now()))
    return {"pushed": True, "accepted": done.accepted, "duplicates": done.duplicates, "rejected": len(done.rejected)}
