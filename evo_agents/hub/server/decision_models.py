"""The models of decisions (``evo_agents.hub.server.decisions`` is the routes and the design): what an agent asks, what
every view of a decision shows, and an answer.

Besides its question and options, a question may carry why the agent recommends its option (``recommended_reason``,
only with ``recommended``), whether the act it asks about can be undone (``reversible``) and how (``undo``, only when
it can), and the refs of what it rests on (``evidence``, each in a form of ``evo_agents.hub.decision_asks``); a ref in
no such form is 422. A decision asked without them shows null and an empty list.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from evo_agents.hub import decision_asks, outputs, runs
from evo_agents.hub.server.output_views import RunOutput
from evo_agents.hub.server.runs.models import LINE, MAX_ID

LABEL_CHARS = 200  # an option's label, one line
DESCRIPTION_CHARS = 1000  # an option's description
MAX_OPTIONS_BYTES = 16 * 1024  # all the options of a decision as JSON, as schema 0010 bounds them

OptionKey = Annotated[str, Field(pattern=runs.OPTION_KEY, description="letters, digits, _ and -, at most 32")]
EvidenceRef = Annotated[str, Field(min_length=1, max_length=decision_asks.MAX_REF_CHARS)]
REASON = (
    f"why the agent recommends that option, at most {decision_asks.MAX_REASON_CHARS} characters; only with recommended"
)
REVERSIBLE = "for an act: whether it can be undone; null when the question asks about no act, or nobody said"
UNDO = f"how to undo the act, at most {decision_asks.MAX_UNDO_CHARS} characters; only with reversible true"
EVIDENCE = f"what the question rests on, at most {decision_asks.MAX_EVIDENCE} refs, each one of {decision_asks.FORMS}"


class OptionIn(BaseModel):
    key: OptionKey
    label: str = Field(min_length=1, max_length=LABEL_CHARS, pattern=LINE, description="one line")
    description: str | None = Field(None, min_length=1, max_length=DESCRIPTION_CHARS)
    waits: bool | None = Field(
        None,
        description="true when the option says not yet: choosing it leaves the decision open and the run waiting, and "
        "no answer goes to the agent",
    )


class DecisionAsk(BaseModel):
    category: Literal[runs.ASKED_DECISION_CATEGORIES] = Field(description="the agent asks only about these")
    question: str = Field(min_length=1, max_length=runs.MAX_QUESTION_CHARS)
    context: str | None = Field(
        None, min_length=1, max_length=runs.MAX_DECISION_CONTEXT_BYTES, description="markdown, at most 16 KiB of UTF-8"
    )
    options: list[OptionIn] = Field(min_length=runs.DECISION_OPTIONS[0], max_length=runs.DECISION_OPTIONS[1])
    recommended: OptionKey | None = Field(None, description="the key of the option the agent recommends")
    recommended_reason: str | None = Field(
        None, min_length=1, max_length=decision_asks.MAX_REASON_CHARS, description=REASON
    )
    reversible: bool | None = Field(None, description=REVERSIBLE)
    undo: str | None = Field(None, min_length=1, max_length=decision_asks.MAX_UNDO_CHARS, description=UNDO)
    evidence: list[EvidenceRef] = Field(
        default_factory=list, max_length=decision_asks.MAX_EVIDENCE, description=EVIDENCE
    )
    step_key: str | None = Field(
        None, min_length=1, max_length=200, pattern=LINE, description="the step of the plan it is about"
    )
    output_ids: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        default_factory=list,
        max_length=outputs.MAX_ATTACHED,
        description="outputs of the run the question carries, such as the screenshot it is about",
    )

    @field_validator("evidence")
    @classmethod
    def _refs(cls, value: list[str]) -> list[str]:
        return [decision_asks.evidence_ref(ref) for ref in value]

    @model_validator(mode="after")
    def _consistent(self):
        if not self.question.strip():
            raise ValueError("a decision needs a question")
        if self.context is not None and len(self.context.encode()) > runs.MAX_DECISION_CONTEXT_BYTES:
            raise ValueError(f"a decision's context is at most {runs.MAX_DECISION_CONTEXT_BYTES} bytes of UTF-8")
        keys = [option.key for option in self.options]
        if len(set(keys)) != len(keys):
            raise ValueError("each option of a decision needs a key of its own")
        if self.recommended is not None and self.recommended not in keys:
            raise ValueError(f"the recommended option {self.recommended!r} is not one of {', '.join(keys)}")
        self._extras_consistent()
        if all(option.waits for option in self.options):
            raise ValueError("a decision needs an option that answers it: every option of this one waits")
        if len(json.dumps(self.stored_options(), ensure_ascii=False).encode()) > MAX_OPTIONS_BYTES:
            raise ValueError(f"the options of a decision take at most {MAX_OPTIONS_BYTES} bytes of JSON")
        return self

    def _extras_consistent(self) -> None:
        if self.recommended_reason is not None:
            if self.recommended is None:
                raise ValueError("recommended_reason says why the recommended option: name it with recommended")
            if not self.recommended_reason.strip():
                raise ValueError("recommended_reason needs some words")
        if self.undo is not None:
            if self.reversible is not True:
                raise ValueError("undo says how to undo the act: it goes with reversible true")
            if not self.undo.strip():
                raise ValueError("undo needs some words")

    def stored_options(self) -> list[dict]:
        stored = []
        for option in self.options:
            entry = {"key": option.key, "label": option.label}
            if option.description is not None:
                entry["description"] = option.description
            if option.key == self.recommended:
                entry["recommended"] = True
            if option.waits:
                entry["waits"] = True
            stored.append(entry)
        return stored

    def stored_extras(self) -> dict:
        """The columns of the decision's reason, reversibility and evidence: NULL for what the agent left out."""
        return {
            "recommended_reason": self.recommended_reason,
            "reversible": self.reversible,
            "undo": self.undo,
            "evidence": self.evidence or None,
        }


class DecisionOption(BaseModel):
    key: str
    label: str
    description: str | None
    recommended: bool
    waits: bool = Field(False, description="choosing it leaves the decision open: the person answers later")


class WakeOn(BaseModel):
    kind: str = Field(description="the kind of hub event the wait wakes on, such as canvas.baseline")
    contains: dict = Field(description="what the event's payload contains, as JSONB containment reads it")


class Decision(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the plan run or author run the decision belongs to")
    run_state: Literal[runs.SHOWN_STATES]
    plan_id: str | None = Field(
        description="the run's plan when its agent asked; null for an author run that had put no plan yet"
    )
    step_key: str | None = Field(description="the step it is about, when the agent named one")
    category: Literal[runs.SHOWN_DECISION_CATEGORIES]
    question: str
    context: str | None = Field(description="markdown")
    options: list[DecisionOption]
    recommended: str | None = Field(description="the key of the option the agent recommends")
    recommended_reason: str | None = Field(None, description="why the agent recommends that option")
    reversible: bool | None = Field(None, description=REVERSIBLE)
    undo: str | None = Field(None, description="how to undo the act, when it can be")
    evidence: list[str] = Field(
        default_factory=list, description=f"what the question rests on, as refs: {decision_asks.FORMS}"
    )
    state: Literal[runs.DECISION_STATES]
    owner: str = Field(description="the login of the run's owner")
    asked_of: str = Field(
        description="the login of the person asked, who answers it: the principal of its run, or the person an intern "
        "reports to (after 24 hours the leads of the intern's unit too), or a hub-made run's sponsor; the owner for a "
        "decision asked before schema 0115"
    )
    answer_option: str | None
    answer_text: str | None
    answered_by: str | None
    answer_run_id: int | None = Field(
        description="the run whose inbox the answer went to: the decision's run, or the run that resumed it"
    )
    asked_at: datetime
    answered_at: datetime | None
    delivered_at: datetime | None = Field(description="when the worker handed the answer to the agent")
    held_at: datetime | None = Field(
        None, description="when the person asked last chose an option that waits, leaving the decision open"
    )
    wake_at: datetime | None = Field(
        None, description="a wait (category wait): when the hub answers it at the latest, its time come"
    )
    wake_on: WakeOn | None = Field(None, description="a wait: the event of the hub that answers it sooner")
    outputs: list[RunOutput] = Field(
        default_factory=list,
        description="the files of its run it carries, oldest first, each with the decision's URL of its bytes",
    )


class DecisionList(BaseModel):
    decisions: list[Decision] = Field(description="newest first")
    total: int = Field(description="decisions that match the filters")
    limit: int
    offset: int


class AnswerIn(BaseModel):
    option: OptionKey | None = Field(None, description="the key of one of the decision's options")
    text: str | None = Field(
        None, min_length=1, max_length=runs.MAX_ANSWER_BYTES, description="the owner's own words, at most 4 KiB"
    )

    @model_validator(mode="after")
    def _something(self):
        if self.text is not None and not self.text.strip():
            raise ValueError("an answer's text needs some words")
        if self.text is not None and len(self.text.encode()) > runs.MAX_ANSWER_BYTES:
            raise ValueError(f"an answer's text is at most {runs.MAX_ANSWER_BYTES} bytes of UTF-8")
        if self.option is None and self.text is None:
            raise ValueError("an answer names an option, gives text, or both")
        return self
