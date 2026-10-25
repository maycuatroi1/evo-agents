"""The tools of the plugin marketing for its agents (criterion a16 of plan autonomous-oss-growth): ``marketing__propose``
takes a post through the gate, at once or at its time; ``marketing__draft`` keeps a draft a person posts;
``marketing__personal`` turns content for a member's personal channel into a proposal for the hub project the plugin's config names
in ``proposals_project`` (the project's blog).

Only the agent of a run of @marketer (this plugin) or @community (plugin crm) in the project calls them, as the
member the agent type runs as, who needs the writer role. No tool changes a channel's mode, the kill switch, a Goal or
the Policy, and none approves a baseline: those are a person's. A personal account never replies, and posts only when
its owner made it autonomous on facebook_profile or tiktok (``flow.personal_guard``); otherwise its text is a draft.

The proposal for the blog project follows section 9 of the report "Tăng trưởng do agent chạy": the agent sends it with
``chat_message`` to the group chat ``growth-proposals``, in Vietnamese, down to the mechanism and with measured numbers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import campaign, flow, settings, store
from evo_agents.core_plugins.marketing.models import MarketingPostIn
from evo_agents.hub.server import core_plugin_api as hub

log = logging.getLogger(__name__)

CALLERS = (("marketing", "marketer"), ("crm", "community"))  # (plugin, agent type) whose runs call the tools
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


async def _typed(conn: AsyncConnection, project: str, profile_id: int | None) -> bool:
    """Whether ``profile_id`` is the profile of an enabled @marketer or @community of the project."""
    found = await hub.read_agent_types(conn, store.handle(), project, profile_id=profile_id)
    return any(kind.enabled and (kind.plugin, kind.name) in CALLERS for kind in found)


async def caller(conn: AsyncConnection, context: hub.Context) -> Caller:
    """The run of @marketer or @community the tool is called from; 403 for any other caller, or for a member without
    the writer role now."""
    refused = store.MarketingError(403, "only the agent of a run of @marketer or @community posts through these tools")
    if context.run_id is None or context.project_id is None:
        raise refused
    try:
        run = await hub.read_run(conn, store.handle(), context.run_id)
        if run is None or run.project != context.project or not await _typed(conn, run.project, run.profile_id):
            raise refused
        member = await hub.run_member(conn, store.handle(), run.project, run.id)
        if member is None:
            raise refused
        access = await hub.project_access(conn, member, context.project)
    except hub.Refused:
        raise refused from None
    if access.id != context.project_id:
        raise refused
    if not hub.has_role(access.role, "writer"):
        raise store.MarketingError(403, f"{member.login} needs the writer role on project {context.project} to post")
    return Caller(context.run_id, member.user_id, access.id, context.project)


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


async def _gate(conn: AsyncConnection, context: hub.Context, who: Caller, body: MarketingPostIn, row) -> str:
    values = await campaign.values_of(conn, who.project, who.project_id, who.user_id, body, row)
    cfg = await settings.config(conn, getattr(context, "config", None), who.project)
    post_id = await campaign.submit(conn, RUNTIME or flow.Runtime(), cfg, who.project, who.project_id, values)
    return _shown(await store.post(conn, post_id))


async def _draft(conn: AsyncConnection, who: Caller, body: MarketingPostIn, row, reason: str) -> str:
    values = await campaign.values_of(conn, who.project, who.project_id, who.user_id, body, row)
    post_id = await store.add_post(conn, **{**values, "scheduled_at": None}, state="draft", reason=reason)
    return _shown(await store.post(conn, post_id))


async def _call(context: hub.Context, work) -> dict:
    try:
        async with hub.transaction(context) as conn:
            return _text(await work(conn, await caller(conn, context)))
    except store.MarketingError as exc:
        return _text(f"error: {exc.message}", error=True)
    except ValidationError as exc:
        return _text(f"error: the arguments do not read: {exc.errors(include_url=False)}", error=True)


async def propose(params: dict, context: hub.Context) -> dict:
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


async def draft(params: dict, context: hub.Context) -> dict:
    """``marketing__draft``: a draft a person posts; it goes through no gate and posts nothing."""
    arguments = params.get("arguments") or {}

    async def work(conn, who: Caller) -> str:
        body = _body(arguments, who)
        row = await _channel(conn, who, body.channel_id)
        return await _draft(conn, who, body, row, "saved as a draft: a person posts it")

    return await _call(context, work)


def blog_proposal(arguments: dict, channel: str, sources: list[str]) -> str:
    """The proposal for the blog project in the shape of section 9 of the report, in Vietnamese."""
    return "\n".join(
        [
            f"Đề xuất bài: {arguments.get('topic')}",
            f"Lý do: {arguments.get('reason')}",
            f"Kênh đích: {channel}",
            f"Nguồn dùng được: {', '.join(sources) or 'chưa có'}",
            f"Hạn: {arguments.get('deadline')}",
        ]
    )


async def personal(params: dict, context: hub.Context) -> dict:
    """``marketing__personal``: content for a member's personal channel. It goes through the gate when its owner made
    the account autonomous on facebook_profile or tiktok, and is a draft otherwise; either way the answer holds the
    proposal to send to the blog project (config ``proposals_project``)."""
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
        blog = (await settings.config(conn, getattr(context, "config", None), who.project)).proposals_project
        if not blog:
            return f"{done}\n\nNo blog project is set (config proposals_project); keep this proposal:\n\n{proposal}"
        return (
            f"{done}\n\nSend this proposal with chat_message to the group chat {BLOG_CHAT} of hub project "
            f"{blog}, as it is:\n\n{proposal}"
        )

    return await _call(context, work)


__all__ = ["BLOG_CHAT", "blog_proposal", "caller", "draft", "personal", "propose"]
