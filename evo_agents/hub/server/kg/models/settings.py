"""The shapes of a project's KG settings (``evo_agents.hub.server.kg_settings``): what they are, a change of them, and
the measure of a candidate on the project's question set."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import kg_settings as store

MAX_REASON_CHARS = 1000
MAX_FIGURES_CHARS = store.MAX_FIGURES_CHARS


class ChangeOut(BaseModel):
    id: int
    kind: str
    changed_by: str | None
    run_id: int | None
    reason: str | None
    figures: dict | None
    before: dict
    after: dict
    undoes: int | None
    changed_at: datetime


class SettingsOut(BaseModel):
    values: dict = Field(description="every key with the value the project is tuned with")
    overrides: dict = Field(description="the keys that differ from the defaults")
    defaults: dict
    needs_rebuild: list[str] = Field(description="the keys changed by this call that act on the next build")
    changes: list[ChangeOut] = Field(description="the newest changes, newest first")
    can_change: bool


class SetBody(BaseModel):
    values: dict = Field(description="keys of the KG settings and their new values")
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS)
    figures: dict | None = Field(None, description="the numbers the change rests on, as the bench or measure gave them")


class UndoBody(BaseModel):
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS)


class MeasureBody(BaseModel):
    values: dict = Field(default_factory=dict, description="the candidate's keys, on top of the project's settings")
    split: Literal["tune", "holdout"] = Field("tune", description="the part of the question set that is asked")
    max_questions: int = Field(300, ge=1, le=1000)


class MeasureOut(BaseModel):
    build_id: int | None
    split: str
    questions: int
    current: dict | None = Field(description="the figures with the project's settings (n, hit@5, tokens per hit, p95)")
    candidate: dict | None = Field(description="the figures with the candidate's query keys on top")
    candidate_settings: dict
    needs_rebuild: list[str] = Field(description="keys of the candidate that need a new build and were not measured")
    by_type: dict | None = Field(
        None,
        description="per question type, {type: {current, candidate}}: the same figures; null when nothing was measured",
    )
    why: str | None = Field(None, description="why nothing was measured")
