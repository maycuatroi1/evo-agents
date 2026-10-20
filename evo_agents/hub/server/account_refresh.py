"""The refresh of an OAuth account's access token at lease time (``docs/credentials.md``, section "Accounts").

An access token of Claude's OAuth lives about eight hours. When a run asks for its credentials, ``refresh_due`` looks at
each OAuth account it may be leased: one whose access token ends within ``REFRESH_WINDOW`` (or has ended) is traded at
the token endpoint for a new one, with its refresh token. Anthropic rotates the refresh token at every use, so the new
set (access token, refresh token, end) is stored whole before anything is leased: the sealed set of the account and the
value of its secret, which is what a lease hands out as ``CLAUDE_CODE_OAUTH_TOKEN``, change in one transaction. A token
that is still good is leased as it is, with no call out.

The secret's row is locked for the whole refresh, so runs of one account that lease close together refresh once: the
ones that wait read the set the first stored and find it good. A refresh that fails leaves the set as it was (a token
that has not ended is still leased; one that has is not) and logs why, without a token; marking the account for a new
sign-in belongs to the failover (``account_choice``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import claude_oauth, tables
from evo_agents.hub.server.account_oauth import OAUTH_ENV_VAR, OAuthTokens, open_tokens, seal_tokens, store_tokens
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, secret_aad

log = logging.getLogger("evo_agents.hub.accounts")

REFRESH_WINDOW = timedelta(minutes=5)  # a token with less left than this is refreshed before it is leased


def is_due(expires_at: datetime | None, now: datetime) -> bool:
    return expires_at is not None and expires_at <= now + REFRESH_WINDOW


async def refresh_due(engine: AsyncEngine, sealer: Sealer, owner_id: int, secrets) -> None:
    """Refresh each of ``secrets`` (env secrets of ``owner_id``) that is an OAuth account whose access token is due."""
    now = datetime.now(UTC)
    for secret in secrets:
        if secret.kind == "env" and secret.env_var == OAUTH_ENV_VAR and is_due(secret.expires_at, now):
            await refresh_account(engine, sealer, owner_id, secret.id)


async def refresh_account(engine: AsyncEngine, sealer: Sealer, owner_id: int, secret_id: int) -> bool:
    """Whether this call stored a new set for account ``secret_id``; False when it was good already (another run
    refreshed it first), is no OAuth account, or the refresh failed."""
    s, a = tables.secrets, tables.accounts
    async with engine.begin() as conn:
        found = (
            await conn.execute(
                select(s.c.name, s.c.kind)
                .where(s.c.id == secret_id, s.c.owner_id == owner_id, s.c.deleted_at.is_(None))
                .with_for_update()
            )
        ).first()
        if found is None:
            return False
        # read once the lock is won, as a statement of its own: it sees what the refresh before it stored
        sets = select(a.c.oauth_sealed, a.c.oauth_nonce, a.c.oauth_key_id).where(a.c.secret_id == secret_id)
        row = (await conn.execute(sets)).first()
        if row is None or row.oauth_sealed is None:
            return False
        try:
            held = open_tokens(sealer, secret_id, Sealed(row.oauth_sealed, row.oauth_nonce, row.oauth_key_id))
            ends = datetime.fromisoformat(held["expires_at"])
        except (Unsealable, ValueError, KeyError) as exc:
            log.warning(
                "an OAuth account's tokens do not open", extra={"secret": found.name, "why": type(exc).__name__}
            )
            return False
        if not is_due(ends, datetime.now(UTC)):
            return False
        try:  # the row stays locked: a run of the same account waits here and then finds the new set
            got = await asyncio.to_thread(claude_oauth.refresh, held["refresh_token"])
        except claude_oauth.OAuthError as exc:
            extra = {"secret": found.name, "why": str(exc), "rejected": isinstance(exc, claude_oauth.RefreshRejected)}
            log.warning("an OAuth account's access token could not be refreshed", extra=extra)
            return False
        oauth = OAuthTokens(
            refresh_token=SecretStr(got.refresh_token),
            access_expires_at=got.expires_at,
            account_uuid=got.account_uuid or held.get("account_uuid"),
            organization_uuid=got.organization_uuid or held.get("organization_uuid"),
            email=got.email or held.get("email"),
        )
        value = sealer.seal(got.access_token, secret_aad(owner_id, found.name, found.kind))
        await conn.execute(
            update(s)
            .where(s.c.id == secret_id)
            .values(
                sealed=value.ciphertext,
                nonce=value.nonce,
                key_id=value.key_id,
                expires_at=got.expires_at,
                updated_at=func.now(),
            )
        )
        await store_tokens(conn, secret_id, seal_tokens(sealer, secret_id, got.access_token, oauth))
    log.info("an OAuth account's access token was refreshed", extra={"secret": found.name})
    return True
