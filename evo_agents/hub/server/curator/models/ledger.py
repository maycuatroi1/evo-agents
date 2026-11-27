"""The shapes of a proposal's ledger (``evo_agents.hub.server.ledger``) and what a line keeps of a commit."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import ledger

SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class LedgerLine(BaseModel):
    """One line of a proposal's ledger, as the hub added it."""

    id: int
    proposal_id: int
    change_id: int | None = Field(description="the change it became, once it became one")
    action: Literal[ledger.ACTIONS]  # pyright: ignore[reportInvalidTypeForm]
    actor: Literal[ledger.ACTORS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="curator: the hub's own code; agent: an agent of a run of the Curator; user: a member"
    )
    actor_login: str | None = Field(description="the member, for a line of a user the hub knows")
    run_id: int | None = Field(description="the run of the agent: the review run, the Builder or the Judge")
    what: str = Field(description="what happened, in words")
    commit_sha: str | None = Field(description="the commit it is about: the head built, judged or merged")
    before_sha: str | None = Field(description="the default branch before the change")
    after_sha: str | None = Field(description="the default branch after it: the merge commit")
    figures: dict | None = Field(
        description="proposed: the figures that set it off; outcome: those figures before and after the merge"
    )
    verdict: dict | None = Field(description="judged: whether it passed, the agent's verdict, the failures")
    pr_url: str | None
    pr_number: int | None
    merged_at: datetime | None
    outcome: Literal[ledger.OUTCOMES] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="outcome: keep, revert or unclear"
    )
    details: dict | None
    created_at: datetime


class Ledger(BaseModel):
    project: str
    proposal_id: int
    lines: list[LedgerLine] = Field(description="oldest first")
    outcome_due_at: datetime | None = Field(
        description="when the hub counts the figures of its merged change again; null before the merge and once "
        "its outcome is in"
    )
