"""The body of ``work set`` (``WorkSetIn``), the fields it names and their columns, the text of the card's message
and the refusals of setting the work and marking it handled."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import system_context
from evo_agents.hub.server.work_status_models import Outcome

TITLE_CHARS = 200
CARD_TEXT = "Work card: {title}"
NO_WORK = (
    "run {run} is in no trace of an agent system with a work chat: `work set` names the work of a trace that handed "
    "work over"
)
NOT_COORDINATOR = "only the system's entry agent, which coordinates the trace, sets its work; @{handle} does not"
NO_WORK_CHAT = "chat {chat} is no work chat"


class WorkSetIn(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=TITLE_CHARS, description="a short name of the work")
    goal: str | None = Field(None, min_length=1, max_length=system_context.BRIEF_FIELD_CHARS)
    done_when: str | None = Field(None, min_length=1, max_length=system_context.BRIEF_FIELD_CHARS)
    outcome: Outcome | None = Field(
        None, description="met or not_met: whether the work reached its done-when, recorded before the final answer"
    )
    left: str | None = Field(
        None,
        min_length=1,
        max_length=system_context.BRIEF_FIELD_CHARS,
        description="what is still to do when the done-when is not met",
    )

    @model_validator(mode="after")
    def _something(self) -> WorkSetIn:
        if all(getattr(self, name) is None for name in FIELDS):
            raise ValueError(GIVE)
        return self


FIELDS = ("title", "goal", "done_when", "outcome", "left")
GIVE = "give --title, --goal, --done-when, --outcome or --left"
COLUMNS = {"left": "outcome_left"}  # a field of WorkSetIn: its column of system_traces, where the name differs
NOT_WRITER = (
    "marking the work of chat {chat} handled needs the writer role on project {project}, whose system's work it is; "
    "you hold {role}"
)
