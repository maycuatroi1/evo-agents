"""The browser adapter of the plugin marketing, hub side: the queue between the gate and the script on a worker.

A browser cannot run in the hub, and the login session of an account must reach no model. So a post for a channel with
no posting API (Viblo, the Facebook profile, Reddit or TikTok before their API is approved; Hacker News stays draft
only) that passes the gate is ``queued``. A script on a worker with a browser (``evo-agents hub marketing browser-post``,
``evo_agents.hub.marketing_browser``) claims the oldest one: the post is ``posting`` and the answer holds the text and
the sealed login session of the account. The script fills the form, and reports the outcome:

- ``posted``: the post is ``posted`` with its link and the screenshot taken after it;
- ``captcha`` or ``lockout`` (a captcha, a checkpoint, a lock or a warning, a 403 or a 429): the post failed, the
  breaker of the channel opens at once, and the screenshot is kept, labelled internal;
- ``session_expired`` (the browser was sent to the login page): the post failed, the channel stops and its session is
  ``stale``, and the admins read in the Inbox that its owner logs in again;
- ``failed``: the post failed, which counts towards the two in a row that open the breaker.

Only a person's login may claim or report, never the scope of an agent's run, so no model reaches a session. The script
never retries: a post whose script reports nothing for ``STUCK`` is a failed post, and one nobody claims for a day is a
draft. A claim checks the kill switch, the breaker, the mode and the session again, as the queue may have waited.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import breaker, store, tables
from evo_agents.core_plugins.marketing.outcome import finish
from evo_agents.core_plugins.marketing.settings import Config
from evo_agents.core_plugins.marketing.tables import BROWSER_NEEDS_MEDIA, MAX_SHOT_BYTES, OUTCOMES

STUCK = timedelta(minutes=30)  # a claimed post with no report after this has failed
UNCLAIMED = timedelta(hours=24)  # a queued post no script claimed after this is a draft
LOGIN_HINT = "its owner logs in again with `evo-agents hub marketing login`"
FAILURE_KIND = {"captcha": "locked", "lockout": "locked", "session_expired": "locked", "failed": "failed"}


def fallback(cfg: Config, channel) -> bool:
    """Whether an API channel with no approved credential (Reddit, LinkedIn before their API is approved) posts through
    the browser instead: its account has a sealed session and its page takes text alone."""
    return BROWSER_NEEDS_MEDIA.get(channel.channel) is False and cfg.session(channel.channel) is not None


async def not_ready(conn: AsyncConnection, cfg: Config, post, channel) -> str | None:
    """Why a post that passed the gate cannot be queued for the browser, or None when it can."""
    needs_media = BROWSER_NEEDS_MEDIA.get(channel.channel)
    if post.kind == "reply":
        return "the browser adapter posts original posts only, never a reply"
    if needs_media is None:
        return f"no browser profile for {channel.channel}: a person posts it"
    if needs_media:
        return f"{channel.channel} takes a video, which the browser adapter does not upload: a person posts it"
    if channel.session_state == "stale":
        return f"the browser session of {channel.channel} expired: {LOGIN_HINT}"
    if not cfg.session(channel.channel):
        return f"no browser session for {channel.channel}: {LOGIN_HINT}"
    if await store.in_flight(conn, channel.id):
        return f"another post of {channel.channel} is on its way: one at a time"
    return None


def _why_not(killed, channel, cfg: Config) -> str | None:
    if killed is not None:
        return f"the kill switch of the project is set: {killed.reason or 'no reason given'}"
    if channel.paused_at is not None:
        return f"the breaker of the channel is open: {channel.paused_reason or 'no reason'}"
    if channel.mode != "autonomous":
        return "the account is draft_only: a person posts it"
    if channel.session_state == "stale" or not cfg.session(channel.channel):
        return f"the browser session of {channel.channel} is gone: {LOGIN_HINT}"
    return None


async def claim(conn: AsyncConnection, cfg: Config, project_id: int, now: datetime) -> dict | None:
    """The oldest queued post that may still go out, as the job a script runs (``posting`` from now on); posts that may
    not (a breaker opened while they waited) become drafts on the way. None when nothing waits."""
    t = tables.marketing_posts.c
    killed = await store.killed(conn, project_id)
    waiting = await conn.execute(
        select(*store.post_columns())
        .where(t.project_id == project_id, t.state == "queued")
        .order_by(t.id)
        .with_for_update(skip_locked=True)
    )
    for post in list(waiting):
        channel = await store.channel(conn, project_id, post.channel_id)
        why = _why_not(killed, channel, cfg)
        if why:
            layers = {**post.layers, "claim": {"ok": False, "detail": why}}
            await finish(conn, project_id, post, "draft", why, layers)
            continue
        await store.save_post(conn, post.id, state="posting", claimed_at=now)
        return {
            "post_id": post.id,
            "channel": post.channel,
            "account": post.account,
            "target": channel.target,
            "body": post.body,
            "session": cfg.session(post.channel),
        }
    return None


def _picture(encoded: str | None) -> bytes | None:
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise store.MarketingError(422, "the screenshot is not base64") from None
    if len(raw) > MAX_SHOT_BYTES:
        raise store.MarketingError(422, f"the screenshot is over {MAX_SHOT_BYTES // 1024} KiB")
    return raw


async def report(
    conn: AsyncConnection,
    project: str,
    project_id: int,
    post_id: int,
    outcome: str,
    url: str | None,
    detail: str,
    screenshot: str | None,
    now: datetime,
) -> dict:
    """What the script says of the post it claimed. The post ends (posted or failed) and the screenshot is kept; the
    breaker and the session of the channel follow the outcome. Returns the state and the reason."""
    if outcome not in OUTCOMES:
        raise store.MarketingError(422, f"the outcome is one of {', '.join(OUTCOMES)}")
    post = await store.post(conn, post_id, project_id=project_id, lock=True)
    if post is None:
        raise store.MarketingError(404, f"no post {post_id} in project {project}")
    if post.state != "posting":
        raise store.MarketingError(409, f"post {post_id} is {post.state}, not claimed by a script")
    picture = _picture(screenshot)
    channel = await store.channel(conn, project_id, post.channel_id)
    layers = {**post.layers}
    if picture is not None:
        layers["screenshot"] = {
            "label": "internal",
            "when": "after the post" if outcome == "posted" else "where the script stopped",
            "bytes": len(picture),
            "sha256": hashlib.sha256(picture).hexdigest(),
        }
    shot = {"screenshot": picture} if picture is not None else {}
    if outcome == "posted":
        if not url:
            raise store.MarketingError(422, "a post that went out reports its link (url)")
        layers["adapter"] = {"ok": True, "detail": url}
        await store.update_channel(conn, channel.id, session_state="ok")
        state = await finish(conn, project_id, post, "posted", None, layers, posted_url=url, posted_at=now, **shot)
        return {"state": state, "reason": None}
    message = f"{outcome}: {detail[:300]}" if detail else outcome
    layers["adapter"] = {"ok": False, "detail": message}
    state = await finish(conn, project_id, post, "failed", message, layers, **shot)
    if outcome == "session_expired":
        await store.update_channel(conn, channel.id, session_state="stale")
        await breaker.trip(
            conn, project_id, project, channel, f"the browser session of {channel.channel} expired: {LOGIN_HINT}"
        )
    else:
        await breaker.after_failure(conn, project_id, project, channel, FAILURE_KIND[outcome], message)
    return {"state": state, "reason": message}


async def expire(conn: AsyncConnection, project: str, project_id: int, now: datetime) -> int:
    """Posts a script claimed and never reported are failed (the platform may hold them: a person looks before posting
    again); posts no script claimed in a day are drafts. How many it ended."""
    t = tables.marketing_posts.c
    ended = 0
    found = await conn.execute(
        select(*store.post_columns()).where(
            t.project_id == project_id,
            ((t.state == "posting") & (t.claimed_at < now - STUCK))
            | ((t.state == "queued") & (t.decided_at < now - UNCLAIMED)),
        )
    )
    for post in list(found):
        channel = await store.channel(conn, project_id, post.channel_id)
        if post.state == "queued":
            await finish(
                conn, project_id, post, "draft", "no worker claimed it within a day: a person posts it", post.layers
            )
            ended += 1
            continue
        message = "the script reported nothing in 30 minutes: look at the platform before posting it again"
        await finish(
            conn, project_id, post, "failed", message, {**post.layers, "adapter": {"ok": False, "detail": message}}
        )
        await breaker.after_failure(conn, project_id, project, channel, "failed", message)
        ended += 1
    return ended
