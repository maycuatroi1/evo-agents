"""The shapes of the weeks of confirmed answers loaded into prompts (``evo_agents.hub.server.kg.service.answer_reuse``)
and the names of the daily signal."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel

SIGNAL_KEY = "context.answer_reuse"
SIGNAL_SOURCE = "context-answers"
INGEST_KEY = "context-answers"  # the project's ingest key the signal comes through, made on first use
DEFAULT_WEEKS = 8
MAX_WEEKS = 52


class ContextAnswerWeek(BaseModel):
    start: AwareDatetime
    runs: int
    runs_with_loads: int
    answers: int


class ContextAnswerWeeks(BaseModel):
    project: str
    enabled: bool
    since: AwareDatetime
    until: AwareDatetime
    weeks: list[ContextAnswerWeek]
