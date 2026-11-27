"""The shapes and limits of invite links (``evo_agents.hub.server.invites``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub.server.base.models import LOGIN_NAME, PROJECT_NAME

PREFIX = "evn_"
TOKEN_BYTES = 32  # 256 random bits
DEFAULT_DAYS = 7
MAX_DAYS = 30
PAGE = "/invite/"  # the web's page of a link: EVO_HUB_PUBLIC_URL + PAGE + its token
JOIN, REGISTER = "join", "register"
OPEN, ACCEPTED, REVOKED, EXPIRED = "open", "accepted", "revoked", "expired"
ASK_AGAIN = "ask a hub admin for a new link; nothing was granted"

Role = Literal["reader", "writer", "admin"]
Status = Literal["open", "accepted", "revoked", "expired"]


class InviteRequest(BaseModel):
    kind: Literal["join", "register"] = Field(
        description="join: a grant on an existing project; register: the right to register one new project"
    )
    project: str | None = Field(None, pattern=PROJECT_NAME, description="join: the project the link grants on")
    role: Role | None = Field(None, description="join: the role it gives")
    max_level: str | None = Field(
        None, min_length=1, max_length=100, description="join: the highest level of the project's ladder it reaches"
    )
    for_login: str | None = Field(
        None,
        pattern=LOGIN_NAME,
        description="the one GitHub login that may accept the link; anyone signed in if left out",
    )
    days: int = Field(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description="how long the link stays open")

    @model_validator(mode="after")
    def _fields_of_the_kind(self) -> InviteRequest:
        named = [self.project, self.role, self.max_level]
        if self.kind == JOIN and None in named:
            raise ValueError("a join link names a project, a role and a max level")
        if self.kind == REGISTER and any(value is not None for value in named):
            raise ValueError("a register link names no project, role or max level")
        return self


class InviteRow(BaseModel):
    id: int
    kind: Literal["join", "register"]
    project: str | None = Field(
        description="join: the project the link grants on; register: the project its right registered, once used"
    )
    role: Role | None = Field(description="join: the role it gives")
    max_level: str | None = Field(description="join: the highest level it reaches")
    for_login: str | None = Field(description="the one login that may accept it; null for anyone signed in")
    created_by: str = Field(description="the hub admin who made it")
    created_at: datetime
    expires_at: datetime = Field(description="after this the link can no longer be accepted")
    status: Status = Field(description="revoked, else accepted, else expired once expires_at is past, else open")
    accepted_by: str | None
    accepted_at: datetime | None
    used_at: datetime | None = Field(description="register: when its right registered the project")


class InviteCreated(InviteRow):
    token: str = Field(description="evn_...: shown this once, the hub keeps only its SHA-256")
    url: str | None = Field(description="the link to hand out, EVO_HUB_PUBLIC_URL + /invite/ + token; null without one")


class InviteAccept(BaseModel):
    token: str = Field(min_length=1, max_length=200, description="the token of the link, evn_...")


class InvitePreview(BaseModel):
    kind: Literal["join", "register"]
    invited_by: str = Field(description="the hub admin who made the link")
    status: Status
    expires_at: datetime
    bound: bool = Field(description="whether one GitHub login alone may accept the link; which one is never shown")
    viewer: str | None = Field(description="the login signed in on the web that asks; null for a visitor")
    for_viewer: bool = Field(description="whether the viewer may accept the link: signed in, and it unbound or theirs")
    accepted_by_viewer: bool = Field(description="whether the viewer accepted the link")
    project: str | None = Field(
        description="join: the project; register: the project its right registered, once used. Null unless the link "
        "is open and for the viewer (unbound, anyone), or the viewer accepted it"
    )
    role: Role | None = Field(description="join: the role it gives, shown as the project is")
    max_level: str | None = Field(description="join: the highest level it reaches, shown as the project is")
