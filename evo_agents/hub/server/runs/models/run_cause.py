"""The shape of a cause restored (``evo_agents.hub.server.run_cause``) and the keys of the run events that a plugin's
classification of a failed run's cause, or its undoing, writes."""

from __future__ import annotations

from pydantic import BaseModel, Field

CAUSE_KEY = "cause_classification"  # the key of a run event that a plugin's classification, or its undoing, writes
CLASSIFIED = "classified"
RESTORED = "restored"


class CauseRestored(BaseModel):
    run_id: int
    failure_cause: str | None = Field(description="the cause the run has now: the one the hub had guessed")
    was: str | None = Field(description="the cause the plugin had given it")
