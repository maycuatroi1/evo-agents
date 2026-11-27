"""The bodies of what the agent of a triage thread does on its own and what it answers (``triage.service.actions`` is
the design), and the texts of its ``Decision:`` lines."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import profiles, runs, triage_rules
from evo_agents.hub.plans import MAX_VALUE_CHARS
from evo_agents.hub.server.runs.models import AccountName
from evo_agents.hub.server.runs.models.placement import HubPlaced

REASON_CHARS = 600
OLD_VALUE_CHARS = 300  # how much of the old verify or what the Decision line keeps, to undo the edit by
OLD_VALUES_CHARS = 2400  # how much of them all it keeps when one call edits many steps, shared out among them
MAX_EDITS = 50  # steps one call may edit
STEP_REASON_CHARS = 300
Reason = Annotated[str, Field(min_length=1, max_length=REASON_CHARS, description="why, for the Decision: line")]


class RerunOptions(HubPlaced):
    runtime: str | None = Field(None, max_length=100, description="a runtime for the rerun")
    account: AccountName | None = Field(None, description="one of the owner's accounts")
    cause_confirmed: bool = Field(
        False, description="true after the agent read the run's log and the cause the hub guessed is the right one"
    )


class TriageRerunIn(RerunOptions):
    reason: Reason


class StepEdit(BaseModel):
    step: str = Field(min_length=1, max_length=100, description="the step's id, a step that is pending")
    verify: str | None = Field(None, max_length=MAX_VALUE_CHARS, description="the new verify: no command fewer")
    what: str | None = Field(None, max_length=MAX_VALUE_CHARS, description="the new what: shorter than the old one")
    why: str | None = Field(
        None, min_length=1, max_length=STEP_REASON_CHARS, description="why this step, when the call's reason is not all"
    )

    @model_validator(mode="after")
    def _something(self) -> StepEdit:
        if self.verify is None and self.what is None:
            raise ValueError(f"step {self.step}: give verify, what, or both")
        return self


class TriagePlanIn(RerunOptions):
    step: str | None = Field(
        None, min_length=1, max_length=100, description="the step's id, a step that is pending; or give steps"
    )
    verify: str | None = Field(None, max_length=MAX_VALUE_CHARS, description="the new verify: no command fewer")
    what: str | None = Field(None, max_length=MAX_VALUE_CHARS, description="the new what: shorter than the old one")
    steps: list[StepEdit] | None = Field(
        None,
        min_length=1,
        max_length=MAX_EDITS,
        description="several steps edited at once, in one revision and one Decision line, instead of step, verify "
        "and what",
    )
    if_revision: int = Field(ge=1, description="the revision of the plan the edit was made against")
    reason: Reason
    rerun: bool = Field(False, description="run the failed step again once the plan is edited, in the same step")

    @model_validator(mode="after")
    def _something(self) -> TriagePlanIn:
        if self.steps is None:
            if self.step is None:
                raise ValueError("give step, or steps")
            if self.verify is None and self.what is None:
                raise ValueError("give verify, what, or both")
            return self
        if self.step is not None or self.verify is not None or self.what is not None:
            raise ValueError("give steps, or step with verify and what, not both")
        keys = [edit.step for edit in self.steps]
        twice = sorted({key for key in keys if keys.count(key) > 1})
        if twice:
            raise ValueError(f"step {', '.join(twice)} is edited twice: give each step once")
        return self

    def edits(self) -> list[StepEdit]:
        """The steps the call edits, in its order."""
        if self.steps is not None:
            return self.steps
        return [StepEdit(step=self.step, verify=self.verify, what=self.what)]  # pyright: ignore[reportCallIssue]


class TriageInviteIn(BaseModel):
    handle: str = Field(
        min_length=1, max_length=100, pattern=profiles.HANDLE_PATTERN, description="the agent's handle, @ left out"
    )
    reason: Reason


class TriageInvited(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the agent's name")
    project: str = Field(description="the project of the agent invited")
    added: bool = Field(description="false when the agent was in the chat already")


class TriageCancelIn(BaseModel):
    reason: Reason


class TriageCancelled(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the agent's name")
    run_id: int = Field(description="the stale run whose worker was asked to stop it")


class TriageActed(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the agent's name")
    run_id: int | None = Field(description="the run queued again; null for a plan edit without a rerun")
    plan_revision: int | None = Field(description="the revision the edit wrote; null for a rerun alone")


def repos(failed) -> set[str]:
    """The repos failed run ``failed`` worked in: its repo and those its ``repos`` name."""
    named = {entry["repo"] for entry in failed.repos or () if isinstance(entry, dict) and entry.get("repo")}
    return named | ({failed.repo} if failed.repo else set())


def fields(edit: StepEdit) -> str:
    """The fields ``edit`` changes, ``verify and what`` for both."""
    return " and ".join(name for name in triage_rules.EDITABLE if getattr(edit, name) is not None)


def old_values(step: dict, edit: StepEdit, limit: int) -> str:
    """What the fields of ``step`` that ``edit`` changes said before, each clipped to ``limit``."""
    parts = []
    for name in triage_rules.EDITABLE:
        if getattr(edit, name) is not None:
            old = " ".join(str(step.get(name) or "").split())
            parts.append(f"old {name}: {runs.clip(old, limit) if old else 'none'}")
    return "; ".join(parts)


def decision(plan_id: str, was: int, now: int, edits: list[StepEdit], old: list[str], reason: str, again: str) -> str:
    """The ``Decision:`` line of a plan edit: one line for one step; for several, a line and then one item per step with
    what it said before, last, so a line the thread clips loses old values only."""
    undo = f"Undo: `evo-agents hub plan history` has revision {was}.{again}"
    if len(edits) == 1:
        (edit,) = edits
        return (
            f"Decision: changed {fields(edit)} of step {edit.step} of plan {plan_id} (revision {was} to {now}): "
            f"{reason} {edit.why + '; ' if edit.why else ''}{old[0]}. {undo}"
        )
    head = f"Decision: changed {len(edits)} steps of plan {plan_id} (revision {was} to {now}): {reason} {undo}"
    items = [f"- step {e.step}: {e.why + '; ' if e.why else ''}{said}" for e, said in zip(edits, old, strict=True)]
    return "\n".join([head, *items])
