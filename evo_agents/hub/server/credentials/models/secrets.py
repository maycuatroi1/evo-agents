"""The shapes of the owner's secrets under /v1/secrets (``routes.secrets``), their limits and the refusal texts more
than one layer gives."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Path
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr

from evo_agents.hub.credentials import (
    DEFAULT_GIT_USERNAME,
    MAX_SECRET_BYTES,
    SECRET_ENVIRONMENTS,
    SECRET_KINDS,
    SECRET_NAME,
)
from evo_agents.hub.server.accounts.models.oauth import OAUTH_ENV_VAR, OAuthTokens
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.runs.models.placement import PoolName
from evo_agents.hub.server.share_views import CredentialShare
from evo_agents.hub.server.workers.models.workers import LINE, WORKER_NAME

MAX_ENV_VAR_CHARS = 200  # as the secrets table accepts them
MAX_URL_PREFIX_CHARS = 2000
MAX_USERNAME_CHARS = 200
MAX_BINDINGS = 100  # projects, and workers, one secret names
# https://host[:port][/path], host in lower case, no user, no trailing slash: what normalize_origin makes of an https
# URL, and what the secrets table accepts
URL_PREFIX = re.compile(r"https://[a-z0-9._-]+(:[0-9]{1,5})?(/[^\s]*[^/\s])?")
NO_KEY = (
    "this hub keeps no secrets: it needs {missing}, the key that seals them; ask its operator to set it "
    "(docs/credentials.md)"
)
NO_SECRET = "you have no secret named {name} on this hub: see GET /v1/secrets"
PROVIDER_HOLDS = "{name} is one of your provider accounts: change it with /v1/provider-accounts/{name}"
REBIND_NEITHER = "give workers or pools: the binding that replaces the secret's; an empty list is any worker of yours"
REBIND_BOTH = "give workers or pools, not both: the one given replaces the secret's binding whole"

SecretName = Annotated[str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="unique among the caller's secrets")]


class SecretWrite(BaseModel):
    kind: Literal[SECRET_KINDS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="env: a variable of the agent's environment; git: what git's credential helper answers; forge: "
        "a GitLab token the hub alone uses to drive merge requests, never leased to a run"
    )
    env_var: str | None = Field(
        None,
        max_length=MAX_ENV_VAR_CHARS,
        description="kind env only: the variable it sets, upper case, never one that steers the shell, git or the "
        "worker (PATH, HOME, EVO_*, GIT_* ...)",
    )
    url_prefix: str | None = Field(
        None,
        max_length=MAX_URL_PREFIX_CHARS,
        description="kinds git and forge: the https prefix of the origins it answers for, such as "
        "https://gitlab.example.org/group; kept as https://host/path, without .git or a trailing slash",
    )
    username: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_USERNAME_CHARS,
        pattern=LINE,
        description=f"kind git only: the user git sends with the value; {DEFAULT_GIT_USERNAME} when left out",
    )
    environment: Literal[SECRET_ENVIRONMENTS] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None,
        description="kind env only: the environment it is for; a run whose principal's K7 is auto:test_secrets is "
        "leased the env secrets labelled test alone (docs/autonomy.md)",
    )
    projects: list[Annotated[str, Field(pattern=PROJECT_NAME)]] = Field(
        default_factory=list,
        max_length=MAX_BINDINGS,
        description="the projects whose runs get it; the writer role on each, the admin role for kind forge; left out "
        "or empty for a personal secret, kept for its owner and leased to no run, which kind forge cannot be",
    )
    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] = Field(
        default_factory=list,
        max_length=MAX_BINDINGS,
        description="names of the caller's own workers that are not revoked; left out or empty for any of them; "
        "none for kind forge",
    )
    pools: list[PoolName] = Field(
        default_factory=list,
        max_length=MAX_BINDINGS,
        description="names of the caller's own worker pools: each node of each gets it, the nodes that join later "
        "too; never with workers, and none for kind forge",
    )
    expires_at: AwareDatetime | None = Field(
        None,
        description="after this no run gets it, and the hub no longer uses one of kind forge; in the future; "
        "required for kind forge",
    )
    oauth: OAuthTokens | None = Field(
        None,
        description=f"kind env, env_var {OAUTH_ENV_VAR} only: the account signed in with OAuth. The value is its "
        "access token; this holds the refresh token and when the access token ends, and the hub keeps all of them "
        "sealed. expires_at, when given, is that end",
    )
    value: SecretStr = Field(
        min_length=1,
        max_length=MAX_SECRET_BYTES,
        description=f"the secret itself, 1 to {MAX_SECRET_BYTES} bytes of UTF-8; written once, never shown again",
    )


class SecretRebind(BaseModel):
    """The binding a PATCH puts in place of the secret's: workers or pools, exactly one of them."""

    model_config = ConfigDict(extra="forbid")

    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] | None = Field(
        None,
        max_length=MAX_BINDINGS,
        description="names of the caller's own workers that are not revoked, in place of its binding; empty for any "
        "worker of the caller; never with pools",
    )
    pools: list[PoolName] | None = Field(
        None,
        max_length=MAX_BINDINGS,
        description="names of the caller's own worker pools, in place of its binding: each node of each gets it, the "
        "nodes that join later too; empty for any worker of the caller; never with workers",
    )


class Secret(BaseModel):
    """A secret as its owner sees it: everything but the value."""

    name: str
    kind: Literal[SECRET_KINDS]  # pyright: ignore[reportInvalidTypeForm]
    env_var: str | None = Field(description="kind env: the variable it sets")
    url_prefix: str | None = Field(description="kinds git and forge: the https prefix of the origins it answers for")
    username: str | None = Field(description="kind git: the user git sends with it")
    environment: str | None = Field(None, description="kind env: the environment it is labelled for (test)")
    projects: list[str] = Field(description="the projects whose runs get it; empty for a personal secret")
    workers: list[str] = Field(description="the workers it is bound to; empty for any worker of its owner")
    pools: list[str] = Field([], description="the worker pools it is bound to, each node of each")
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime = Field(description="when its value was last written")
    shares: list[CredentialShare] = Field(description="its shares not revoked, newest first")


class SecretWritten(Secret):
    created: bool = Field(description="false when a secret of this name was replaced")
