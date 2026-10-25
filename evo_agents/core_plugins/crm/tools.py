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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import github, rules, settings, store
from evo_agents.hub.server import core_plugin_api as hub

log = logging.getLogger(__name__)

COMMUNITY = "community"
ROLE = "reader"
INVITE_DAYS = 7  # how long a link lasts: the hub's default for a join link


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


async def community_run(conn: AsyncConnection, context: hub.Context) -> Run:
    """The run of @community the tool is called from; Refused for any other caller."""
    refused = Refused(f"only the agent of a run of @{COMMUNITY} in project {context.project} makes an invite link")
    if context.run_id is None or context.project_id is None or context.project is None:
        raise refused
    plugin = store.handle()
    run = await hub.read_run(conn, plugin, context.run_id)
    if run is None or run.project != context.project:
        raise refused
    try:
        project = await hub.read_project(conn, plugin, context.project)
        kind = await hub.enabled_agent_type(conn, plugin, context.project, COMMUNITY)
    except hub.Refused:  # the project does not enable the plugin
        raise refused from None
    if project.id != context.project_id or kind is None or not kind.enabled or kind.profile_id != run.profile_id:
        raise refused
    return Run(context.run_id, run.dispatcher_id, project.id)


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


async def _invite(conn: AsyncConnection, context: hub.Context, arguments: dict) -> str:
    run = await community_run(conn, context)
    login, ref = rules.login_of(str(arguments.get("login") or "")), str(arguments.get("ref") or "")
    if login is None or not ref:
        raise Refused("give the requester's GitHub login and the request's ref (issue#<n>)")
    cfg = await settings.config(conn, context.project)
    await _allowed(conn, run, cfg, login, ref)
    if not getattr(context.config, "public_url", None):
        raise Refused("the hub has no public URL (EVO_HUB_PUBLIC_URL), so a link cannot be given")
    made = await hub.create_invite(
        conn,
        store.handle(),
        context.config,
        context.project,
        created_by=run.user_id,
        for_login=login,
        role=ROLE,
        days=INVITE_DAYS,
    )
    await store.add_sent(conn, run.project_id, ref, login, made.id, run.run_id)
    log.info("community invite made", extra={"invite": made.id, "run_id": run.run_id})
    return (
        f"Invite link for {login}, valid {INVITE_DAYS} days, only their GitHub login can accept it: "
        f"{made.url}\nPost it in {ref} as the answer; it is shown this once."
    )


async def invite(params: dict, context: hub.Context) -> dict:
    """``crm__invite``: a join link for the login that asked in invite request ``ref``, when the Policy allows."""
    try:
        async with hub.transaction(context) as conn:
            return _text(await _invite(conn, context, params.get("arguments") or {}))
    except Refused as exc:
        return _text(f"refused: {exc.message}", error=True)


__all__ = ["COMMUNITY", "community_run", "invite"]
