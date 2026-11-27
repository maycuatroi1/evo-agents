"""The SQL of the audit trail and every user's tokens (``evo_agents.hub.server.admin_console``): a page of the
trail, its distinct actions, a page of the tokens, and revoking one."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, case, func, literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.hub_admin.repository.admin import TOKEN_EXPIRING, TOKEN_LIVE, TOKEN_UNUSED


def _key(after: tuple[datetime, int], at, row_id):
    """The sort key of a cursor, bound with the types of its columns (an id may need all of bigint)."""
    return tuple_(literal(after[0], at.type), literal(after[1], row_id.type))


def _trail(actor, action, project, since, until, after: tuple[datetime, int] | None, limit: int):
    """The audit rows the filters leave, newest first, at most ``limit``; only those after the sort key ``after``."""
    trail, users, projects = tables.audit, tables.users, tables.projects
    query = select(
        trail.c.id,
        trail.c.at,
        users.c.login.label("actor"),
        trail.c.token_id,
        trail.c.action,
        trail.c.target,
        projects.c.name.label("project"),
    ).select_from(
        trail.outerjoin(users, users.c.id == trail.c.actor_id).outerjoin(projects, projects.c.id == trail.c.project_id)
    )
    if actor is not None:
        named = users.alias("named_user")  # the actor's row, apart from the users joined for every row's login
        actor_id = select(named.c.id).where(func.lower(named.c.login) == func.lower(actor)).scalar_subquery()
        query = query.where(trail.c.actor_id == actor_id)
    if action is not None:
        query = query.where(trail.c.action == action)
    if project is not None:
        named = projects.alias("named_project")
        project_id = select(named.c.id).where(named.c.name == project).scalar_subquery()
        query = query.where(trail.c.project_id == project_id)
    if since is not None:
        query = query.where(trail.c.at >= since)
    if until is not None:
        query = query.where(trail.c.at < until)
    if after is not None:
        query = query.where(tuple_(trail.c.at, trail.c.id) < _key(after, trail.c.at, trail.c.id))
    return query.order_by(trail.c.at.desc(), trail.c.id.desc()).limit(limit)


def _actions():
    """Every distinct action without reading the whole table: one index probe per action (audit_action_idx, schema
    0007). A recursive CTE starts from the first action and each step takes the next one after it."""
    trail = tables.audit
    first = select(trail.c.action).order_by(trail.c.action).limit(1)
    found = first.cte("found", recursive=True)
    later = trail.alias("a")
    following = (
        select(later.c.action)
        .where(later.c.action > found.c.action)
        .order_by(later.c.action)
        .limit(1)
        .scalar_subquery()
    )
    found = found.union_all(select(following).where(found.c.action.is_not(None)))
    return select(found.c.action).where(found.c.action.is_not(None))


_tokens = tables.tokens
# What each ``state`` of GET /v1/admin/tokens keeps; ``any`` keeps every token.
TOKEN_FILTERS = {
    "active": TOKEN_LIVE,
    "revoked": _tokens.c.revoked_at.is_not(None),
    "expired": and_(_tokens.c.revoked_at.is_(None), _tokens.c.expires_at <= func.now()),
    "expiring": TOKEN_EXPIRING,
    "unused": TOKEN_UNUSED,
}


def _listed(login, kind, state: str, after: tuple[datetime, int] | None, limit: int):
    """The tokens of every user the filters leave, newest first, at most ``limit``; only those after ``after``."""
    tokens, users = tables.tokens, tables.users
    token_state = case(
        (tokens.c.revoked_at.is_not(None), "revoked"),
        (tokens.c.expires_at <= func.now(), "expired"),
        else_="active",
    )
    query = select(
        tokens.c.id,
        users.c.login,
        tokens.c.kind,
        tokens.c.host,
        tokens.c.created_at,
        tokens.c.last_used_at,
        tokens.c.expires_at,
        tokens.c.revoked_at,
        token_state.label("state"),
    ).join_from(tokens, users, users.c.id == tokens.c.user_id)
    if login is not None:
        query = query.where(func.lower(users.c.login) == func.lower(login))
    if kind is not None:
        query = query.where(tokens.c.kind == kind)
    if state in TOKEN_FILTERS:
        query = query.where(TOKEN_FILTERS[state])
    if after is not None:
        query = query.where(tuple_(tokens.c.created_at, tokens.c.id) < _key(after, tokens.c.created_at, tokens.c.id))
    return query.order_by(tokens.c.created_at.desc(), tokens.c.id.desc()).limit(limit)


async def trail_rows(conn: AsyncConnection, actor, action, project, since, until, after, limit: int):
    """The rows of ``_trail``."""
    return (await conn.execute(_trail(actor, action, project, since, until, after, limit))).all()


async def actions(conn: AsyncConnection) -> list[str]:
    """Every distinct action of the trail, in alphabetical order."""
    return list((await conn.execute(_actions())).scalars())


async def token_rows(conn: AsyncConnection, login, kind, state: str, after, limit: int):
    """The rows of ``_listed``."""
    return (await conn.execute(_listed(login, kind, state, after, limit))).all()


async def revoke_token(conn: AsyncConnection, token_id: int) -> str | None:
    """Revoke token ``token_id`` unless it was; the login of its owner, or None when nothing was revoked."""
    tokens, users = tables.tokens, tables.users
    revoked = await conn.execute(
        update(tokens)
        .values(revoked_at=func.now())
        .where(tokens.c.id == token_id, users.c.id == tokens.c.user_id, tokens.c.revoked_at.is_(None))
        .returning(users.c.login)
    )
    return revoked.scalar()


async def token_revoked_at(conn: AsyncConnection, token_id: int):
    """(revoked_at,) of token ``token_id``, or None when the hub never issued it."""
    tokens = tables.tokens
    found = await conn.execute(select(tokens.c.revoked_at).where(tokens.c.id == token_id))
    return found.first()
