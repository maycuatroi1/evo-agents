"""The posting gate end to end: what happens to a post from the moment it is proposed (``docs/marketing-plugin.md``).

``decide`` moves one post through the layers in a fixed order and stops at the first that fails, which makes the post a
draft (or, for the Judge, withdrawn) with the reason:

1. the switches: the kill switch of the project, the breaker of the channel and the mode of the account;
2. the project's deny-list; 3. the source rule; 4. the channel's caps, minimum gap and self-promotion rules (``gate``);
5. the Judge in a fresh context (``judge``), which may answer later: the post waits as ``judging``;
6. the dry run of a channel that went autonomous lately; 7. the month's budget (``budget``);
then the adapter of the channel posts it: an API adapter at once, a browser channel through the queue of the worker's
script (``browser``). A post that passes needs no human. Every layer's result, the Judge's scores,
the cost and the run that proposed it stay on the post (the audit), and each outcome writes an audit row.

``decide`` is safe to call again: it does nothing for a post that is decided, and a post that waits for the Judge goes on
from the Judge's answer. The runtime gives it the Judge and the transport; a test passes fakes, the job passes none.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import adapters, breaker, browser, budget, gate, judge, registry, store
from evo_agents.core_plugins.marketing.outcome import finish as _finish
from evo_agents.core_plugins.marketing.settings import Config
from evo_agents.core_plugins.marketing.tables import PERSONAL_AUTONOMOUS

log = logging.getLogger(__name__)

JudgeFn = Callable[[AsyncConnection, str, object, object], Awaitable[dict | None]]
MONTH_DAYS = 30


@dataclass
class Runtime:
    """What a pass of the gate is given: a Judge that answers at once (None asks the project's judge agent), the
    transport the API adapters call out with (None is the real one) and the clock."""

    judge: JudgeFn | None = None
    transport: adapters.Transport | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def personal_guard(channel, kind: str = "post") -> str | None:
    """Why the code refuses a post command to this account, or None: a personal account that is draft_only or on a
    channel that may not post by itself, and any reply from a personal account."""
    if channel.account_kind != "personal":
        return None
    if channel.channel not in PERSONAL_AUTONOMOUS:
        return f"a personal account on {channel.channel} never posts by itself: a person posts its drafts"
    if kind == "reply":
        return "a personal account posts original posts only, never a reply"
    if channel.mode != "autonomous":
        return "this personal account is draft_only: its owner switches it, and an agent drafts for them"
    return None


def _switches(channel, killed) -> gate.Layer:
    if killed is not None:
        return gate.Layer(
            "switches", False, f"the kill switch of the project is set: {killed.reason or 'no reason given'}"
        )
    if channel.paused_at is not None:
        return gate.Layer(
            "switches", False, f"the breaker of the channel is open: {channel.paused_reason or 'no reason'}"
        )
    if channel.mode != "autonomous":
        return gate.Layer("switches", False, "the account is draft_only: a person posts it")
    return gate.Layer("switches", True)


async def _deterministic(
    conn: AsyncConnection, project_id: int, post, channel, cfg: Config, now: datetime
) -> list[gate.Layer]:
    """The layers the gate decides without a model, up to the first that fails."""
    killed = await store.killed(conn, project_id)
    layers = [_switches(channel, killed)]
    if not layers[-1].ok:
        return layers
    layers.append(gate.denylist(post.body, post.links, await store.denylist(conn, project_id)))
    if layers[-1].ok:
        layers.append(gate.source_rule(post.body, post.sources))
    if layers[-1].ok:
        rules = registry.rules_for(channel.channel, cfg.rules)
        done = await store.posted_since(conn, channel.id, now - timedelta(days=MONTH_DAYS))
        promo = sum(1 for p in done if p.kind == "post")
        layers.append(
            gate.channel_rules(
                post.body, post.links, post.kind, rules, done, now, promo=promo, replies=len(done) - promo
            )
        )
    return layers


async def _verdict(conn: AsyncConnection, rt: Runtime, project: str, post, channel) -> judge.Verdict | None:
    """The Judge's verdict, None while it has not answered (the question id is kept on the post)."""
    if rt.judge is not None:
        axes = await rt.judge(conn, project, post, channel)
        return None if axes is None else judge.verdict_of(axes)
    if post.question_id is None:
        await store.save_post(
            conn, post.id, question_id=await judge.ask(conn, project, post.id, post.body, post.links, channel.channel)
        )
        return None
    return await judge.answered(conn, post.question_id)


async def _judged(conn, rt, project_id, project, post, channel, layers: dict) -> str | None:
    """Run the Judge layer: a state when it ends the post (waiting, or not passed), None when the post may go on."""
    try:
        verdict = await _verdict(conn, rt, project, post, channel)
    except HTTPException as exc:  # no judge agent: fail closed, and say why
        layers["judge"] = {"ok": False, "detail": f"the Judge could not be asked: {exc.detail}"}
        return await _finish(conn, project_id, post, "draft", layers["judge"]["detail"], layers)
    if verdict is None:
        await store.save_post(conn, post.id, state="judging", layers=layers)
        return "judging"
    layers["judge"] = {"ok": verdict.ok, "detail": verdict.detail}
    if verdict.ok:
        await store.save_post(conn, post.id, judge=verdict.as_dict())
        return None
    state = await _finish(conn, project_id, post, "withdrawn", verdict.detail, layers, judge=verdict.as_dict())
    if verdict.near_line:
        await breaker.tell(
            conn, project_id, project, "Marketing: a post sat at 3 on one axis", f"post {post.id}: {verdict.detail}"
        )
    await breaker.after_judge(conn, project_id, project, channel)
    return state


def _dry(channel, cfg: Config, now: datetime) -> str | None:
    since = channel.autonomous_since
    if since is not None and now < since + timedelta(hours=cfg.dry_run_hours):
        return f"dry run until {(since + timedelta(hours=cfg.dry_run_hours)).isoformat(timespec='minutes')}"
    return None


async def decide(conn: AsyncConnection, rt: Runtime, cfg: Config, project: str, project_id: int, post_id: int) -> str:
    """Take one post as far as the gate lets it; its state after (``store.OPEN`` states are not decided)."""
    post = await store.post(conn, post_id, project_id=project_id, lock=True)
    if post is None or post.state not in store.OPEN:
        return post.state if post else "missing"
    channel = await store.channel(conn, project_id, post.channel_id)
    now = rt.now()
    layers = {}
    for layer in await _deterministic(conn, project_id, post, channel, cfg, now):
        layers[layer.name] = layer.as_dict()
        if not layer.ok:
            return await _finish(conn, project_id, post, "draft", layer.detail, layers)
    refused = personal_guard(channel, post.kind)
    if refused:
        layers["personal"] = {"ok": False, "detail": refused}
        return await _finish(conn, project_id, post, "draft", refused, layers)
    ended = await _judged(conn, rt, project_id, project, post, channel, layers)
    if ended:
        return ended
    return await _go(conn, rt, cfg, project, project_id, post, channel, layers, now)


async def _go(
    conn, rt: Runtime, cfg: Config, project: str, project_id: int, post, channel, layers: dict, now: datetime
) -> str:
    """The layers after the Judge: the dry run, the budget, then the adapter."""
    dry = _dry(channel, cfg, now)
    if dry:
        layers["dry_run"] = {"ok": False, "detail": dry}
        return await _finish(conn, project_id, post, "draft", dry, layers, dry_run=True)
    spec = registry.spec(channel.channel)
    month = budget.month_of(now)
    if not await budget.reserve(conn, project_id, month, spec.cost_usd, cfg.budget_usd):
        layers["budget"] = {"ok": False, "detail": f"the month's budget of {cfg.budget_usd:g} USD is spent"}
        return await _finish(conn, project_id, post, "draft", layers["budget"]["detail"], layers)
    layers["budget"] = {"ok": True, "detail": f"{spec.cost_usd:g} USD reserved"}
    return await _publish(conn, rt, cfg, project, project_id, post, channel, layers, spec.cost_usd, now)


async def _publish(
    conn,
    rt: Runtime,
    cfg: Config,
    project: str,
    project_id: int,
    post,
    channel,
    layers: dict,
    reserved: float,
    now: datetime,
) -> str:
    month = budget.month_of(now)
    if channel.adapter == "browser" or (not cfg.token(channel.channel) and browser.fallback(cfg, channel)):
        return await _queue(conn, cfg, project_id, post, channel, layers, reserved, month, now)
    adapter = adapters.for_channel(channel.channel, rt.transport)
    token = cfg.token(channel.channel)
    missing = (
        f"no api adapter for {channel.channel}: a person posts it"
        if adapter is None
        else None
        if token
        else f"no credential for {channel.channel} in the plugin's config"
    )
    if missing:
        await budget.settle(conn, project_id, month, reserved, 0)
        layers["adapter"] = {"ok": False, "detail": missing}
        return await _finish(conn, project_id, post, "draft", missing, layers)
    content = adapters.Content(post.body, channel.account, channel.target, post.reply_to, media_url=_media(post.links))
    try:
        sent = await adapter.publish(content, token)
    except Exception as exc:  # a platform's error, or the network's: nothing went out, so nothing was spent
        error = exc if isinstance(exc, adapters.AdapterError) else adapters.AdapterError("failed", type(exc).__name__)
        await budget.settle(conn, project_id, month, reserved, 0)
        return await _failed(conn, project_id, project, post, channel, layers, error)
    spent = await budget.settle(conn, project_id, month, reserved, sent.cost_usd)
    layers["adapter"] = {"ok": True, "detail": sent.url}
    state = await _finish(
        conn,
        project_id,
        post,
        "posted",
        None,
        layers,
        posted_url=sent.url,
        external_id=sent.external_id,
        cost_usd=sent.cost_usd,
        posted_at=now,
    )
    if spent > cfg.budget_usd:
        await breaker.kill(conn, project_id, project, None, "the month's spend passed the budget")
    return state


async def _queue(conn, cfg: Config, project_id: int, post, channel, layers: dict, reserved: float, month, now) -> str:
    """A post for a browser channel: it goes into the queue of the worker's script, or is a draft with the reason. A
    browser channel is free, so what the budget reserved goes back."""
    await budget.settle(conn, project_id, month, reserved, 0)
    why = await browser.not_ready(conn, cfg, post, channel)
    if why:
        layers["adapter"] = {"ok": False, "detail": why}
        return await _finish(conn, project_id, post, "draft", why, layers)
    layers["adapter"] = {"ok": True, "detail": "queued: a script on a worker with a browser posts it"}
    return await _finish(conn, project_id, post, "queued", None, layers)


async def _failed(
    conn, project_id: int, project: str, post, channel, layers: dict, error: adapters.AdapterError
) -> str:
    layers["adapter"] = {"ok": False, "detail": error.message}
    if error.kind == "missing":
        return await _finish(conn, project_id, post, "draft", error.message, layers)
    state = await _finish(conn, project_id, post, "failed", error.message, layers)
    await breaker.after_failure(conn, project_id, project, channel, error.kind, error.message)
    return state


def _media(links: list) -> str | None:
    return next(
        (url for url in links if str(url).lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".mp4"))), None
    )
