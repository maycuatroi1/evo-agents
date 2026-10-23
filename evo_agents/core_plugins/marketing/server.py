"""The routes of the plugin marketing, under /v1/x/marketing/<project>/ for a member with a grant on a project that
enabled it (the hub checks both first, ``evo_agents.hub.server.plugin_routes``; a project that did not enable it answers
404).

A member with a grant reads the channels and the status and sets the kill switch (``POST /kill``). A writer reads the
posts and the deny-list, proposes a post through the gate (``POST /posts``), saves a draft (``POST /drafts``), reports a
post (``POST /posts/{id}/report``, which opens the channel's breaker) and opens a breaker by hand. An admin makes a
project account, switches its mode, edits the deny-list and closes a breaker or the kill switch (``resume``). A
personal account is made by its owner, as draft_only, and only the owner switches it to autonomous, on facebook_profile
and tiktok; no request made with the scope of an agent's run changes a mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request, Response
from sqlalchemy import select

from evo_agents.core_plugins.marketing import (
    breaker,
    browser,
    budget,
    campaign,
    flow,
    registry,
    settings,
    store,
    weekly,
)
from evo_agents.core_plugins.marketing.models import (
    MarketingBrowserClaim,
    MarketingBrowserJob,
    MarketingBrowserResult,
    MarketingBrowserResultIn,
    MarketingBudget,
    MarketingChannel,
    MarketingChannelIn,
    MarketingDeny,
    MarketingDenyIn,
    MarketingDone,
    MarketingModeIn,
    MarketingPost,
    MarketingPostIn,
    MarketingReasonIn,
    MarketingSessionIn,
    MarketingStatus,
    MarketingWeekly,
)
from evo_agents.hub import tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.tables_marketing import POST_STATES

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
ChannelId = Annotated[int, Path(ge=1)]
PostId = Annotated[int, Path(ge=1)]

router = APIRouter(tags=["plugins"], responses={401: {"model": ErrorBody}, **REFUSALS})


async def _answer(request: Request, work):
    """``work(conn)`` in one transaction; a refusal as the hub's error body, nothing written."""
    try:
        async with request.app.state.engine.begin() as conn:
            return await work(conn)
    except store.MarketingError as exc:
        return error_response(request, exc.status, exc.message)
    except registry.RegistryError as exc:
        return error_response(request, 422, str(exc))


def _runtime(request: Request) -> flow.Runtime:
    return getattr(request.app.state, "marketing_runtime", None) or flow.Runtime()


async def _logins(conn, ids) -> dict[int, str]:
    u = tables.users.c
    found = await conn.execute(select(u.id, u.login).where(u.id.in_(list(ids) or [0])))
    return {row.id: row.login for row in found}


def _channel(row, logins: dict) -> MarketingChannel:
    return MarketingChannel(
        id=row.id,
        channel=row.channel,
        account=row.account,
        kind=row.account_kind,
        mode=row.mode,
        adapter=row.adapter,
        owner=logins.get(row.owner_id),
        target=row.target,
        paused=row.paused_at is not None,
        paused_reason=row.paused_reason,
        autonomous_since=row.autonomous_since,
        session_state=row.session_state,
    )


def _post(row) -> MarketingPost:
    return MarketingPost(
        id=row.id,
        channel_id=row.channel_id,
        channel=row.channel,
        account=row.account,
        kind=row.kind,
        body=row.body,
        links=row.links,
        state=row.state,
        reason=row.reason,
        layers=row.layers,
        judge=row.judge,
        run_id=row.run_id,
        posted_url=row.posted_url,
        cost_usd=float(row.cost_usd),
        dry_run=row.dry_run,
        created_at=row.created_at,
        posted_at=row.posted_at,
        claim_id=row.claim_id,
        signal_key=row.signal_key,
        scheduled_at=row.scheduled_at,
    )


async def _found(conn, access: ProjectAccess, channel_id: int, *, lock: bool = False):
    row = await store.channel(conn, access.project_id, channel_id, lock=lock)
    if row is None:
        raise store.MarketingError(
            404, f"no channel {channel_id} in project {access.name}: see `evo-agents hub marketing channels`"
        )
    return row


async def _audit(conn, user, access: ProjectAccess, action: str, target: str) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=access.project_id
    )


# Channels


@router.get("/{project}/channels", response_model=list[MarketingChannel])
async def list_channels(request: Request, project: ProjectName, user: CurrentUser):
    """The channel accounts of the project, with their mode and whether their breaker is open."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.reader(access)
        rows = await store.channels(conn, access.project_id)
        logins = await _logins(conn, {r.owner_id for r in rows if r.owner_id})
        return [_channel(r, logins) for r in rows]

    return await _answer(request, work)


@router.post("/{project}/channels", status_code=201, response_model=MarketingChannel)
async def add_channel(request: Request, project: ProjectName, body: MarketingChannelIn, user: CurrentUser):
    """Make a channel account, always draft_only. An admin makes a project account; any writer makes a personal one,
    which belongs to them. The credential is not here: it is a secret of the plugin's config."""

    async def work(conn):
        access = await project_access(conn, user, project)
        if body.kind == "personal":
            store.writer(access, "making a personal account")
        else:
            store.admin(access, "making a project account")
        adapter = registry.check_account(body.channel, body.kind, body.adapter)
        owner = user.user_id if body.kind == "personal" else None
        made = await store.add_channel(
            conn,
            access.project_id,
            channel=body.channel,
            account=body.account,
            account_kind=body.kind,
            mode="draft_only",
            adapter=adapter,
            owner_id=owner,
            target=body.target,
            created_by=user.user_id,
        )
        if made is None:
            raise store.MarketingError(
                409, f"project {project} has an account {body.account} on {body.channel} already"
            )
        await _audit(conn, user, access, "marketing.channel.add", f"channel:{made}")
        return _channel(await store.channel(conn, access.project_id, made), {owner: user.login} if owner else {})

    return await _answer(request, work)


def _may_switch(access: ProjectAccess, user, row, mode: str) -> None:
    """The rule of a mode change; MarketingError when this caller may not make it."""
    if user.scope is not None:
        raise store.MarketingError(403, "an agent cannot change a mode: a member does, in the web or the CLI")
    if row.account_kind == "personal":
        if user.user_id != row.owner_id and not (mode == "draft_only" and store_admin(access)):
            raise store.MarketingError(403, "only the member who owns a personal account switches it to autonomous")
        return
    store.admin(access, "changing the mode of a project account")


def store_admin(access: ProjectAccess) -> bool:
    try:
        store.admin(access, "x")
    except store.MarketingError:
        return False
    return True


@router.put("/{project}/channels/{channel_id}/mode", response_model=MarketingChannel)
async def set_mode(
    request: Request, project: ProjectName, channel_id: ChannelId, body: MarketingModeIn, user: CurrentUser
):
    """Switch an account between draft_only and autonomous. An agent never does; a personal account goes autonomous only
    for its owner and only on facebook_profile and tiktok; hackernews is always draft_only."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.reader(access)
        row = await _found(conn, access, channel_id, lock=True)
        _may_switch(access, user, row, body.mode)
        if body.mode == "autonomous" and not registry.may_be_autonomous(row.channel, row.account_kind):
            raise store.MarketingError(
                422, f"{row.account_kind} accounts on {row.channel} are draft_only: a person posts their drafts"
            )
        since = datetime.now(UTC) if body.mode == "autonomous" and row.mode != "autonomous" else row.autonomous_since
        await store.update_channel(
            conn, row.id, mode=body.mode, autonomous_since=since if body.mode == "autonomous" else None
        )
        await _audit(conn, user, access, f"marketing.channel.{body.mode}", f"channel:{row.id}")
        fresh = await store.channel(conn, access.project_id, row.id)
        return _channel(fresh, await _logins(conn, {fresh.owner_id} if fresh.owner_id else set()))

    return await _answer(request, work)


@router.post("/{project}/channels/{channel_id}/pause", response_model=MarketingDone)
async def pause_channel(
    request: Request, project: ProjectName, channel_id: ChannelId, body: MarketingReasonIn, user: CurrentUser
):
    """Open the breaker of a channel by hand: it posts nothing until an admin resumes it."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "pausing a channel")
        row = await _found(conn, access, channel_id)
        return MarketingDone(ok=await breaker.trip(conn, access.project_id, project, row, body.reason, by=user.user_id))

    return await _answer(request, work)


@router.post("/{project}/channels/{channel_id}/resume", response_model=MarketingDone)
async def resume_channel(request: Request, project: ProjectName, channel_id: ChannelId, user: CurrentUser):
    """Close the breaker of a channel; only an admin does, and nothing closes it by itself."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.admin(access, "resuming a channel")
        row = await _found(conn, access, channel_id)
        return MarketingDone(ok=await breaker.close(conn, access.project_id, row, user.user_id, user.token_id))

    return await _answer(request, work)


@router.post("/{project}/channels/{channel_id}/session", response_model=MarketingChannel)
async def set_session(
    request: Request, project: ProjectName, channel_id: ChannelId, body: MarketingSessionIn, user: CurrentUser
):
    """Say whether the login session of a browser channel works: ``ok`` after its owner logged in
    (``evo-agents hub marketing login``), which does not close a breaker; ``stale`` when it is known gone. The owner of a
    personal account or an admin; never the scope of an agent's run."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.reader(access)
        _no_agent(user, "the login session of a channel")
        row = await _found(conn, access, channel_id, lock=True)
        if row.adapter != "browser":
            raise store.MarketingError(422, f"channel {channel_id} posts through its API: it has no browser session")
        if not (user.user_id == row.owner_id or store_admin(access)):
            raise store.MarketingError(403, "only the owner of the account or an admin says its session works")
        await store.update_channel(conn, row.id, session_state=body.state)
        await _audit(conn, user, access, f"marketing.session.{body.state}", f"channel:{row.id}")
        fresh = await store.channel(conn, access.project_id, row.id)
        return _channel(fresh, await _logins(conn, {fresh.owner_id} if fresh.owner_id else set()))

    return await _answer(request, work)


# The kill switch and the status


@router.post("/{project}/kill", response_model=MarketingDone)
async def kill(request: Request, project: ProjectName, body: MarketingReasonIn, user: CurrentUser):
    """Stop every channel of the project at once. Any member with a grant may."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.reader(access)
        return MarketingDone(
            ok=await breaker.kill(conn, access.project_id, project, user.user_id, body.reason, token_id=user.token_id)
        )

    return await _answer(request, work)


@router.post("/{project}/resume", response_model=MarketingDone)
async def resume(request: Request, project: ProjectName, user: CurrentUser):
    """Clear the kill switch; only an admin does. The breakers that are open stay open."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.admin(access, "clearing the kill switch")
        return MarketingDone(ok=await breaker.unkill(conn, access.project_id, project, user.user_id, user.token_id))

    return await _answer(request, work)


@router.get("/{project}/status", response_model=MarketingStatus)
async def status(request: Request, project: ProjectName, user: CurrentUser):
    """The kill switch, the breakers and this month's budget."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.reader(access)
        cfg = await settings.config(conn, request.app.state.config, project)
        month = budget.month_of(datetime.now(UTC))
        reserved, spent = await budget.status(conn, access.project_id, month)
        rows = await store.channels(conn, access.project_id)
        stopped = await store.killed(conn, access.project_id)
        return MarketingStatus(
            project=project,
            killed=stopped is not None,
            reason=stopped.reason if stopped else None,
            channels=len(rows),
            open_breakers=sum(1 for r in rows if r.paused_at is not None),
            budget=MarketingBudget(
                month=month, budget_usd=cfg.budget_usd, reserved_usd=float(reserved), spent_usd=float(spent)
            ),
        )

    return await _answer(request, work)


@router.get("/{project}/weekly", response_model=MarketingWeekly)
async def show_weekly(request: Request, project: ProjectName, user: CurrentUser):
    """The Growth section of the weekly bulletin: the 7 days up to now."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.reader(access)
        cfg = await settings.config(conn, request.app.state.config, project)
        return await weekly.read(conn, cfg, project, access.project_id, datetime.now(UTC))

    return await _answer(request, work)


# Posts


@router.get("/{project}/posts", response_model=list[MarketingPost])
async def list_posts(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[Literal[*POST_STATES] | None, Query(description="only posts in this state")] = None,
):
    """The posts of the project, newest first, with what each layer of the gate said."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "reading the posts")
        return [_post(r) for r in await store.posts(conn, access.project_id, state=state)]

    return await _answer(request, work)


@router.get("/{project}/posts/{post_id}", response_model=MarketingPost)
async def show_post(request: Request, project: ProjectName, post_id: PostId, user: CurrentUser):
    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "reading a post")
        row = await store.post(conn, post_id, project_id=access.project_id)
        if row is None:
            raise store.MarketingError(404, f"no post {post_id} in project {project}")
        return _post(row)

    return await _answer(request, work)


@router.post("/{project}/posts", status_code=201, response_model=MarketingPost)
async def propose_post(request: Request, project: ProjectName, body: MarketingPostIn, user: CurrentUser):
    """Propose a post to the gate: it is checked in order and goes out when every layer passes, with no human. A post
    command to a personal account that is draft_only, or on a channel a personal account never posts on by itself, is
    refused here (403); save a draft instead."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "proposing a post")
        row = await _found(conn, access, body.channel_id)
        refused = flow.personal_guard(row, body.kind)
        if refused:
            raise store.MarketingError(403, refused)
        values = await campaign.values_of(conn, access.project_id, user.user_id, body, row)
        cfg = await settings.config(conn, request.app.state.config, project)
        post_id = await campaign.submit(conn, _runtime(request), cfg, project, access.project_id, values)
        return _post(await store.post(conn, post_id))

    return await _answer(request, work)


@router.post("/{project}/drafts", status_code=201, response_model=MarketingPost)
async def save_draft(request: Request, project: ProjectName, body: MarketingPostIn, user: CurrentUser):
    """Save a draft that goes through no gate and posts nothing: a person posts it. The only way an agent leaves a text
    for a personal account."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "saving a draft")
        row = await _found(conn, access, body.channel_id)
        values = await campaign.values_of(conn, access.project_id, user.user_id, body, row)
        values = {**values, "scheduled_at": None}
        post_id = await store.add_post(conn, **values, state="draft", reason="saved as a draft: a person posts it")
        await _audit(conn, user, access, "marketing.post.draft", f"post:{post_id}")
        return _post(await store.post(conn, post_id))

    return await _answer(request, work)


@router.post("/{project}/posts/{post_id}/report", response_model=MarketingDone)
async def report_post(
    request: Request, project: ProjectName, post_id: PostId, body: MarketingReasonIn, user: CurrentUser
):
    """A report or complaint about a post that went out: the breaker of its channel opens at once."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "reporting a post")
        row = await store.post(conn, post_id, project_id=access.project_id)
        if row is None:
            raise store.MarketingError(404, f"no post {post_id} in project {project}")
        channel = await _found(conn, access, row.channel_id)
        opened = await breaker.trip(
            conn, access.project_id, project, channel, f"a report on post {post_id}: {body.reason}", by=user.user_id
        )
        return MarketingDone(ok=opened)

    return await _answer(request, work)


# The script on a worker with a browser


def _no_agent(user, what: str) -> None:
    if user.scope is not None:
        raise store.MarketingError(403, f"{what} is not for an agent: only a login's script reaches a browser session")


@router.post("/{project}/browser/claim", response_model=MarketingBrowserClaim)
async def claim_browser_post(request: Request, project: ProjectName, user: CurrentUser):
    """The next post the gate passed for a browser channel, with the sealed session of its account, for the script of
    ``evo-agents hub marketing browser-post``. Needs the writer role and a login: the scope of an agent's run gets 403."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "posting through the browser")
        _no_agent(user, "claiming a post for the browser")
        cfg = await settings.config(conn, request.app.state.config, project)
        found = await browser.claim(conn, cfg, access.project_id, _runtime(request).now())
        return MarketingBrowserClaim(job=MarketingBrowserJob(**found) if found else None)

    return await _answer(request, work)


@router.post("/{project}/browser/{post_id}/result", response_model=MarketingBrowserResult)
async def report_browser_post(
    request: Request, project: ProjectName, post_id: PostId, body: MarketingBrowserResultIn, user: CurrentUser
):
    """What the script found: the link and a screenshot of the post, or the captcha, lock or login page it stopped at,
    which opens the breaker of the channel."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "reporting a browser post")
        _no_agent(user, "reporting a browser post")
        done = await browser.report(
            conn,
            project,
            access.project_id,
            post_id,
            body.outcome,
            body.url,
            body.detail,
            body.screenshot,
            _runtime(request).now(),
        )
        await _audit(conn, user, access, "marketing.browser.report", f"post:{post_id}")
        return MarketingBrowserResult(**done)

    return await _answer(request, work)


@router.get(
    "/{project}/posts/{post_id}/screenshot", response_class=Response, responses={200: {"content": {"image/png": {}}}}
)
async def show_screenshot(request: Request, project: ProjectName, post_id: PostId, user: CurrentUser):
    """The PNG the script took after the post, or where it stopped (labelled internal in the audit)."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "reading a screenshot")
        picture = await store.screenshot(conn, post_id, access.project_id)
        if picture is None:
            raise store.MarketingError(404, f"post {post_id} has no screenshot")
        return Response(bytes(picture), media_type="image/png", headers={"Cache-Control": "no-store"})

    return await _answer(request, work)


# The deny-list


@router.get("/{project}/denylist", response_model=list[MarketingDeny])
async def list_denylist(request: Request, project: ProjectName, user: CurrentUser):
    async def work(conn):
        access = await project_access(conn, user, project)
        store.writer(access, "reading the deny-list")
        return [
            MarketingDeny(id=r.id, kind=r.kind, value=r.value, added_at=r.added_at)
            for r in await store.denylist(conn, access.project_id)
        ]

    return await _answer(request, work)


@router.post("/{project}/denylist", status_code=201, response_model=MarketingDone)
async def add_deny(request: Request, project: ProjectName, body: MarketingDenyIn, user: CurrentUser):
    """Add a term, a domain or a person no post may name. Only an admin edits it; the audit holds the change."""

    async def work(conn):
        access = await project_access(conn, user, project)
        store.admin(access, "editing the deny-list")
        added = await store.add_deny(conn, access.project_id, body.kind, body.value.strip(), user.user_id)
        if added:
            await _audit(conn, user, access, "marketing.denylist.add", f"project:{access.project_id}")
        return MarketingDone(ok=added)

    return await _answer(request, work)


@router.delete("/{project}/denylist/{deny_id}", response_model=MarketingDone)
async def remove_deny(request: Request, project: ProjectName, deny_id: Annotated[int, Path(ge=1)], user: CurrentUser):
    async def work(conn):
        access = await project_access(conn, user, project)
        store.admin(access, "editing the deny-list")
        gone = await store.remove_deny(conn, access.project_id, deny_id)
        if gone:
            await _audit(conn, user, access, "marketing.denylist.remove", f"deny:{deny_id}")
        return MarketingDone(ok=gone)

    return await _answer(request, work)


__all__ = ["router"]
