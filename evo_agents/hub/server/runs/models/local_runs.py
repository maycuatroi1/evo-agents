"""The shapes and bounds of local runs (``evo_agents.hub.server.local_runs``): what the machine registers and reports,
and a decision its session asks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from evo_agents.hub import runs
from evo_agents.hub.plans import PLAN_ID
from evo_agents.hub.server.decisions.models import DecisionAsk
from evo_agents.hub.server.runs.models.limits import LINE

MAX_WAIT_SECONDS = 25  # the longest a long-poll for an answer holds
POLL_SECONDS = 5.0  # a long-poll looks again this often, wakeup or not
MAX_TITLE_CHARS = 200  # runs.title


def one_line(value: str | None) -> str | None:
    """``value`` as one trimmed line: 422 for a blank one or one with a control character."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("this needs some text")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError("this is one line: no control characters")
    return value


class LocalRegister(BaseModel):
    machine: str = Field(min_length=1, max_length=runs.MAX_LOCAL_FIELD, description="the machine's name")
    cwd: str = Field(min_length=1, max_length=runs.MAX_LOCAL_FIELD, description="the session's working directory")
    claude_email: str = Field(
        "unknown",
        min_length=1,
        max_length=runs.MAX_LOCAL_FIELD,
        description="the Claude account the session runs as; 'unknown' when the machine could not read it",
    )
    claude_profile: str | None = Field(
        None, min_length=1, max_length=runs.MAX_LOCAL_FIELD, description="the claude-profiles profile, when known"
    )
    title: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_TITLE_CHARS,
        description="what the session is about; the machine and directory when left out",
    )

    _lines = field_validator("machine", "cwd", "claude_email", "claude_profile", "title")(one_line)


class LocalState(BaseModel):
    state: Literal[runs.LOCAL_STATES] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None,
        description="running: a turn is under way; waiting: the agent waits for a decision; idle: the turn ended and "
        "the person has the word; ended: the session is over",
    )
    plan_id: str | None = Field(None, pattern=PLAN_ID, description="the plan the session works on, as the hub holds it")
    step_key: str | None = Field(
        None, min_length=1, max_length=200, pattern=LINE, description="the step of that plan it works on"
    )
    title: str | None = Field(None, min_length=1, max_length=MAX_TITLE_CHARS)

    _lines = field_validator("title")(one_line)

    @model_validator(mode="after")
    def _plan_first(self):
        if self.step_key is not None and self.plan_id is None:
            raise ValueError("a step belongs to a plan: give plan_id with step_key")
        return self


class LocalDecisionAsk(DecisionAsk):
    category: Literal[runs.LOCAL_DECISION_CATEGORIES] = Field(  # pyright: ignore[reportInvalidTypeForm]
        runs.LOCAL_DECISION_CATEGORY, description="a question of the person fits 'question'"
    )
