"""The bodies of the skill routes, the place of a skill, its audiences, the audit actions and the limits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub.server.signals.models import PRINTABLE  # the regex of projects.PRINTABLE, in a leaf module
from evo_agents.hub.skills import MAX_BUNDLE

PUBLISH = "skill.publish"  # audit action; the target is skill:global/<name> or skill:project/<project>/<name>
AUDIENCE = "skill.audience"  # audit action; the target is skill:global/<name> audience=<was>-><is>
SCOPES = ("global", "project")
AUDIENCES = ("everyone", "admins")  # who reads a global skill: every signed-in member, or hub admins alone
EVERYONE, ADMINS = AUDIENCES
MAX_SOURCE_REPO = 200
SKILL_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"  # evo_agents.hub.skills.NAME
UNAVAILABLE = "the blob store did not answer; no version was created, try again shortly"
AUDIENCE_TEXT = "who reads a global skill: every signed-in member, or hub admins alone; a project skill is everyone's"


class PublishRequest(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="the bundle, committed to the blob store already")
    size: int = Field(ge=1, le=MAX_BUNDLE, description="bytes of the bundle")
    source_repo: str | None = Field(None, min_length=1, max_length=MAX_SOURCE_REPO, pattern=PRINTABLE)
    source_commit: str | None = Field(None, pattern=r"^[0-9a-f]{7,64}$", description="hex, abbreviated or whole")
    audience: Literal[AUDIENCES] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None, description="a global skill's audience: default everyone for a new skill, unchanged for one that exists"
    )

    @model_validator(mode="after")
    def _source_whole(self):
        if (self.source_repo is None) != (self.source_commit is None):
            raise ValueError("source_repo and source_commit go together")
        return self


class Version(BaseModel):
    version: int
    description: str = Field(description="from the frontmatter of SKILL.md")
    sha256: str
    size: int
    source_repo: str | None
    source_commit: str | None
    published_by: str
    published_at: datetime


class Skill(BaseModel):
    """A skill with its latest version."""

    scope: Literal[SCOPES]  # pyright: ignore[reportInvalidTypeForm]
    project: str | None
    name: str
    version: int
    description: str
    sha256: str
    size: int
    source_repo: str | None
    source_commit: str | None
    published_by: str
    published_at: datetime
    audience: Literal[AUDIENCES] = Field(description=AUDIENCE_TEXT)  # pyright: ignore[reportInvalidTypeForm]


class SkillHistory(BaseModel):
    scope: Literal[SCOPES]  # pyright: ignore[reportInvalidTypeForm]
    project: str | None
    name: str
    created_at: datetime
    versions: list[Version] = Field(description="the latest first")
    audience: Literal[AUDIENCES] = Field(description=AUDIENCE_TEXT)  # pyright: ignore[reportInvalidTypeForm]


class Published(BaseModel):
    scope: Literal[SCOPES]  # pyright: ignore[reportInvalidTypeForm]
    project: str | None
    name: str
    created: bool = Field(description="false when the latest version has this bundle already")
    latest: Version
    audience: Literal[AUDIENCES] = Field(description=AUDIENCE_TEXT)  # pyright: ignore[reportInvalidTypeForm]


class AudienceChange(BaseModel):
    audience: Literal[AUDIENCES] = Field(description=AUDIENCE_TEXT)  # pyright: ignore[reportInvalidTypeForm]


class SkillAudience(BaseModel):
    name: str
    audience: Literal[AUDIENCES] = Field(description=AUDIENCE_TEXT)  # pyright: ignore[reportInvalidTypeForm]
    changed: bool = Field(description="false when the skill had this audience already")


class BundleTicket(BaseModel):
    scope: Literal[SCOPES]  # pyright: ignore[reportInvalidTypeForm]
    project: str | None
    name: str
    version: int
    sha256: str = Field(description="what the downloaded bytes must hash to")
    size: int
    url: str = Field(description="presigned GET of the bundle; a bearer credential until it expires")
    expires_at: datetime


@dataclass(frozen=True)
class Place:
    """Where a skill lives: global, or a project the caller has a grant on; with the audiences the caller reads there."""

    scope: str
    project_id: int | None
    project: str | None
    audiences: tuple[str, ...] = AUDIENCES

    def target(self, name: str) -> str:
        """The skill ``name`` here, as audit rows name it."""
        return f"skill:global/{name}" if self.project is None else f"skill:project/{self.project}/{name}"

    def describe(self) -> str:
        return "global skills" if self.project is None else f"skills of project {self.project}"


GLOBAL_PLACE = Place("global", None, None)
