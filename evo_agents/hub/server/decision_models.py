"""The models of decisions (``evo_agents.hub.server.decisions`` is the routes and the design): what an agent asks, what
every view of a decision shows, and an answer.

Besides its question and options, a question may carry why the agent recommends its option (``recommended_reason``,
only with ``recommended``), whether the act it asks about can be undone (``reversible``) and how (``undo``, only when
it can), and the refs of what it rests on (``evidence``, each in a form of ``evo_agents.hub.decision_asks``); a ref in
no such form is 422. A decision asked without them shows null and an empty list.

A decision of category credential may carry a request (``credential_request``, ``evo_agents.hub.credential_requests``):
the variable the run needs, the name of the secret that keeps its value and whether it asks for the owner's account on
the hub. Such a decision takes no options of the agent's: the hub gives it those of ``credential_requests.options``.
The answer to it may carry the variable's value (``AnswerIn.secret``), which no view of the decision, no error and no
message ever shows.

A decision of category merge may carry the approval it asks for (``approval``, ``evo_agents.hub.approval_heads``):
the pull request, its head and the hash of its scope. Its answer opens the gate of the agent's merge for that head and
scope alone; the person asked reads what it is bound to under the context.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from evo_agents.hub import approval_heads, credential_requests, decision_asks, outputs, runs
from evo_agents.hub.credentials import MAX_SECRET_BYTES
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


class CredentialRequest(BaseModel):
    """What a decision of category credential asks the run's owner for; never a value."""

    env_var: str | None = Field(
        None,
        min_length=1,
        max_length=credential_requests.MAX_ENV_VAR_CHARS,
        description="the variable of the agent's environment the run needs, such as an API key; never one that steers "
        "the shell, git or the worker (EVO_*, GIT_* ...)",
    )
    secret_name: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="the name of the owner's secret that keeps its value; the variable in lower case with - for _ "
        "when left out",
    )
    hub_account: bool = Field(False, description="whether the run asks to run as its owner on the hub")

    @model_validator(mode="after")
    def _askable(self):
        refusal = credential_requests.request_refusal(self.env_var, self.secret_name, self.hub_account)
        if refusal is not None:
            raise ValueError(refusal)
        if self.env_var is not None and self.secret_name is None:
            self.secret_name = credential_requests.default_secret_name(self.env_var)
        return self


class Approval(BaseModel):
    """What a merge approval holds for: one head of one request, with one scope."""

    pull: str | None = Field(
        None,
        max_length=300,
        pattern=approval_heads.PULL.pattern,
        description="the request: owner/name#number on GitHub, the repo and !iid of a merge request on GitLab",
    )
    head: str = Field(pattern=approval_heads.HEAD.pattern, description="the commit at its head, lower-case hex")
    scope: str | None = Field(
        None,
        pattern=approval_heads.SCOPE.pattern,
        description="sha256: and the hash of its files, migrations and needs-you lines (approval_heads.scope_of)",
    )


MERGE_CATEGORY = "merge"


class DecisionAsk(BaseModel):
    category: Literal[runs.ASKED_DECISION_CATEGORIES] = Field(description="the agent asks only about these")
    question: str = Field(min_length=1, max_length=runs.MAX_QUESTION_CHARS)
    context: str | None = Field(
        None, min_length=1, max_length=runs.MAX_DECISION_CONTEXT_BYTES, description="markdown, at most 16 KiB of UTF-8"
    )
    options: list[OptionIn] = Field(
        default_factory=list,
        max_length=runs.DECISION_OPTIONS[1],
        description=f"{runs.DECISION_OPTIONS[0]} to {runs.DECISION_OPTIONS[1]}; none for a credential request, which "
        "takes the hub's",
    )
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
    credential_request: CredentialRequest | None = Field(
        None,
        description="category credential only: what the run needs of its owner, a variable's value, their hub account "
        "or both; the hub gives the decision its options",
    )
    approval: Approval | None = Field(
        None,
        description="category merge only: the pull request, head and scope its answer holds for; a push that moves "
        "the head or changes the scope leaves the answer behind",
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
        self._requested_options()
        if self.approval is not None and self.category != MERGE_CATEGORY:
            raise ValueError(f"an approval binds a decision of category {MERGE_CATEGORY}")
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

    def _requested_options(self) -> None:
        """A credential request gets the hub's options; any other decision brings 2 to 6 of its own."""
        request = self.credential_request
        if request is None:
            fewest, most = runs.DECISION_OPTIONS
            if not fewest <= len(self.options) <= most:
                raise ValueError(f"a decision offers {fewest} to {most} options")
            return
        if self.category != credential_requests.CATEGORY:
            raise ValueError(f"a credential request is a decision of category {credential_requests.CATEGORY}")
        if self.options:
            raise ValueError("a credential request takes the hub's options: leave out options")
        given = credential_requests.options(request.env_var, request.secret_name, request.hub_account)
        self.options = [OptionIn(**option) for option in given]

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
        """The columns of the decision's reason, reversibility, evidence and credential request: NULL for what the
        agent left out."""
        request = self.credential_request
        return {
            "recommended_reason": self.recommended_reason,
            "reversible": self.reversible,
            "undo": self.undo,
            "evidence": self.evidence or None,
            "credential_request": None if request is None else request.model_dump(),
            "approval": None if self.approval is None else self.approval.model_dump(),
        }

    def stored_context(self) -> str | None:
        """The context the person asked reads: the agent's, and under it what an approval is bound to."""
        bound = approval_heads.line(None if self.approval is None else self.approval.model_dump())
        if not bound:
            return self.context
        return runs.clip(f"{self.context}\n\n{bound}" if self.context else bound, runs.MAX_DECISION_CONTEXT_BYTES)


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
    credential_request: CredentialRequest | None = Field(
        None,
        description="what a decision of category credential asks the run's owner for: a variable's value, kept as "
        "their secret secret_name, their hub account, or both; null for any other decision",
    )
    approval: Approval | None = Field(
        None,
        description="a merge approval: the request, head and scope its answer holds for; null for any other decision, "
        "and for one asked before schema 0184",
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
    secret: SecretStr | None = Field(
        None,
        min_length=1,
        max_length=MAX_SECRET_BYTES,
        description="a credential request's variable: its value, which the hub keeps as the owner's secret of kind "
        "env for the run's project and never shows again; with option value or both, which it stands for when left "
        "out",
    )

    @model_validator(mode="after")
    def _something(self):
        if self.text is not None and not self.text.strip():
            raise ValueError("an answer's text needs some words")
        if self.text is not None and len(self.text.encode()) > runs.MAX_ANSWER_BYTES:
            raise ValueError(f"an answer's text is at most {runs.MAX_ANSWER_BYTES} bytes of UTF-8")
        if self.option is None and self.text is None and self.secret is None:
            raise ValueError("an answer names an option, gives text, or both")
        return self
