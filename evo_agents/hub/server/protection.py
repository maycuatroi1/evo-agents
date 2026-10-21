"""The repos whose ruleset keeps the Curator off their default branch. A Builder of the Curator runs on a repo of GitHub
only once the hub checked, with the Curator's App, within PROTECTION_DAYS, that an active ruleset of its default branch
restricts updates and never lets that App by (``pulls.check_ruleset``); ``evo_agents.hub.server.changes`` holds the
rest of the Curator's changes.

curator_repo_checks keeps the last check of each repo of a project. GET /v1/projects/{p}/curator/protection (reader)
lists the project's repos with it, and POST .../curator/protection/{repo}/check (admin) checks one now, audited as
curator.protection. The job ``curator.changes`` checks rulesets on its own too (``check_rulesets``), with checked_by
null: first each repo a planned change on GitHub waits for, at its next pass when the repo has no check within
PROTECTION_DAYS, and every WAITING_RECHECK while its last check did not pass; then every check older than
RECHECK_HOURS. Each asks GitHub outside any transaction of the database, and a hub without the Curator's App asks
nothing. ``ruleset_wait`` says why a Builder still waits for a repo, which the night shift keeps as its change's
reason. The hub never creates or changes a ruleset: the repo's owner adds one, as docs/curator.md says under "Two
GitHub Apps and the ruleset".
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import judge, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.config import CURATOR_APP_VARIABLES
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.pulls import check_ruleset
from evo_agents.hub.server.runs.models import LINE
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

PROTECTION = "curator.protection"  # a repo's ruleset was checked: "<project> repo=<name> protected=<bool>"
PROTECTION_DAYS = 2  # a ruleset check older than this no longer lets a Builder run
RECHECK_HOURS = 24  # the job checks a ruleset again once its last check is this old
WAITING_RECHECK = timedelta(hours=1)  # how often the job checks again a ruleset that did not pass a change waits for
MAX_CHECKS = 20  # rulesets the job checks in one pass, for each of its two kinds of check
GUIDE = 'docs/curator.md, under "Two GitHub Apps and the ruleset"'

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}

RepoName = Annotated[str, Path(min_length=1, max_length=200, pattern=LINE)]


class RepoCheck(BaseModel):
    """A repo of the project and the last check of its ruleset."""

    repo: str
    origin: str | None
    forge: Literal[judge.FORGES] | None = Field(description="null without an origin the Curator could push to")
    github_repo: str | None = Field(description="owner/name on GitHub; null elsewhere")
    protected: bool | None = Field(
        description="a ruleset keeps the Curator's App off its default branch; null: never checked"
    )
    default_branch: str | None
    reason: str | None
    rulesets: list[dict] = Field(default_factory=list, description="each {id, enforcement, can_bypass}")
    checked_at: datetime | None
    checked_by: str | None = Field(description="who asked; null for the hub's own check again")


class Protection(BaseModel):
    project: str
    repos: list[RepoCheck]


# What changes and protection share


def reason_line(text: str | None) -> str | None:
    """``text`` as the one line a change or a check keeps as its reason, at most 2000 characters; None when empty."""
    if not text:
        return None
    flat = " ".join(text.split())
    return flat[:2000] or None


def need_reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the Curator of project {access.name} needs a grant on it")


async def repo_origins(conn: AsyncConnection, project_id: int) -> dict[str, str | None]:
    """The project's repos, each with its origin, by name."""
    pr = tables.project_repos
    rows = await conn.execute(select(pr.c.name, pr.c.origin).where(pr.c.project_id == project_id).order_by(pr.c.name))
    return {row.name: row.origin for row in rows}


@dataclass(frozen=True)
class Target:
    owner: str
    name: str


def github_target(origin: str | None) -> Target | None:
    """owner/name of ``origin`` on GitHub; None for an origin elsewhere, or none."""
    found = github_repo(origin) if origin else None
    return Target(*found) if found else None


# The routes


def _repo_check(name: str, origin: str | None, row) -> RepoCheck:
    found = github_repo(origin) if origin else None
    return RepoCheck(
        repo=name,
        origin=origin,
        forge=judge.forge_of(origin),
        github_repo="/".join(found) if found else None,
        protected=None if row is None else row.protected,
        default_branch=None if row is None else row.default_branch,
        reason=None if row is None else row.reason,
        rulesets=[] if row is None else list(row.rulesets or []),
        checked_at=None if row is None else row.checked_at,
        checked_by=None if row is None else row.checked_by_login,
    )


def _checks(project_id: int):
    rc, u = tables.curator_repo_checks, tables.users
    return (
        select(rc, u.c.login.label("checked_by_login"))
        .join_from(rc, u, u.c.id == rc.c.checked_by, isouter=True)
        .where(rc.c.project_id == project_id)
    )


@router.get("/{project}/curator/protection", response_model=Protection, responses=REFUSALS)
async def protection(request: Request, project: ProjectName, user: CurrentUser) -> Protection:
    """The project's repos, and whether the hub checked that a ruleset keeps the Curator off each default branch."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        origins = await repo_origins(conn, access.project_id)
        checks = {row.repo: row for row in await conn.execute(_checks(access.project_id))}
    return Protection(
        project=access.name, repos=[_repo_check(name, origin, checks.get(name)) for name, origin in origins.items()]
    )


async def store_check(conn: AsyncConnection, project_id: int, repo: str, owner_repo: str, found, by: int | None):
    """Keep ``found``, a ``pulls.RulesetCheck`` of ``repo``, as its last check, asked by user ``by`` (None: the hub)."""
    rc = tables.curator_repo_checks
    values = {
        "github_repo": owner_repo,
        "default_branch": found.default_branch,
        "protected": found.protected,
        "reason": reason_line(found.reason) or "no reason",
        "rulesets": found.rulesets,
        "checked_at": func.now(),
        "checked_by": by,
    }
    statement = pg_insert(rc).values(project_id=project_id, repo=repo, **values)
    await conn.execute(statement.on_conflict_do_update(constraint="curator_repo_checks_pkey", set_=values))


@router.post(
    "/{project}/curator/protection/{repo}/check",
    response_model=RepoCheck,
    responses={**REFUSALS, 422: {"model": ErrorBody}, 502: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def check_protection(request: Request, project: ProjectName, repo: RepoName, user: CurrentUser) -> RepoCheck:
    """Check now, with the Curator's App, whether a ruleset keeps the Curator off the repo's default branch; the
    project's admins alone may."""
    state = request.app.state
    async with state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        if not has_role(access.role, "admin"):
            raise HTTPException(403, f"checking the rulesets of project {access.name} needs the admin role on it")
        origins = await repo_origins(conn, access.project_id)
    if repo not in origins:
        raise HTTPException(404, f"project {project} has no repo {repo}")
    found_repo = github_repo(origins[repo]) if origins[repo] else None
    if found_repo is None:
        raise HTTPException(422, f"repo {repo} is not on GitHub: the hub checks rulesets on GitHub alone")
    app: GitHubApp | None = getattr(state, "curator_app", None)
    if app is None:
        unset = ", ".join(state.config.curator_app_missing())
        raise HTTPException(503, f"this hub has no Curator App ({unset} not set): it cannot check a ruleset")
    owner, name = found_repo
    try:
        found = await check_ruleset(app, owner, name)
    except GitHubUnavailable as exc:
        raise HTTPException(502, str(exc)) from None
    async with state.engine.begin() as conn:
        await store_check(conn, access.project_id, repo, f"{owner}/{name}", found, user.user_id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=PROTECTION,
            target=f"{access.name} repo={repo} protected={str(found.protected).lower()}",
            project_id=access.project_id,
        )
        row = (await conn.execute(_checks(access.project_id).where(tables.curator_repo_checks.c.repo == repo))).one()
    log.info("ruleset checked", extra={"project": project, "repo": repo, "protected": found.protected})
    return _repo_check(repo, origins[repo], row)


# Why a Builder waits, and the job's checks


async def ruleset_wait(conn: AsyncConnection, project_id: int, repo: str, *, curator_app: bool) -> str | None:
    """Why a Builder may not run on ``repo``, a repo of GitHub, for its ruleset yet; None once the hub checked it within
    PROTECTION_DAYS and found it keeps the Curator's App off. ``curator_app``: whether this hub has that App."""
    rc = tables.curator_repo_checks
    fresh = (rc.c.checked_at > func.now() - timedelta(days=PROTECTION_DAYS)).label("fresh")
    query = select(rc.c.github_repo, rc.c.protected, rc.c.reason, fresh).where(
        rc.c.project_id == project_id, rc.c.repo == repo
    )
    check = (await conn.execute(query)).one_or_none()
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


async def _waited_for(conn: AsyncConnection) -> list:
    """The repos of GitHub a planned change waits for whose ruleset is due a check: none within PROTECTION_DAYS, or a
    last check that did not pass, WAITING_RECHECK ago or more."""
    c, pr, rc = tables.curator_changes, tables.project_repos, tables.curator_repo_checks
    due = (
        rc.c.project_id.is_(None)
        | (rc.c.checked_at < func.now() - timedelta(days=PROTECTION_DAYS))
        | (rc.c.protected.is_(False) & (rc.c.checked_at < func.now() - WAITING_RECHECK))
    )
    query = (
        select(c.c.project_id, c.c.repo, pr.c.origin)
        .distinct()
        .join_from(c, pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == c.c.repo))
        .outerjoin(rc, (rc.c.project_id == c.c.project_id) & (rc.c.repo == c.c.repo))
        .where(c.c.state == "planned", c.c.forge == "github", due)
        .order_by(c.c.project_id, c.c.repo)
        .limit(MAX_CHECKS)
    )
    return (await conn.execute(query)).all()


async def _stale(conn: AsyncConnection) -> list:
    """The rulesets checked more than RECHECK_HOURS ago, oldest first."""
    rc, pr = tables.curator_repo_checks, tables.project_repos
    query = (
        select(rc.c.project_id, rc.c.repo, pr.c.origin)
        .join_from(rc, pr, (pr.c.project_id == rc.c.project_id) & (pr.c.name == rc.c.repo))
        .where(rc.c.checked_at < func.now() - timedelta(hours=RECHECK_HOURS))
        .order_by(rc.c.checked_at)
        .limit(MAX_CHECKS)
    )
    return (await conn.execute(query)).all()


async def check_rulesets(engine: AsyncEngine, curator_app: GitHubApp | None, outcomes: Counter) -> None:
    """The job's own checks of rulesets, with the Curator's App, each asked of GitHub outside any transaction: first
    the repos a planned change waits for (``ruleset_checked``), then the checks older than RECHECK_HOURS
    (``rechecked``). Nothing without the Curator's App; GitHub failing leaves the rest for a later pass."""
    if curator_app is None:
        return
    for word, due in (("ruleset_checked", _waited_for), ("rechecked", _stale)):
        async with engine.begin() as conn:
            rows = await due(conn)
        try:
            for row in rows:
                target = github_target(row.origin)
                if target is None:
                    continue
                found = await check_ruleset(curator_app, target.owner, target.name)
                async with engine.begin() as conn:
                    await store_check(conn, row.project_id, row.repo, f"{target.owner}/{target.name}", found, None)
                outcomes[word] += 1
        except GitHubUnavailable as exc:
            log.warning("rulesets left to check again later", extra={"why": str(exc)})
