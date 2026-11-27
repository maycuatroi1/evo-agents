"""The shapes of the branch a release run's worker reports it pushed (``routes.claims``)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from evo_agents.hub import releases
from evo_agents.hub.server.runs.models import OBJECT_NAME


class ReleasePushed(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    branch: str = Field(min_length=1, max_length=255, description="release/<version>")
    head: str = Field(pattern=OBJECT_NAME, description="the commit the branch has now")
    version: str = Field(pattern=releases.VERSION.pattern, description="the version the agent picked, X.Y.Z")
    decision: str | None = Field(None, max_length=releases.MAX_REASON_CHARS, description="the agent's Decision line")


class ReleaseBranch(BaseModel):
    run_id: int
    cycle_id: int
    repo: str
    branch: str
    head: str
    version: str
