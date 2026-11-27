"""The shapes and the texts of the merge check of a pull request on github.com (``service.check``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub.server.runs.models import LINE

STATES = JUDGING, PASSED, FAILED = ("judging", "passed", "failed")
REBASE = "needs a rebase"
WAIT_HOURS = 24  # a wait whose verdict never came wakes then: asking again reads where the check stands
COMMAND = "evo-agents worker merge-check"


# Asking for it


class MergeCheckIn(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=LINE, description="the repo of the run's pull request")
    number: int | None = Field(None, ge=1, description="the pull request; null: the open one of the run's branch")
    branch: str | None = Field(
        None, min_length=1, max_length=255, pattern=LINE, description="the branch the run pushes, as its worker has it"
    )


class MergeCheck(BaseModel):
    """Where the merge check of a pull request stands, and the wait its verdict answers."""

    id: int
    repo: str
    number: int
    head_sha: str = Field(description="the head the verdict holds for; a new push needs a new check")
    state: str = Field(description="judging, passed or failed")
    reason: str | None
    web_url: str | None
    decision_id: int | None = Field(description="the run's wait the verdict answers; null when the verdict is there")
    wake_at: datetime | None = Field(description="when the wait wakes at the latest")


# The gate of the agent's merge command


class MergeGateIn(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=LINE, description="owner/name of the pull request's repo")
    number: int = Field(ge=1, description="the pull request the merge command merges")
    head: str | None = Field(
        None, description="the head of the pull request as the hook read it now; null when it could not"
    )


class MergeGate(BaseModel):
    gated: bool = Field(description="false: the repo does not have merge_check on, so the command is not the hub's")
    passed: bool = Field(description="the hub holds a passed verdict for the head named, and the command may go on")
    head_sha: str | None = Field(description="the head the hub's verdict holds for; the merge pins it")
    reason: str | None = Field(description="why the command does not pass")
    command: str = Field(description="the command that asks for the check")


# The agent of a held check's thread


class MergeTriageIn(BaseModel):
    action: Literal["rejudge", "return"] = Field(  # merge_holds.ACTIONS
        description="rejudge: judge it again from its first Judge, once the system is fixed; return: hand the failure "
        "back to the plan run, which caused it after all"
    )
    reason: str = Field(min_length=1, max_length=600, description="why, for the Decision: line")


class MergeTriaged(BaseModel):
    check_id: int
    state: str = Field(description="judging again after rejudge, failed after return")
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the agent's name")
