"""A lease a run got, as its owner sees it (``evo_agents.hub.server.run_leases``): everything but the value."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub.credentials import HUB_KIND, LEASED_KINDS, PROVIDERS, RUN_ACCESS
from evo_agents.hub.gateway_runs import GATEWAY


class RunLease(BaseModel):
    """A lease a run got, as its owner sees it: everything but the value."""

    id: int
    name: str = Field(
        description="the secret's name, github-app:<account>, @<handle>:<name> for an account of an agent, "
        "gateway:<model> for a run's key of the model gateway, or the provider account's name that key reaches"
    )

    provider: Literal[(*PROVIDERS, GATEWAY, RUN_ACCESS)] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="secret: the owner's; github-app: a token the hub's App made; gateway: the model gateway's; "
        "run-access: the run's own token of the hub, as its owner (evo_agents.hub.server.run_access)"
    )
    kind: Literal[(*LEASED_KINDS, GATEWAY, HUB_KIND)] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="env: a variable of the agent; git: what git's helper answered; gateway: a run's key of the "
        "model gateway, or a provider account it reaches; hub: the run's token of the hub"
    )
    target: str = Field(
        description="kind env and hub: the variable it set; kind git: the origins of the run's repos it answered for, "
        "space-separated, in the form of normalize_origin; kind gateway: the model"
    )
    worker: str = Field(description="the worker it was leased to")
    issued_at: datetime
    expires_at: datetime | None = Field(description="when it stops working: a GitHub token's hour, a secret's end")
    revoked_at: datetime | None = Field(description="when the run gave it back or the hub took it; null while out")
    profile: str | None = Field(
        None, description="the handle of the agent profile it was handed out under; null for a lease before schema 0030"
    )
    profile_id: int | None = Field(None, description="that profile's id")
    profile_version: int | None = Field(None, description="the version of that profile")
