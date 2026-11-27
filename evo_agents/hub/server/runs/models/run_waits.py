"""The shapes and limits of a wait of a plan run's agent (``evo_agents.hub.server.run_waits``)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import hub_events, runs
from evo_agents.hub.server import workflow_waits
from evo_agents.hub.server.decisions.models import DecisionAsk
from evo_agents.hub.server.runs.models import LINE

MAX_REASON_CHARS = 1000
MAX_CONTAINS_BYTES = 2048  # what a wait's event must contain, as JSON
PAYLOAD_IN_MESSAGE = 2048  # bytes of the event's payload the message that wakes the agent repeats
MAX_WAIT = timedelta(days=runs.MAX_WAIT_DAYS)
TEXT_KEYS = {  # keys of the payload of a kind that hold text
    hub_events.PLAN_STEP: ("plan", "plan_id", "step"),
    hub_events.WORKFLOW_RUN: workflow_waits.TEXT_KEYS,
}
GO_ON, STOP = "now", "stop"
OPTIONS = (
    (GO_ON, "Go on now", "What the run waits for came, or it need not wait: the agent goes on at once."),
    (STOP, "Stop waiting", "What the run waits for will not come: the agent goes on without it."),
)


class WaitIn(BaseModel):
    reason: str = Field(
        min_length=1, max_length=MAX_REASON_CHARS, description="what the run waits for and why, for the owner"
    )
    until: datetime | None = Field(
        None, description=f"when to wake at the latest, with a timezone, at most {runs.MAX_WAIT_DAYS} days ahead"
    )
    on: str | None = Field(
        None, min_length=1, max_length=120, description="a kind of hub event to wake on sooner, such as canvas.baseline"
    )
    contains: dict[str, Any] = Field(
        default_factory=dict, description="what the event's payload contains, as JSONB containment reads it"
    )
    step_key: str | None = Field(
        None, min_length=1, max_length=200, pattern=LINE, description="the step of the plan it is about"
    )

    @model_validator(mode="after")
    def _consistent(self):
        if not self.reason.strip():
            raise ValueError("a wait needs a reason")
        if self.until is None and self.on is None:
            raise ValueError("a wait wakes at a time (until), on an event of the hub (on), or both")
        if self.until is not None and self.until.utcoffset() is None:
            raise ValueError("until needs a timezone, such as 2026-10-30T09:00:00Z")
        if self.on is None and self.contains:
            raise ValueError("contains filters the event of on: name the kind with on")
        if self.on is not None and self.on not in hub_events.KINDS and not hub_events.OWN_KIND.fullmatch(self.on):
            kinds = ", ".join(hub_events.KINDS)
            raise ValueError(f"on is a kind of hub event: one of {kinds}, or plugin.<plugin>.<name>")
        for key in TEXT_KEYS.get(self.on, ()):  # pyright: ignore[reportArgumentType, reportCallIssue]
            value = self.contains.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                self.contains[key] = str(value)
        if len(json.dumps(self.contains, ensure_ascii=False).encode()) > MAX_CONTAINS_BYTES:
            raise ValueError(f"contains takes at most {MAX_CONTAINS_BYTES} bytes of JSON")
        return self


class WaitAsk(DecisionAsk):
    """The decision a wait opens: DecisionAsk, in the category the agent cannot name with `worker ask`."""

    category: Literal["wait"]  # runs.WAIT_DECISION_CATEGORY
