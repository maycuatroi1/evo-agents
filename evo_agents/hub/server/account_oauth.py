"""The tokens of an account that signed in with OAuth (``docs/credentials.md``, section "Accounts"): what the hub keeps
of them and how.

``evo-agents hub account oauth-login`` runs Claude's OAuth sign-in on the member's machine and writes the result with
PUT /v1/secrets/{name}: the access token is the secret's value, as a token of ``claude setup-token`` is, and the rest
is ``OAuthTokens``, sent as the field ``oauth``. The hub keeps the whole set (access token, refresh token, when the
access token ends, who the account is) as one JSON object sealed under the associated data ``account-oauth:{secret
id}``, in the oauth_sealed, oauth_nonce and oauth_key_id columns of the account's row; so a set copied to another
account's row does not open there. No column holds a token in plaintext, and no response, audit row or log line shows
one: an account tells only that it is OAuth (``Account.auth``).
"""

from __future__ import annotations

import json

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import MAX_SECRET_BYTES
from evo_agents.hub.server.sealing import Sealed, Sealer

OAUTH_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"  # the one variable an OAuth account sets
MAX_IDENTITY_CHARS = 200


def oauth_aad(secret_id: int) -> bytes:
    """The associated data of an account's sealed tokens: they open only as the tokens of this secret's account."""
    return f"account-oauth:{secret_id}".encode()


class OAuthTokens(BaseModel):
    """The tokens beside the access token (which is the secret's value) that PUT /v1/secrets/{name} takes for an
    account of OAuth; never shown again."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: SecretStr = Field(
        min_length=1, max_length=MAX_SECRET_BYTES, description="the refresh token; sealed, never shown again"
    )
    access_expires_at: AwareDatetime = Field(description="when the access token (the value) ends")
    account_uuid: str | None = Field(None, max_length=MAX_IDENTITY_CHARS, description="the Claude account's id")
    organization_uuid: str | None = Field(None, max_length=MAX_IDENTITY_CHARS, description="its organization's id")
    email: str | None = Field(None, max_length=MAX_IDENTITY_CHARS, description="the Claude account's address")


def seal_tokens(sealer: Sealer, secret_id: int, access_token: str, oauth: OAuthTokens) -> Sealed:
    """The whole set as one sealed JSON object."""
    document = {
        "access_token": access_token,
        "refresh_token": oauth.refresh_token.get_secret_value(),
        "expires_at": oauth.access_expires_at.isoformat(),
        "account_uuid": oauth.account_uuid,
        "organization_uuid": oauth.organization_uuid,
        "email": oauth.email,
    }
    return sealer.seal(json.dumps(document), oauth_aad(secret_id))


def open_tokens(sealer: Sealer, secret_id: int, sealed: Sealed) -> dict:
    """The set ``sealed`` holds; ``Unsealable`` when it does not open for this account."""
    return json.loads(sealer.open(sealed, oauth_aad(secret_id)))


async def store_tokens(conn: AsyncConnection, secret_id: int, sealed: Sealed) -> None:
    """The account's row, made when there is none, holding ``sealed`` in place of the set it held."""
    a = tables.accounts
    values = {"oauth_sealed": sealed.ciphertext, "oauth_nonce": sealed.nonce, "oauth_key_id": sealed.key_id}
    statement = pg_insert(a).values(secret_id=secret_id, **values)
    await conn.execute(
        statement.on_conflict_do_update(index_elements=[a.c.secret_id], set_=values | {"updated_at": func.now()})
    )


async def drop_tokens(conn: AsyncConnection, secret_id: int) -> None:
    """The account's sealed set gone: the secret was deleted, or written again as a token of another kind."""
    a = tables.accounts
    await conn.execute(
        update(a).values(oauth_sealed=None, oauth_nonce=None, oauth_key_id=None).where(a.c.secret_id == secret_id)
    )
