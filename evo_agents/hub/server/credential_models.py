"""The answer bodies of the credential routes (``evo_agents.hub.server.credentials``), which imports them back."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evo_agents.hub.credentials import HUB_KIND, HUB_TOKEN_ENV, LEASED_KINDS, PROVIDERS, RUN_ACCESS


class CredentialLease(BaseModel):
    """One credential a run holds, as ``evo_agents.hub.credentials.Lease`` reads it."""

    id: int = Field(description="the lease; the same id when it is asked for again")
    kind: Literal[LEASED_KINDS] = Field(description="env: a variable of the agent; git: what git's helper answers")
    provider: Literal[PROVIDERS] = Field(description="secret: the owner's; github-app: a token the hub's App made")
    name: str = Field(description="the secret's name, or github-app:<account>")
    env_var: str | None = Field(description="kind env: the variable it sets")
    url_prefix: str | None = Field(description="kind git: the https prefix of the origins it answers for")
    username: str | None = Field(description="kind git: the user git sends with the value")
    value: str = Field(description="the credential itself: for memory only, never a file, a log line or an event")
    expires_at: datetime | None = Field(description="when it stops working; ask again before, for a GitHub token")


class GatewayLease(BaseModel):
    """The lease of a run on a provider account's model (``evo_agents.hub.gateway_runs``): the hub's gateway route
    and a token of the run for it; never a provider's key nor the gateway's."""

    id: int = Field(description="the lease of the run's key of the gateway; the same id when it is asked for again")
    kind: Literal["gateway"]
    provider: Literal["gateway"]
    name: str = Field(description="gateway:<prefix>, the prefix of the run's model")
    env_var: None = None
    url_prefix: None = None
    username: None = None
    value: str = Field(
        description="the run's token to the route: for memory only, never a file, a log line or an event"
    )
    expires_at: datetime | None = Field(description="when the run's key of the gateway ends")
    base_url: str = Field(description="the hub's gateway route, <hub>/v1, that the runtime sends its model calls to")
    model: str = Field(description="the run's model, <prefix>/<model of the provider>")
    api_format: Literal["anthropic", "openai", "openai-responses"] = Field(description="what the runtime speaks")


class HubLease(BaseModel):
    """The lease of a run its owner lets run as them (``evo_agents.hub.server.run_access``): the run's own token of the
    hub, which calls the hub's API as the owner, and the hub's URL. A worker that knows no kind hub leaves it unused."""

    id: int = Field(description="the lease of the run's token; the same id, and token, when it is asked for again")
    kind: Literal[HUB_KIND]
    provider: Literal[RUN_ACCESS]
    name: str = Field(description="run-access:<owner>, the login the token acts as")
    env_var: Literal[HUB_TOKEN_ENV] = Field(description="the variable that carries the token")
    url_prefix: None = None
    username: None = None
    value: str = Field(description="the run's token, evr_...: for memory only, never a file, a log line or an event")
    expires_at: None = Field(None, description="none: the token ends with the lease, when the run is no longer held")
    base_url: str = Field(description="the hub's URL, which the token is for")
    model: None = None
    api_format: None = None


class MissingOrigin(BaseModel):
    """A repo of the run whose origin no lease covers: git uses the machine's own credentials for it."""

    repo: str
    origin: str | None = Field(description="as the project registered it; null when it registered none")
    reason: str


class RunRepoSource(BaseModel):
    """A repo of the run and where it comes from (``repo_origins``), so a worker without a checkout of it clones it."""

    repo: str
    origin: str | None = Field(description="as a project registered it; null when none did")
    path: str | None = Field(description="as that project registered it: relative to its workspace, ~/..., or absolute")
    project: str | None = Field(description="the project that registered it: the run's, or another of its owner's")


class Credentials(BaseModel):
    leases: list[Annotated[CredentialLease | GatewayLease | HubLease, Field(discriminator="kind")]]
    missing: list[MissingOrigin]
    repos: list[RunRepoSource] = Field(default_factory=list, description="each repo of the run, in the run's order")


class GivenBack(BaseModel):
    revoked: int = Field(description="leases of the run this worker gave back now; 0 when none was out any more")
