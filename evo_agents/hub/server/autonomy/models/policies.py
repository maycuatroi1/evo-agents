"""The shapes of a project's autonomy policy (``evo_agents.hub.server.autonomy.routes.policies``): one revision whole,
the next revision a writer sends, the list of revisions and the keys that differ between two of them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from evo_agents.hub import autonomy
from evo_agents.hub.server.base.models import StrictModel

MAX_REVISION = 2**31 - 1
MAX_AUDITED_KEYS_CHARS = 1000


class AutonomyPolicy(BaseModel):
    """One revision of a project's autonomy policy."""

    project: str
    revision: int = Field(description="0 while the project has none: every repo off, every key at its default")
    policy: dict[str, Any] = Field(description="the keys the revision sets, as its writer wrote them")
    effective: dict[str, Any] = Field(
        description="every key at the value the revision sets or its default, each repo of the project included"
    )
    note: str | None = Field(description="what the writer said of it")
    created_by: str | None
    created_at: datetime | None


class PolicyWritten(AutonomyPolicy):
    changed: bool = Field(description="false when the newest revision is this policy already: nothing was written")


class PolicyWrite(StrictModel):
    """The next revision of the policy: the whole of it, as its writer wants it."""

    policy: dict[str, Any] = Field(
        description="sections repos, merge, judge, breaker, audit, loop, context, routing; a key left out reads as its "
        "default"
    )
    note: str | None = Field(None, max_length=autonomy.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(None, ge=0, description="409 unless the policy is at this revision now")


class PolicyRevision(BaseModel):
    revision: int
    note: str | None
    created_by: str
    created_at: datetime
    changed: list[str] = Field(description="the dotted keys whose value differs from the revision before")


class PolicyChange(BaseModel):
    key: str = Field(description="dotted: merge.hold_until, repos.<repo>.mode, repos.<repo>.jobs.uat_check")
    before: Any
    after: Any


class PolicyComparison(BaseModel):
    project: str
    from_revision: int
    to_revision: int
    changes: list[PolicyChange] = Field(description="each key whose value differs, as the two revisions read now")
