"""The tool ``crm__invite`` of @community (criterion a16 of plan autonomous-oss-growth, ``docs/crm-plugin.md``): a
one-time join link of the project bound to the GitHub login that asked for it in an invite-request issue.

Only the agent of a run of @community in the project calls it. Apart from a hub admin, it is the only maker of invite
links, and only when the Policy allows: the login asked with that request (an invite_request touchpoint of its contact),
no link was made for the request before, the project made fewer than ``invite_links_per_week`` links this way in the
last 7 days (3 by default), and the GitHub account is at least ``invite_min_github_age_days`` old (30 by default). The
link gives the reader role at the lowest level of the project's ladder, lasts 7 days and only that login can accept it.
It is made in the name of the member @community runs as, with the hub's audit row ``invite.create``.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import github, rules, settings, store
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext, enabled_in
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import project_ladder
from evo_agents.hub.server.invites import DEFAULT_DAYS, JOIN, PAGE, PREFIX, TOKEN_BYTES
from evo_agents.hub.server.security import hash_token

log = logging.getLogger(__name__)

COMMUNITY = "community"
ROLE = "reader"


class Refused(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Run:
    run_id: int
    user_id: int
    project_id: int


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


async def community_run(conn: AsyncConnection, context: CoreContext) -> Run:
    """The run of @community the tool is called from; Refused for any other caller."""
    # Imported here: agent_types goes round a cycle of the server's modules when a tool's module loads first.
    from evo_agents.hub.server.agent_types import enabled_type

    refused = Refused(f"only the agent of a run of @{COMMUNITY} in project {context.project} makes an invite link")
    if context.run_id is None or context.project_id is None:
        raise refused
    r = tables.runs.c
    run = (
        await conn.execute(select(r.project_id, r.profile_id, r.dispatched_by).where(r.id == context.run_id))
    ).first()
    plugin = await enabled_in(conn, store.PLUGIN, context.project_id)
    if run is None or plugin is None or run.project_id != context.project_id:
        raise refused
    kind = await enabled_type(conn, context.project_id, plugin.id, COMMUNITY)
    if kind is None or kind.disabled_at is not None or kind.profile_id != run.profile_id:
        raise refused
    return Run(context.run_id, run.dispatched_by, run.project_id)


async def _old_enough(login: str, days: int) -> None:
    fetcher = github.default_fetcher()
    try:
        created = await fetcher.account_created(login)
    except Exception as exc:  # GitHub is out of our hands: no link without its answer
        raise Refused("GitHub did not say how old the account is; try again later") from exc
    if created is None:
        raise Refused(f"GitHub has no account {login}")
    if datetime.now(UTC) - created < timedelta(days=days):
        raise Refused(f"the GitHub account {login} is younger than {days} days: a maintainer decides")


async def _allowed(conn: AsyncConnection, run: Run, cfg: settings.Config, login: str, ref: str) -> None:
    """Refused unless the Policy allows a link for ``login``'s request ``ref`` now."""
    if not await store.requested(conn, run.project_id, login, ref):
        raise Refused(f"{login} made no invite request {ref} in this project")
    if await store.sent_for(conn, run.project_id, ref):
        raise Refused(f"a link was made for {ref} already")
    if await store.sent_this_week(conn, run.project_id) >= cfg.invite_links_per_week:
        raise Refused(f"the project made {cfg.invite_links_per_week} links this week, its cap: a maintainer decides")
    await _old_enough(login, cfg.invite_min_github_age_days)


async def _make(conn: AsyncConnection, run: Run, project: str, login: str) -> tuple[int, str]:
    """A join link of the project for ``login``; (its id, its token)."""
    _, ladder = await project_ladder(conn, project)
    token = PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    i = tables.invites
    invite_id = await conn.scalar(
        insert(i)
        .values(
            kind=JOIN,
            project_id=run.project_id,
            role=ROLE,
            max_level=ladder[0],
            for_login=login,
            token_hash=hash_token(token),
            created_by=run.user_id,
            expires_at=func.now() + timedelta(days=DEFAULT_DAYS),
        )
        .returning(i.c.id)
    )
    target = f"invite:{invite_id} kind={JOIN} project={project} role={ROLE} max_level={ladder[0]} for={login}"
    await audit.record(
        conn, actor_id=run.user_id, token_id=None, action=audit.INVITE_CREATE, target=target, project_id=run.project_id
    )
    return invite_id, token


async def _invite(conn: AsyncConnection, context: CoreContext, arguments: dict) -> str:
    run = await community_run(conn, context)
    login, ref = rules.login_of(str(arguments.get("login") or "")), str(arguments.get("ref") or "")
    if login is None or not ref:
        raise Refused("give the requester's GitHub login and the request's ref (issue#<n>)")
    row = await enabled_in(conn, store.PLUGIN, run.project_id)
    cfg = await settings.config(conn, row, run.project_id)
    await _allowed(conn, run, cfg, login, ref)
    public = getattr(context.config, "public_url", None)
    if not public:
        raise Refused("the hub has no public URL (EVO_HUB_PUBLIC_URL), so a link cannot be given")
    invite_id, token = await _make(conn, run, context.project, login)
    await store.add_sent(conn, run.project_id, ref, login, invite_id, run.run_id)
    log.info("community invite made", extra={"invite": invite_id, "run_id": run.run_id})
    return (
        f"Invite link for {login}, valid {DEFAULT_DAYS} days, only their GitHub login can accept it: "
        f"{public}{PAGE}{token}\nPost it in {ref} as the answer; it is shown this once."
    )


async def invite(params: dict, context: CoreContext) -> dict:
    """``crm__invite``: a join link for the login that asked in invite request ``ref``, when the Policy allows."""
    try:
        async with context.engine.begin() as conn:
            return _text(await _invite(conn, context, params.get("arguments") or {}))
    except Refused as exc:
        return _text(f"refused: {exc.message}", error=True)


__all__ = ["COMMUNITY", "community_run", "invite"]
