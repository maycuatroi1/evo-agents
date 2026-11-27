"""Signing in from the CLI and on the web, signing out, and who the caller is (``evo_agents.hub.server.auth``): the
users row of a GitHub account, the token it gets with its audit row, in one transaction."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.config import HubConfig
from evo_agents.hub.server import audit, org_store
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.forge.models.github import GitHubRefused, GitHubUnavailable, GitHubUser
from evo_agents.hub.server.identity.models.auth import USER_RETRIES, GrantInfo, SignedIn, TokenInfo, WhoAmI
from evo_agents.hub.server.identity.repository import auth as repository
from evo_agents.hub.server.security import (
    LOGIN_HINT,
    MACHINE,
    WEB,
    WWW_AUTHENTICATE,
    Principal,
    bind_admin,
    issue_token,
    revoke_token,
)

log = logging.getLogger("evo_agents.hub.server.auth")


@contextmanager
def github_errors(refused_status: int, hint: str):
    """GitHub's answers as API errors: refused becomes ``refused_status``, unavailable becomes 502."""
    try:
        yield
    except GitHubRefused as exc:
        headers = WWW_AUTHENTICATE if refused_status == 401 else None
        raise HTTPException(refused_status, f"{exc}: {hint}", headers=headers) from None
    except GitHubUnavailable as exc:
        raise HTTPException(502, str(exc)) from None


async def upsert_user(conn: AsyncConnection, user: GitHubUser) -> tuple[int, str]:
    """The id and login of the users row for a GitHub account, created or brought up to date."""
    for _ in range(USER_RETRIES):
        own = await repository.own_row(conn, user.id)
        holder = await repository.login_holder(conn, user.login)
        if holder is not None and holder.github_id != user.id:
            holder_id, holder_github_id = holder
            if holder_github_id is None and own is None:  # granted before the first sign-in: claim it
                await repository.claim(conn, holder_id, user.id, user.login)
                return holder_id, user.login
            if holder_github_id is None:  # granted to the new login of a known account: merge the grants into it
                await repository.merge_grants(conn, own.id, holder_id)  # pyright: ignore[reportOptionalMemberAccess]
                await org_store.merge_user(conn, own.id, holder_id)  # pyright: ignore[reportOptionalMemberAccess]
                await repository.delete_user(conn, holder_id)
            else:
                await repository.move_aside(conn, holder_id)
        if own is not None:
            await repository.seen(conn, own.id, user.login)
            return own.id, user.login
        user_id = await repository.create(conn, user.id, user.login)
        if user_id is not None:
            await org_store.place_user(conn, user_id)
            return user_id, user.login
    raise RuntimeError(f"could not settle the users row of GitHub account {user.id} after {USER_RETRIES} tries")


async def sign_in(conn: AsyncConnection, config: HubConfig, user: GitHubUser, kind: str, host: str | None) -> SignedIn:
    """A new token of ``kind`` for the GitHub ``user``, with its audit row, in the caller's transaction."""
    user_id, login = await upsert_user(conn, user)
    admin_as = await bind_admin(conn, config.admins, user_id, login)  # the account keeps the role through a rename
    issued = await issue_token(conn, user_id, kind, host)
    target = f"machine:{host}" if kind == MACHINE else "web"
    await audit.record(conn, actor_id=user_id, token_id=issued.token_id, action=audit.LOGIN, target=target)
    log.info("signed in", extra={"login": login, "kind": kind, "token_id": issued.token_id})
    return SignedIn(
        token=issued.token,
        token_id=issued.token_id,
        login=login,
        admin=config.is_admin(admin_as),
        expires_at=issued.expires_at,
    )


async def web_sign_in(engine: AsyncEngine, config: HubConfig, user: GitHubUser) -> SignedIn:
    """A new web session for the GitHub ``user``, in its own transaction."""
    async with UnitOfWork(engine) as conn:
        return await sign_in(conn, config, user, WEB, None)


async def sign_out(engine: AsyncEngine, user: Principal) -> None:
    """Revoke the credential the request came with."""
    async with UnitOfWork(engine) as conn:
        if await revoke_token(conn, user.token_id, user.user_id):
            target = audit.token_target(user.token_id)
            await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.LOGOUT, target=target)
    log.info("signed out", extra={"login": user.login, "kind": user.kind, "token_id": user.token_id})


async def github_login(engine: AsyncEngine, config: HubConfig, github, github_token: str, host: str) -> SignedIn:
    """Trade the GitHub token of the CLI's device flow for a machine token of the hub; with the client secret set,
    GitHub must first confirm the token was issued to this hub's OAuth App."""
    with github_errors(401, LOGIN_HINT):
        if config.github_client_id and config.github_client_secret:
            if not await github.issued_to_app(github_token):
                log.warning("sign-in refused: the GitHub token belongs to another app")
                raise GitHubRefused("the GitHub token was not issued to this hub's OAuth App")
        user = await github.user(github_token)
    async with UnitOfWork(engine) as conn:
        return await sign_in(conn, config, user, MACHINE, host)


async def whoami(engine: AsyncEngine, user: Principal) -> WhoAmI:
    """The caller, the token the request came with and the grants they hold."""
    async with UnitOfWork(engine) as conn:
        token = await repository.token_info(conn, user.token_id)
        grant_rows = await repository.grants_of(conn, user.user_id)
    return WhoAmI(
        login=user.login,
        admin=user.admin,
        token=TokenInfo(**token._mapping),
        grants=[GrantInfo(**row._mapping) for row in grant_rows],
    )
