"""The API of the plugin crm as Pydantic models, each named ``Crm...`` in the hub's OpenAPI document. A contact carries a
GitHub login, the public link to its profile, the source and the stage: no email address, no name, no text."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from evo_agents.hub.tables_crm import MAX_NOTE_CHARS


class CrmContact(BaseModel):
    login: str = Field(description="the GitHub login, in lower case")
    profile_url: str = Field(description="the public profile on GitHub")
    source: str = Field(description="what it was first seen from: invite_request, invite_link, grant, issue, ...")
    stage: str = Field(description="lead, invited, activated or retained; it only moves forward")
    first_seen_at: datetime
    stage_at: datetime = Field(description="when it reached its stage")


class CrmTouchpoint(BaseModel):
    kind: str = Field(description="invite_request, invite_link, grant, issue, pull_request, discussion or note")
    ref: str = Field(description="what makes it one touchpoint: issue#12, invite:3, a note's id")
    url: str | None = None
    note: str | None = Field(default=None, description="the text of a note a writer added; the other kinds have none")
    author: str | None = Field(default=None, description="the login of the member who wrote a note")
    happened_at: datetime


class CrmStageReached(BaseModel):
    stage: str
    reached_at: datetime


class CrmContactDetail(CrmContact):
    touchpoints: list[CrmTouchpoint] = Field(description="newest first")
    history: list[CrmStageReached] = Field(description="each stage the contact reached, in the order of the stages")


class CrmContacts(BaseModel):
    project: str
    counts: dict[str, int] = Field(description="stage -> how many of the contacts you can see are in it")
    contacts: list[CrmContact] = Field(description="newest first, at most 500")


class CrmNoteIn(BaseModel):
    note: str = Field(min_length=1, max_length=MAX_NOTE_CHARS, description="what to remember about the contact")


class CrmDeleted(BaseModel):
    deleted: bool = Field(description="true: the contact, its touchpoints and its history are gone")
    touchpoints: int = Field(description="how many touchpoints went with it")
