"""The refresh of an OAuth account's access token at lease time (``docs/credentials.md``, section "Accounts").

An access token of Claude's OAuth lives about eight hours. When a run asks for its credentials, ``refresh_due`` looks at
each OAuth account it may be leased: one whose access token ends within ``REFRESH_WINDOW`` (or has ended) is traded at
the token endpoint for a new one, with its refresh token. Anthropic rotates the refresh token at every use, so the new
set (access token, refresh token, end) is stored whole before anything is leased: the sealed set of the account and the
value of its secret, which is what a lease hands out as ``CLAUDE_CODE_OAUTH_TOKEN``, change in one transaction. A token
that is still good is leased as it is, with no call out.

The secret's row is locked for the whole refresh, so runs of one account that lease close together refresh once: the
ones that wait read the set the first stored and find it good. A refresh that fails leaves the set as it was (a token
that has not ended is still leased; one that has is not) and logs why, without a token; a refresh the endpoint refuses (the
refresh token is revoked or ended) marks the account as needing a new sign-in (``choice.mark_relogin``), and
``refresh_due`` names it so the lease moves the run to another account.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import claude_oauth
from evo_agents.hub.server.accounts.models.oauth import OAUTH_ENV_VAR, OAuthTokens
from evo_agents.hub.server.accounts.repository import refresh as repository
from evo_agents.hub.server.accounts.repository.oauth import open_tokens, seal_tokens, store_tokens
from evo_agents.hub.server.accounts.service.choice import mark_relogin
from evo_agents.hub.server.sealing import Sealer, Unsealable, secret_aad

log = logging.getLogger("evo_agents.hub.accounts")

REFRESH_WINDOW = timedelta(minutes=5)  # a token with less left than this is refreshed before it is leased


def is_due(expires_at: datetime | None, now: datetime) -> bool:
    return expires_at is not None and expires_at <= now + REFRESH_WINDOW


async def refresh_due(engine: AsyncEngine, sealer: Sealer, secrets) -> set[int]:
    """Refresh each of ``secrets`` (env secrets, each with its owner_id: a run's owner's, or one shared with them) that
    is an OAuth account whose access token is due; the ids of those whose refresh token the endpoint refused, which
    now need a new sign-in."""
    now = datetime.now(UTC)
    rejected: set[int] = set()
    for secret in secrets:
        if secret.kind == "env" and secret.env_var == OAUTH_ENV_VAR and is_due(secret.expires_at, now):
            if await _refresh(engine, sealer, secret.owner_id, secret.id) == REJECTED:
                rejected.add(secret.id)
    return rejected


STORED, SKIPPED, REJECTED = "stored", "skipped", "rejected"


async def refresh_account(engine: AsyncEngine, sealer: Sealer, owner_id: int, secret_id: int) -> bool:
    """Whether this call stored a new set for account ``secret_id``; False when it was good already (another run
    refreshed it first), is no OAuth account, or the refresh failed."""
    return await _refresh(engine, sealer, owner_id, secret_id) == STORED


async def _refresh(engine: AsyncEngine, sealer: Sealer, owner_id: int, secret_id: int) -> str:
    """STORED, REJECTED (the endpoint refused the refresh token: the account is marked for a new sign-in) or SKIPPED."""
    async with engine.begin() as conn:
        found = await repository.lock_secret(conn, owner_id, secret_id)
        if found is None:
            return SKIPPED
        sealed = await repository.sealed_set(conn, secret_id)
        if sealed is None:
            return SKIPPED
        try:
            held = open_tokens(sealer, secret_id, sealed)
            ends = datetime.fromisoformat(held["expires_at"])
        except (Unsealable, ValueError, KeyError) as exc:
            log.warning(
                "an OAuth account's tokens do not open", extra={"secret": found.name, "why": type(exc).__name__}
            )
            return SKIPPED
        if not is_due(ends, datetime.now(UTC)):
            return SKIPPED
        try:  # the row stays locked: a run of the same account waits here and then finds the new set
            got = await asyncio.to_thread(claude_oauth.refresh, held["refresh_token"])
        except claude_oauth.OAuthError as exc:
            extra = {"secret": found.name, "why": str(exc), "rejected": isinstance(exc, claude_oauth.RefreshRejected)}
            log.warning("an OAuth account's access token could not be refreshed", extra=extra)
            if not extra["rejected"]:
                return SKIPPED
            await mark_relogin(conn, secret_id)
            return REJECTED
        oauth = OAuthTokens(
            refresh_token=SecretStr(got.refresh_token),
            access_expires_at=got.expires_at,
            account_uuid=got.account_uuid or held.get("account_uuid"),
            organization_uuid=got.organization_uuid or held.get("organization_uuid"),
            email=got.email or held.get("email"),
        )
        value = sealer.seal(got.access_token, secret_aad(owner_id, found.name, found.kind))
        await repository.store_value(conn, secret_id, value, got.expires_at)
        await store_tokens(conn, secret_id, seal_tokens(sealer, secret_id, got.access_token, oauth))
    log.info("an OAuth account's access token was refreshed", extra={"secret": found.name})
    return STORED
