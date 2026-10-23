"""The tools of the plugin marketing for its agents (criterion a16 of plan autonomous-oss-growth): ``marketing__propose``
takes a post through the gate, at once or at its time; ``marketing__draft`` keeps a draft a person posts;
``marketing__personal`` turns content for a member's personal channel into a proposal for the hub project omelet-blog.

Only the agent of a run of @marketer (this plugin) or @community (plugin crm) in the project calls them, as the
member the agent type runs as, who needs the writer role. No tool changes a channel's mode, the kill switch, a Goal or
the Policy, and none approves a baseline: those are a person's. A personal account never replies, and posts only when
its owner made it autonomous on facebook_profile or tiktok (``flow.personal_guard``); otherwise its text is a draft.

The proposal for omelet-blog follows section 9 of the report "Tăng trưởng do agent chạy": the agent sends it with
``chat_message`` to the group chat ``growth-proposals``, in Vietnamese, down to the mechanism and with measured numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import campaign, flow, settings, store
from evo_agents.core_plugins.marketing.models import MarketingPostIn
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.plugin_calls import CoreContext, enabled_in
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger(__name__)

CALLERS = (("marketing", "marketer"), ("crm", "community"))  # (plugin, agent type) whose runs call the tools
BLOG_PROJECT = "omelet-blog"
BLOG_CHAT = "growth-proposals"
RUNTIME: flow.Runtime | None = None  # the gate's runtime; None is the real Judge, transport and clock (tests set it)


@dataclass(frozen=True)
class Caller:
    """The run a tool is called from and the member it acts as."""

    run_id: int
    user_id: int
    project_id: int
    project: str


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


async def _typed(conn: AsyncConnection, project_id: int, profile_id: int | None) -> bool:
    """Whether ``profile_id`` is the profile of an enabled @marketer or @community of the project."""
    # Imported here: agent_types goes round a cycle of the server's modules when a tool's module loads first.
    from evo_agents.hub.server.agent_types import enabled_type

    for plugin, name in CALLERS:
        row = await enabled_in(conn, plugin, project_id)
        kind = await enabled_type(conn, project_id, row.id, name) if row is not None else None
        if kind is not None and kind.disabled_at is None and kind.profile_id == profile_id:
            return True
    return False


async def caller(conn: AsyncConnection, context: CoreContext) -> Caller:
    """The run of @marketer or @community the tool is called from; 403 for any other caller, or for a member without
    the writer role now."""
    refused = store.MarketingError(403, "only the agent of a run of @marketer or @community posts through these tools")
    if context.run_id is None or context.project_id is None:
        raise refused
    r = tables.runs.c
    run = (
        await conn.execute(select(r.project_id, r.profile_id, r.dispatched_by).where(r.id == context.run_id))
    ).first()
    if run is None or run.project_id != context.project_id or not await _typed(conn, run.project_id, run.profile_id):
        raise refused
    login = await conn.scalar(select(tables.users.c.login).where(tables.users.c.id == run.dispatched_by))
    try:
        access = await project_access(
            conn, Principal(run.dispatched_by, login, False, None, MACHINE, ""), context.project
        )
    except HTTPException:
        raise refused from None
    if not has_role(access.role, "writer"):
        raise store.MarketingError(403, f"{login} needs the writer role on project {context.project} to post")
    return Caller(context.run_id, run.dispatched_by, run.project_id, context.project)


async def _channel(conn: AsyncConnection, who: Caller, channel_id: int):
    row = await store.channel(conn, who.project_id, channel_id)
    if row is None:
        raise store.MarketingError(404, f"no channel {channel_id} in project {who.project}")
    return row


def _body(arguments: dict, who: Caller) -> MarketingPostIn:
    fields = {key: value for key, value in arguments.items() if key in MarketingPostIn.model_fields}
    return MarketingPostIn(**{**fields, "run_id": who.run_id})


def _shown(post) -> str:
    when = f", scheduled at {post.scheduled_at.isoformat()}" if post.scheduled_at and post.released_at is None else ""
    reason = f": {post.reason}" if post.reason else ""
    return f"post {post.id} on {post.channel} {post.account} is {post.state}{when}{reason}"


async def _gate(conn: AsyncConnection, context: CoreContext, who: Caller, body: MarketingPostIn, row) -> str:
    values = await campaign.values_of(conn, who.project_id, who.user_id, body, row)
    cfg = await settings.config(conn, context.config, who.project)
    post_id = await campaign.submit(conn, RUNTIME or flow.Runtime(), cfg, who.project, who.project_id, values)
    return _shown(await store.post(conn, post_id))


async def _draft(conn: AsyncConnection, who: Caller, body: MarketingPostIn, row, reason: str) -> str:
    values = await campaign.values_of(conn, who.project_id, who.user_id, body, row)
    post_id = await store.add_post(conn, **{**values, "scheduled_at": None}, state="draft", reason=reason)
    return _shown(await store.post(conn, post_id))


async def _call(context: CoreContext, work) -> dict:
    try:
        async with context.engine.begin() as conn:
            return _text(await work(conn, await caller(conn, context)))
    except store.MarketingError as exc:
        return _text(f"error: {exc.message}", error=True)
    except ValidationError as exc:
        return _text(f"error: the arguments do not read: {exc.errors(include_url=False)}", error=True)


async def propose(params: dict, context: CoreContext) -> dict:
    """``marketing__propose``: a post or a reply through the gate, at once or at ``scheduled_at``."""
    arguments = params.get("arguments") or {}

    async def work(conn, who: Caller) -> str:
        body = _body(arguments, who)
        row = await _channel(conn, who, body.channel_id)
        refused = flow.personal_guard(row, body.kind)
        if refused:
            raise store.MarketingError(403, f"{refused}; use marketing__personal for a member's own channel")
        return await _gate(conn, context, who, body, row)

    return await _call(context, work)


async def draft(params: dict, context: CoreContext) -> dict:
    """``marketing__draft``: a draft a person posts; it goes through no gate and posts nothing."""
    arguments = params.get("arguments") or {}

    async def work(conn, who: Caller) -> str:
        body = _body(arguments, who)
        row = await _channel(conn, who, body.channel_id)
        return await _draft(conn, who, body, row, "saved as a draft: a person posts it")

    return await _call(context, work)


def blog_proposal(arguments: dict, channel: str, sources: list[str]) -> str:
    """The proposal for omelet-blog in the shape of section 9 of the report, in Vietnamese."""
    return "\n".join(
        [
            f"Đề xuất bài: {arguments.get('topic')}",
            f"Lý do: {arguments.get('reason')}",
            f"Kênh đích: {channel}",
            f"Nguồn dùng được: {', '.join(sources) or 'chưa có'}",
            f"Hạn: {arguments.get('deadline')}",
        ]
    )


async def personal(params: dict, context: CoreContext) -> dict:
    """``marketing__personal``: content for a member's personal channel. It goes through the gate when its owner made
    the account autonomous on facebook_profile or tiktok, and is a draft otherwise; either way the answer holds the
    proposal to send to omelet-blog."""
    arguments = params.get("arguments") or {}

    async def work(conn, who: Caller) -> str:
        body = _body({**arguments, "kind": "post", "reply_to": None}, who)
        row = await _channel(conn, who, body.channel_id)
        if row.account_kind != "personal":
            raise store.MarketingError(422, "marketing__personal is for a member's personal account")
        refused = flow.personal_guard(row, "post")
        done = await _draft(conn, who, body, row, refused) if refused else await _gate(conn, context, who, body, row)
        sources = [source.url for source in body.sources] + list(body.links)
        proposal = blog_proposal(arguments, f"{row.channel} {row.account}", sources)
        return (
            f"{done}\n\nSend this proposal with chat_message to the group chat {BLOG_CHAT} of hub project "
            f"{BLOG_PROJECT}, as it is:\n\n{proposal}"
        )

    return await _call(context, work)


__all__ = ["BLOG_CHAT", "BLOG_PROJECT", "blog_proposal", "caller", "draft", "personal", "propose"]
