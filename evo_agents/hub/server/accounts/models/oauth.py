"""The tokens of an account that signed in with OAuth, as PUT /v1/secrets/{name} takes them
(``evo_agents.hub.server.accounts.repository.oauth`` says what the hub keeps of them and how)."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, SecretStr

from evo_agents.hub.credentials import MAX_SECRET_BYTES
from evo_agents.hub.server.base.models import StrictModel

OAUTH_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"  # the one variable an OAuth account sets
MAX_IDENTITY_CHARS = 200


class OAuthTokens(StrictModel):
    """The tokens beside the access token (which is the secret's value) that PUT /v1/secrets/{name} takes for an
    account of OAuth; never shown again."""

    refresh_token: SecretStr = Field(
        min_length=1, max_length=MAX_SECRET_BYTES, description="the refresh token; sealed, never shown again"
    )
    access_expires_at: AwareDatetime = Field(description="when the access token (the value) ends")
    account_uuid: str | None = Field(None, max_length=MAX_IDENTITY_CHARS, description="the Claude account's id")
    organization_uuid: str | None = Field(None, max_length=MAX_IDENTITY_CHARS, description="its organization's id")
    email: str | None = Field(None, max_length=MAX_IDENTITY_CHARS, description="the Claude account's address")
