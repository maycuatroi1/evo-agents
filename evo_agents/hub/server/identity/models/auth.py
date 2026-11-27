"""The shapes of signing in from the CLI and of who the caller is (``evo_agents.hub.server.auth``)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

NO_STORE = {"Cache-Control": "no-store"}
USER_RETRIES = 3  # a sign-in racing another sign-in of the same new account looks again


class AuthConfig(BaseModel):
    github_client_id: str = Field(description="public client id of the hub's GitHub OAuth App")
    web_login: bool = Field(description="whether GET /v1/auth/web/login is configured on this hub")


class GitHubLogin(BaseModel):
    github_token: str = Field(min_length=1, max_length=1024, description="the token GitHub's device flow ended with")
    host: str = Field(
        min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$", description="host name of the machine"
    )


class SignedIn(BaseModel):
    token: str = Field(description="the hub token; shown once, the hub keeps only its SHA-256")
    token_id: int
    login: str
    admin: bool
    expires_at: datetime


class TokenInfo(BaseModel):
    id: int
    kind: str
    host: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime


class GrantInfo(BaseModel):
    project: str
    role: str
    max_level: str


class WhoAmI(BaseModel):
    login: str
    admin: bool
    token: TokenInfo
    grants: list[GrantInfo]
