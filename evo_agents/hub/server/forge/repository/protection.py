"""The SQL of the ruleset checks of the Curator's repos: curator_repo_checks, which keeps the last check of each repo of
a project, and the repos and changes the checks read (``evo_agents.hub.server.forge.service.protection``)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.forge.models.forges import GITHUB
from evo_agents.hub.server.forge.models.protection import (
    MAX_CHECKS,
    PROTECTION_DAYS,
    RECHECK_HOURS,
    WAITING_RECHECK,
)


async def repo_origins(conn: AsyncConnection, project_id: int) -> dict[str, str | None]:
    """The project's repos, each with its origin, by name."""
    pr = tables.project_repos
    rows = await conn.execute(select(pr.c.name, pr.c.origin).where(pr.c.project_id == project_id).order_by(pr.c.name))
    return {row.name: row.origin for row in rows}


def _checks(project_id: int):
    rc, u = tables.curator_repo_checks, tables.users
    return (
        select(rc, u.c.login.label("checked_by_login"))
        .join_from(rc, u, u.c.id == rc.c.checked_by, isouter=True)
        .where(rc.c.project_id == project_id)
    )


async def checks(conn: AsyncConnection, project_id: int) -> dict:
    """The last check of each repo of the project that has one, by repo, with the login of who asked."""
    return {row.repo: row for row in await conn.execute(_checks(project_id))}


async def check_of(conn: AsyncConnection, project_id: int, repo: str):
    """The last check of ``repo`` of the project, with the login of who asked; it must be there."""
    return (await conn.execute(_checks(project_id).where(tables.curator_repo_checks.c.repo == repo))).one()


async def put_check(conn: AsyncConnection, project_id: int, repo: str, values: dict) -> None:
    """Keep ``values`` as the last check of ``repo`` of the project, in place of the one before."""
    rc = tables.curator_repo_checks
    statement = pg_insert(rc).values(project_id=project_id, repo=repo, **values)
    await conn.execute(statement.on_conflict_do_update(constraint="curator_repo_checks_pkey", set_=values))


async def fresh_check(conn: AsyncConnection, project_id: int, repo: str):
    """The last check of ``repo``: its owner/name, whether it passed, its reason and whether it is within
    PROTECTION_DAYS (``fresh``); None when it has none."""
    rc = tables.curator_repo_checks
    fresh = (rc.c.checked_at > func.now() - timedelta(days=PROTECTION_DAYS)).label("fresh")
    query = select(rc.c.github_repo, rc.c.protected, rc.c.reason, fresh).where(
        rc.c.project_id == project_id, rc.c.repo == repo
    )
    return (await conn.execute(query)).one_or_none()


async def waited_for(conn: AsyncConnection) -> list:
    """The repos of GitHub a planned change waits for (each repo of a change in several) whose ruleset is due a check:
    none within PROTECTION_DAYS, or a last check that did not pass, WAITING_RECHECK ago or more."""
    c, pr, rc = tables.curator_changes, tables.project_repos, tables.curator_repo_checks
    cr = tables.curator_change_repos
    repo = func.coalesce(cr.c.repo, c.c.repo)
    due = (
        rc.c.project_id.is_(None)
        | (rc.c.checked_at < func.now() - timedelta(days=PROTECTION_DAYS))
        | (rc.c.protected.is_(False) & (rc.c.checked_at < func.now() - WAITING_RECHECK))
    )
    query = (
        select(c.c.project_id, repo.label("repo"), pr.c.origin)
        .distinct()
        .join_from(c, cr, cr.c.change_id == c.c.id, isouter=True)
        .join(pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == repo))
        .outerjoin(rc, (rc.c.project_id == c.c.project_id) & (rc.c.repo == repo))
        .where(c.c.state == "planned", c.c.forge == GITHUB.name, due)
        .order_by(c.c.project_id, repo)
        .limit(MAX_CHECKS)
    )
    return (await conn.execute(query)).all()  # pyright: ignore[reportReturnType]


async def stale(conn: AsyncConnection) -> list:
    """The rulesets checked more than RECHECK_HOURS ago, oldest first."""
    rc, pr = tables.curator_repo_checks, tables.project_repos
    query = (
        select(rc.c.project_id, rc.c.repo, pr.c.origin)
        .join_from(rc, pr, (pr.c.project_id == rc.c.project_id) & (pr.c.name == rc.c.repo))
        .where(rc.c.checked_at < func.now() - timedelta(hours=RECHECK_HOURS))
        .order_by(rc.c.checked_at)
        .limit(MAX_CHECKS)
    )
    return (await conn.execute(query)).all()  # pyright: ignore[reportReturnType]
