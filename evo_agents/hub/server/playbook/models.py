"""The bodies of the playbook's routes: an entry and the playbook as a reader sees it, with the refusals of the
routes. The delta the agent of a review run writes is the service's (``service.PlaybookDelta``): its evidence is a
proposal's, whose module reaches the service."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from evo_agents.hub.server.errors import ErrorBody

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}


class PlaybookEntry(BaseModel):
    id: int
    ref: str = Field(description="pb-<id>, as the prompt and the command line write it")
    lens: str | None
    text: str
    evidence: list[dict]
    helpful: int
    harmful: int
    created_run_id: int | None
    updated_run_id: int | None
    retired: bool
    retired_note: str | None
    until_proposal_id: int | None = Field(None, description="the proposal it holds until; its merge retires it")
    prompt_exposures: int = Field(0, description="review runs that showed it in their prompt and are done")
    created_at: datetime
    updated_at: datetime


class PlaybookView(BaseModel):
    project: str
    entries: list[PlaybookEntry] = Field(description="the most helpful less harmful first")
    live: int = Field(description="live entries the caller may read")
    retired: int = Field(description="retired entries the caller may read")
