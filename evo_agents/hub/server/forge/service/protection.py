"""The repos whose ruleset keeps the Curator off their default branch. A Builder of the Curator runs on a repo of GitHub
only once the hub checked, with the Curator's App, within PROTECTION_DAYS, that an active ruleset of its default branch
restricts updates and never lets that App by (``pulls.check_ruleset``); ``evo_agents.hub.server.changes`` holds the
rest of the Curator's changes.

curator_repo_checks keeps the last check of each repo of a project. GET /v1/projects/{p}/curator/protection (reader)
lists the project's repos with it, and POST .../curator/protection/{repo}/check (admin) checks one now, audited as
curator.protection (``evo_agents.hub.server.forge.routes.protection``). The job ``curator.changes`` checks rulesets
on its own too (``check_rulesets``), with checked_by null: first each repo a planned change on GitHub waits for, at its
next pass when the repo has no check within PROTECTION_DAYS, and every WAITING_RECHECK while its last check did not
pass; then every check older than RECHECK_HOURS. Each asks GitHub outside any transaction of the database, and a hub
without the Curator's App asks nothing. ``ruleset_wait`` says why a Builder still waits for a repo, which the night
shift keeps as its change's reason. The hub never creates or changes a ruleset: the repo's owner adds one, as
docs/curator.md says under "Two GitHub Apps and the ruleset". The SQL is in
``evo_agents.hub.server.forge.repository.protection``, the shapes in ``evo_agents.hub.server.forge.models.protection``.
"""

from __future__ import annotations

import logging
from collections import Counter

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import judge
from evo_agents.hub.config import CURATOR_APP_VARIABLES, HubConfig
from evo_agents.hub.server import audit
from evo_agents.hub.server.forge.client.github_app import GitHubApp
from evo_agents.hub.server.forge.models.forges import GITHUB
from evo_agents.hub.server.forge.models.github import GitHubUnavailable
from evo_agents.hub.server.forge.models.protection import (
    GUIDE,
    PROTECTION,
    PROTECTION_DAYS,
    Protection,
    RepoCheck,
    reason_line,
)
from evo_agents.hub.server.forge.repository import protection as store
from evo_agents.hub.server.merge.service.pulls import check_ruleset
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds

log = logging.getLogger(__name__)


def need_reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the Curator of project {access.name} needs a grant on it")


def _repo_check(name: str, origin: str | None, row) -> RepoCheck:
    target = GITHUB.target(origin)
    return RepoCheck(
        repo=name,
        origin=origin,
        forge=judge.forge_of(origin),
        github_repo=f"{target.owner}/{target.name}" if target else None,
        protected=None if row is None else row.protected,
        default_branch=None if row is None else row.default_branch,
        reason=None if row is None else row.reason,
        rulesets=[] if row is None else list(row.rulesets or []),
        checked_at=None if row is None else row.checked_at,
        checked_by=None if row is None else row.checked_by_login,
    )


async def protection(conn: AsyncConnection, user: Principal, project: str) -> Protection:
    """The project's repos, each with the last check of its ruleset, for a reader of the project."""
    access = await project_access(conn, user, project)
    need_reader(access)
    origins = await store.repo_origins(conn, access.project_id)
    checks = await store.checks(conn, access.project_id)
    return Protection(
        project=access.name, repos=[_repo_check(name, origin, checks.get(name)) for name, origin in origins.items()]
    )


async def store_check(conn: AsyncConnection, project_id: int, repo: str, owner_repo: str, found, by: int | None):
    """Keep ``found``, a ``pulls.RulesetCheck`` of ``repo``, as its last check, asked by user ``by`` (None: the hub)."""
    values = {
        "github_repo": owner_repo,
        "default_branch": found.default_branch,
        "protected": found.protected,
        "reason": reason_line(found.reason) or "no reason",
        "rulesets": found.rulesets,
        "checked_at": func.now(),
        "checked_by": by,
    }
    await store.put_check(conn, project_id, repo, values)


async def check_now(
    engine: AsyncEngine, app: GitHubApp | None, config: HubConfig, user: Principal, project: str, repo: str
) -> RepoCheck:
    """Check ``repo`` of ``project`` now with the Curator's App ``app``, for an admin of the project, keep the check
    and audit it; GitHub is asked outside any transaction."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        if not holds(access.role, "admin"):
            raise HTTPException(403, f"checking the rulesets of project {access.name} needs the admin role on it")
        origins = await store.repo_origins(conn, access.project_id)
    if repo not in origins:
        raise HTTPException(404, f"project {project} has no repo {repo}")
    target = GITHUB.target(origins[repo])
    if target is None:
        raise HTTPException(422, f"repo {repo} is not on GitHub: the hub checks rulesets on GitHub alone")
    if app is None:
        unset = ", ".join(config.curator_app_missing())
        raise HTTPException(503, f"this hub has no Curator App ({unset} not set): it cannot check a ruleset")
    try:
        found = await check_ruleset(app, target.owner, target.name)
    except GitHubUnavailable as exc:
        raise HTTPException(502, str(exc)) from None
    async with engine.begin() as conn:
        await store_check(conn, access.project_id, repo, f"{target.owner}/{target.name}", found, user.user_id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=PROTECTION,
            target=f"{access.name} repo={repo} protected={str(found.protected).lower()}",
            project_id=access.project_id,
        )
        row = await store.check_of(conn, access.project_id, repo)
    log.info("ruleset checked", extra={"project": project, "repo": repo, "protected": found.protected})
    return _repo_check(repo, origins[repo], row)


# Why a Builder waits, and the job's checks


async def ruleset_wait(conn: AsyncConnection, project_id: int, repo: str, *, curator_app: bool) -> str | None:
    """Why a Builder may not run on ``repo``, a repo of GitHub, for its ruleset yet; None once the hub checked it within
    PROTECTION_DAYS and found it keeps the Curator's App off. ``curator_app``: whether this hub has that App."""
    check = await store.fresh_check(conn, project_id, repo)
    if check is not None and check.protected and check.fresh:
        return None
    if not curator_app:
        unset = " and ".join(CURATOR_APP_VARIABLES)
        return (
            f"a Builder runs on repo {repo} only once the hub has checked that its ruleset keeps the Curator's App off "
            f"its default branch, and this hub has no Curator App to check it with ({unset} are not both set), so it "
            f"asks GitHub nothing; see {GUIDE}"
        )
    if check is None:
        return (
            f"the hub has not checked the ruleset of repo {repo} yet: the job curator.changes checks it with the "
            "Curator's App at its next pass"
        )
    if not check.fresh:
        return (
            f"the hub's last check of the ruleset of repo {repo} ({check.github_repo}) is more than {PROTECTION_DAYS} "
            "days old: the job curator.changes checks it again at its next pass"
        )
    return (
        f"the ruleset of repo {repo} ({check.github_repo} on GitHub) does not keep the Curator's App off its default "
        f"branch: {check.reason}. The repo's owner has to set it up as {GUIDE} says: the Curator's App installed on "
        "the repo, and an active ruleset of its default branch that restricts updates and leaves that App out of its "
        "bypass list. The hub checks it again every hour while a change waits for it"
    )


async def check_rulesets(engine: AsyncEngine, curator_app: GitHubApp | None, outcomes: Counter) -> None:
    """The job's own checks of rulesets, with the Curator's App, each asked of GitHub outside any transaction: first
    the repos a planned change waits for (``ruleset_checked``), then the checks older than RECHECK_HOURS
    (``rechecked``). Nothing without the Curator's App; GitHub failing leaves the rest for a later pass."""
    if curator_app is None:
        return
    for word, due in (("ruleset_checked", store.waited_for), ("rechecked", store.stale)):
        async with engine.begin() as conn:
            rows = await due(conn)
        try:
            for row in rows:
                target = GITHUB.target(row.origin)
                if target is None:
                    continue
                found = await check_ruleset(curator_app, target.owner, target.name)
                async with engine.begin() as conn:
                    await store_check(conn, row.project_id, row.repo, f"{target.owner}/{target.name}", found, None)
                outcomes[word] += 1
        except GitHubUnavailable as exc:
            log.warning("rulesets left to check again later", extra={"why": str(exc)})
