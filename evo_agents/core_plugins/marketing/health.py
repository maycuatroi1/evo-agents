"""The health check of a channel account (plan autonomous-oss-growth, step 22): whether it could post now, found by a
read that posts nothing (``docs/marketing-plugin.md``, section Health).

An API account reads the platform once with its channel's credential (``health`` of its adapter: the repo and the push
right on GitHub, the user behind the credential on X, Reddit and LinkedIn, the page's tasks on Facebook, the creator
info on TikTok). A browser account, or an API one that posts through the browser for want of a credential, is ok when
its login session is. A channel no code posts on (hackernews, facebook_group) is ``n/a``. No credential is failing
with the reason "no credential". The credential goes into the adapter's request header only: a reason is the adapter's
words or the name of an error, never the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import adapters, browser, registry, store
from evo_agents.core_plugins.marketing.settings import Config


@dataclass(frozen=True)
class Health:
    state: str  # ok, failing or n/a (tables_marketing.HEALTH_STATES)
    reason: str


def _session(channel) -> Health:
    if channel.session_state == "ok":
        return Health("ok", "the login session works")
    if channel.session_state == "stale":
        return Health("failing", "the login session expired: its owner logs in again")
    return Health("failing", "no login session: its owner runs `evo-agents hub marketing login`")


async def check(cfg: Config, channel, transport: adapters.Transport | None = None) -> Health:
    """The health of one account, read through ``transport`` (the real one when none)."""
    if registry.spec(channel.channel).always_draft:
        return Health("n/a", f"{channel.channel} is draft_only always: a person posts its drafts")
    token = cfg.token(channel.channel)
    if channel.adapter == "browser" or (not token and browser.fallback(cfg, channel)):
        return _session(channel)
    adapter = adapters.for_channel(channel.channel, transport)
    if adapter is None:
        return Health("failing", f"no api adapter for {channel.channel}: use the browser adapter")
    if not token:
        return Health("failing", "no credential")
    try:
        return Health("ok", await adapter.health(channel.target, token))
    except adapters.AdapterError as exc:
        return Health("failing", exc.message)
    except Exception as exc:  # the network's error: its name only, as its words may hold the request
        return Health("failing", f"{channel.channel}: the platform did not answer ({type(exc).__name__})")


async def run(conn: AsyncConnection, cfg: Config, channel, now: datetime, transport=None) -> Health:
    """Check one account and keep the result on its row."""
    found = await check(cfg, channel, transport)
    await store.update_channel(
        conn, channel.id, health=found.state, health_reason=found.reason[:500], health_checked_at=now
    )
    return found
