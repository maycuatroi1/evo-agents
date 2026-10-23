"""The rule ``growth`` of the plugin marketing on the Loop (extension point loop_rules; criterion a16 of plan
autonomous-oss-growth, ``docs/marketing-plugin.md``).

- A claim of the Channels or Customer segments cell that an approved baseline moves to testing makes a campaign: a
  ``growth_idea`` task of @marketer that turns the claim's test card into a plan of posts, each proposed through
  ``marketing__propose`` with the claim, a Signal key and its time.
- A scheduled post whose time came, while the project's kill switch is off, makes a ``growth_publish`` job: once it
  runs, the post is released and the job ``advance`` takes it through the gate like any post; the work item is done when
  the gate decided it.
- A post that went out makes a ``growth_review`` job: done when its numbers at +7d are kept, or 8 days after it went
  out, with the numbers in its detail.
- A new reply under a post of a project account makes a ``growth_draft`` task of @marketer, at most ``replies_per_day``
  in 24 hours: the answer is a reply through the gate. A personal account's posts are never read for replies, and a
  personal account never replies (``flow.personal_guard``).

The handler reads with its own connection and writes nothing but the release of a post; the hub writes the work items.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import settings, store
from evo_agents.hub import tables

CELLS = ("channels", "customer_segments")
TESTING = "testing"
IDEA, DRAFT, PUBLISH, REVIEW = "growth_idea", "growth_draft", "growth_publish", "growth_review"
MEASURE_DAYS = 8  # a post is measured until then: the +7d numbers are read by then or never
REPLY_DAYS = 7  # how far back a reply is still answered
BATCH = 20  # work items of one kind one call makes at most
VIETNAMESE = ("viblo", "facebook_page", "facebook_profile", "tiktok")
LANGUAGE = (
    "Write in English on international channels (github, hackernews, reddit, x, linkedin) and in Vietnamese on viblo, "
    "facebook_page, facebook_profile and tiktok."
)


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


async def _taken(conn: AsyncConnection, project_id: int, keys: list[str]) -> set[str]:
    """The keys of ``keys`` the project already has a work item of, finished or not (as stored, ``marketing:``)."""
    if not keys:
        return set()
    i = tables.loop_items.c
    stored = [f"{store.PLUGIN}:{key}" for key in keys]
    found = await conn.scalars(select(i.key).where(i.project_id == project_id, i.key.in_(stored)))
    return {key.removeprefix(f"{store.PLUGIN}:") for key in found}


# A campaign


async def _claims(conn: AsyncConnection, ids: list[int]) -> list:
    c = tables.canvas.claims.c
    found = await conn.execute(
        select(c.id, c.number, c.cell, c.text, c.test_card, c.signal_keys).where(c.id.in_(ids), c.cell.in_(CELLS))
    )
    return list(found)


def _channels_text(channels: list) -> str:
    lines = [
        f"- channel_id {row.id}: {row.channel} {row.account} ({row.account_kind}, {row.mode}"
        f"{', paused' if row.paused else ''})"
        for row in channels
    ]
    return "\n".join(lines) or "- none: say so and propose no post"


def campaign_task(claim, channels: list) -> str:
    """What @marketer is asked for a claim in testing."""
    card = json.dumps(claim.test_card or {}, ensure_ascii=False, sort_keys=True)
    keys = ", ".join(claim.signal_keys or []) or "none yet: name one per post"
    return (
        f"Claim CL-{claim.number} ({claim.cell}) is in testing: {claim.text}\n\n"
        f"Its test card: {card}\nIts Signal keys: {keys}\n\n"
        "Make the plan of posts the test card asks for, on the accounts below. Propose each post with "
        f"marketing__propose: claim CL-{claim.number}, one Signal key, and scheduled_at when it should go out. "
        f"Every post passes the gate; one that fails becomes a draft with the reason.\n{LANGUAGE}\n\n"
        f"The project's accounts:\n{_channels_text(channels)}"
    )


async def _campaigns(conn: AsyncConnection, params: dict) -> list[dict]:
    payload = (params["event"] or {}).get("payload") or {}
    moved = {
        t["claim_id"]: t
        for t in payload.get("transitions") or []
        if isinstance(t, dict) and t.get("to") == TESTING and t.get("from") != TESTING
    }
    if not moved:
        return []
    channels = [row for row in await store.channels(conn, params["project_id"]) if row.account_kind == "project"]
    found = []
    for claim in await _claims(conn, sorted(moved)):
        detail = {"task": campaign_task(claim, channels), "title": f"Campaign for CL-{claim.number}"}
        detail |= {"claim": f"CL-{claim.number}", "baseline": payload.get("baseline_id")}
        key = f"campaign:{claim.id}:b{payload.get('baseline_id')}"
        found.append({"effect": "work", "kind": IDEA, "key": key, "claim_id": claim.id, "detail": detail})
    return found


# Publishing at a time, and measuring


async def _publish(conn: AsyncConnection, project_id: int, now: datetime, killed: bool) -> list[dict]:
    if killed:
        return []
    due = await store.due_posts(conn, project_id, now, BATCH)
    taken = await _taken(conn, project_id, [f"publish:{row.id}" for row in due])
    return [
        {"effect": "work", "kind": PUBLISH, "key": f"publish:{row.id}", "detail": {"post_id": row.id}}
        for row in due
        if f"publish:{row.id}" not in taken
    ]


async def _measure(conn: AsyncConnection, project_id: int, now: datetime) -> list[dict]:
    went = await store.gone_out(conn, project_id, now - timedelta(days=MEASURE_DAYS), BATCH * 5)
    taken = await _taken(conn, project_id, [f"measure:{row.id}" for row in went])
    fresh = [row for row in went if f"measure:{row.id}" not in taken][:BATCH]
    return [
        {"effect": "work", "kind": REVIEW, "key": f"measure:{row.id}", "detail": {"post_id": row.id}} for row in fresh
    ]


async def _post(conn: AsyncConnection, project_id: int, item: dict):
    post_id = item["detail"].get("post_id")
    return await store.post(conn, post_id, project_id=project_id) if isinstance(post_id, int) else None


async def _publishing(conn: AsyncConnection, project_id: int, item: dict, now: datetime, killed: bool) -> dict | None:
    """Where a running growth_publish work item goes: release its post, then done once the gate decided it."""
    post = await _post(conn, project_id, item)
    if post is None:
        return {"effect": "move", "item_id": item["id"], "state": "cancelled", "reason": "dropped"}
    if post.state in store.OPEN:
        if post.released_at is None and not killed:
            await store.release(conn, post.id, now)
        return None
    return {"effect": "move", "item_id": item["id"], "state": "done", "detail": {"post_state": post.state}}


async def _measuring(conn: AsyncConnection, project_id: int, item: dict, now: datetime) -> dict | None:
    """Where a running growth_review work item goes: done with the numbers once +7d is kept or 8 days passed."""
    post = await _post(conn, project_id, item)
    if post is None:
        return {"effect": "move", "item_id": item["id"], "state": "cancelled", "reason": "dropped"}
    numbers = await store.kept_numbers(conn, post.id)
    old = post.posted_at is not None and now - post.posted_at >= timedelta(days=MEASURE_DAYS)
    if "7d" not in numbers and not old:
        return None
    detail = {"post_id": post.id, "signal_key": post.signal_key, "numbers": numbers, "url": post.posted_url}
    return {"effect": "move", "item_id": item["id"], "state": "done", "detail": detail}


async def _running(
    conn: AsyncConnection, project_id: int, items: list[dict], now: datetime, killed: bool
) -> list[dict]:
    found = []
    for item in items:
        if item["state"] != "running":
            continue
        if item["kind"] == PUBLISH:
            moved = await _publishing(conn, project_id, item, now, killed)
        elif item["kind"] == REVIEW:
            moved = await _measuring(conn, project_id, item, now)
        else:
            moved = None
        if moved is not None:
            found.append(moved)
    return found


# Replies


def reply_task(reply) -> str:
    """What @marketer is asked for a reply under a post of a project account."""
    return (
        f"Someone replied under the project's {reply.channel} post {reply.posted_url or reply.post_id}.\n"
        f"Author: {reply.author or 'unknown'}\nLink: {reply.url or 'none'}\nReply:\n{reply.body}\n\n"
        f"Answer it once, if an answer helps them: marketing__propose with kind reply, channel_id {reply.channel_id} "
        f"and reply_to {reply.external_id}. Answer only from what the project's docs, releases and changelog say, "
        f"with the link as a source. Leave spam, insults and anything about a person unanswered.\n{LANGUAGE}"
    )


async def _made_today(conn: AsyncConnection, project_id: int, now: datetime) -> int:
    i = tables.loop_items.c
    query = select(func.count()).where(
        i.project_id == project_id,
        i.key.like(f"{store.PLUGIN}:reply:%"),
        i.created_at >= now - timedelta(hours=24),
    )
    return int(await conn.scalar(query) or 0)


async def _replies(conn: AsyncConnection, project_id: int, now: datetime, per_day: int) -> list[dict]:
    room = per_day - await _made_today(conn, project_id, now)
    if room <= 0:
        return []
    seen = await store.new_replies(conn, project_id, now - timedelta(days=REPLY_DAYS), BATCH * 5)
    taken = await _taken(conn, project_id, [f"reply:{row.id}" for row in seen])
    fresh = [row for row in seen if f"reply:{row.id}" not in taken][: min(room, BATCH)]
    return [
        {
            "effect": "work",
            "kind": DRAFT,
            "key": f"reply:{row.id}",
            "detail": {"task": reply_task(row), "title": f"Answer a reply on {row.channel}", "reply_id": row.id},
        }
        for row in fresh
    ]


async def _clock(conn: AsyncConnection, params: dict, context) -> list[dict]:
    project_id, now = params["project_id"], _at(params["now"])
    killed = await store.killed(conn, project_id) is not None
    cfg = await settings.config(conn, getattr(context, "config", None), params["project"])
    effects = await _running(conn, project_id, params["items"], now, killed)
    effects += await _publish(conn, project_id, now, killed)
    effects += await _measure(conn, project_id, now)
    effects += await _replies(conn, project_id, now, cfg.replies_per_day)
    return effects


async def growth(params: dict, context) -> dict:
    """The handler of rule ``growth``: a campaign on a baseline, the rest on the clock."""
    async with context.engine.begin() as conn:
        if params["event"] is None:
            effects = await _clock(conn, params, context)
        else:
            effects = await _campaigns(conn, params)
    return {"effects": effects[:50]}


__all__ = ["CELLS", "campaign_task", "growth", "reply_task"]
