"""The shapes and the texts of the ruleset checks of the Curator's repos
(``evo_agents.hub.server.forge.service.protection`` says what they are): a repo with its last check, a project's
repos, the line a change or a check keeps as its reason, and the constants of the checks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import judge

PROTECTION = "curator.protection"  # a repo's ruleset was checked: "<project> repo=<name> protected=<bool>"
PROTECTION_DAYS = 2  # a ruleset check older than this no longer lets a Builder run
RECHECK_HOURS = 24  # the job checks a ruleset again once its last check is this old
WAITING_RECHECK = timedelta(hours=1)  # how often the job checks again a ruleset that did not pass a change waits for
MAX_CHECKS = 20  # rulesets the job checks in one pass, for each of its two kinds of check
GUIDE = 'docs/curator.md, under "Two GitHub Apps and the ruleset"'


class RepoCheck(BaseModel):
    """A repo of the project and the last check of its ruleset."""

    repo: str
    origin: str | None
    forge: Literal[judge.FORGES] | None = Field(description="null without an origin the Curator could push to")  # pyright: ignore[reportInvalidTypeForm]
    github_repo: str | None = Field(description="owner/name on GitHub; null elsewhere")
    protected: bool | None = Field(
        description="a ruleset keeps the Curator's App off its default branch; null: never checked"
    )
    default_branch: str | None
    reason: str | None
    rulesets: list[dict] = Field(default_factory=list, description="each {id, enforcement, can_bypass}")
    checked_at: datetime | None
    checked_by: str | None = Field(description="who asked; null for the hub's own check again")


class Protection(BaseModel):
    project: str
    repos: list[RepoCheck]


def reason_line(text: str | None) -> str | None:
    """``text`` as the one line a change or a check keeps as its reason, at most 2000 characters; None when empty."""
    if not text:
        return None
    flat = " ".join(text.split())
    return flat[:2000] or None
