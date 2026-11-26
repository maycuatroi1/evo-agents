"""The bodies the agent of a proposal's thread posts to its routes (``routes.threads``, which holds the answers,
since they carry a proposal and a change), and the limits of the thread's first message."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import review
from evo_agents.hub.server.runs.models import LINE, MAX_ID

SKILL = "answering-curator-proposals"  # the skill of the evo-hub plugin the agent of the thread follows
SUMMARY_CHARS = 1200  # of the proposal's summary in the first message
SHOWN = 10  # paths, pieces of evidence and steps of the draft the first message names; it counts all


class CuratorAnswerIn(BaseModel):
    proposal_id: int = Field(ge=1, le=MAX_ID, description="the proposal of this thread")
    action: Literal[review.ANSWERS]  # pyright: ignore[reportInvalidTypeForm]
    note: str = Field(
        min_length=1,
        max_length=review.MAX_NOTE_CHARS,
        pattern=LINE,
        description="why, in one line: the ledger and the Decision: line keep it",
    )
    defer_days: int | None = Field(
        None, ge=review.DEFER_DAYS[0], le=review.DEFER_DAYS[1], description="with defer; 7 by default"
    )

    @model_validator(mode="after")
    def _days(self):
        if self.defer_days is not None and self.action != "defer":
            raise ValueError("defer_days goes with the action defer")
        return self


class CuratorMergeIn(BaseModel):
    change_id: int = Field(ge=1, le=MAX_ID, description="the change of this thread's proposal the hub left open")
