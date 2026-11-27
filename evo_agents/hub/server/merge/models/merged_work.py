"""The shapes of POST /v1/worker/merged (``service.merged_work``)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from evo_agents.hub.server.runs.models import MAX_ID

MAX_ASKED = 200  # run ids in one question


class MergedQuestion(BaseModel):
    runs: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        max_length=MAX_ASKED, description="ended runs of this worker whose directory and worktrees it still keeps"
    )


class MergedAnswer(BaseModel):
    merged: list[int] = Field(
        description="those of runs whose work the hub knows merged: the daemon may remove their worktrees now"
    )
