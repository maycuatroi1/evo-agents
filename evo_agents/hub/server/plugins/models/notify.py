"""The shapes of a notice from an isolated plugin to the members of a project (``service.notify``)."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import runs
from evo_agents.hub.server.base.models import LOGIN_NAME, StrictModel
from evo_agents.hub.server.notifications.models import TITLE_CHARS

PERMISSION = "notifications:send"
NOTICE = "plugin_notice"  # evo_agents.hub.runs.NOTICE_KINDS
ROLE_WORDS = {"admins": "admin", "writers": "writer", "members": "reader"}  # the lowest role each word reaches
MAX_TO = 100
MAX_KEY_CHARS = 200
TOPIC = r"^[a-z0-9][a-z0-9._-]{0,63}$"
LOGIN = re.compile(LOGIN_NAME)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PluginNoticeIn(StrictModel):
    to: list[str] = Field(
        min_length=1,
        max_length=MAX_TO,
        description="logins and/or the roles admins, writers (writers and admins) and members (everyone with a grant)",
    )
    title: str = Field(min_length=1, max_length=TITLE_CHARS, description="one line")
    body: str | None = Field(None, description=f"at most {runs.MAX_NOTICE_BODY_BYTES} bytes in UTF-8")
    link: str | None = Field(
        None,
        max_length=500,
        description="a path under /p/{project}/x/{plugin}/, or /p/{project}; default: the plugin's page, else the project",
    )
    topic: str = Field(pattern=TOPIC, description="a short slug the plugin chooses")
    key: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_KEY_CHARS,
        description="a second call of the plugin in the project with the same key creates nothing",
    )

    @field_validator("to")
    @classmethod
    def _to(cls, items: list[str]) -> list[str]:
        for item in items:
            if item not in ROLE_WORDS and not LOGIN.fullmatch(item):
                raise ValueError(f"{item!r} is neither a login nor one of {', '.join(ROLE_WORDS)}")
        return list(dict.fromkeys(items))

    @field_validator("title")
    @classmethod
    def _title(cls, title: str) -> str:
        title = title.strip()
        if not title or CONTROL.search(title):
            raise ValueError("the title is one line of text")
        return title

    @field_validator("key")
    @classmethod
    def _key(cls, key: str | None) -> str | None:
        if key is not None and CONTROL.search(key):
            raise ValueError("the key has no control characters")
        return key


class PluginNoticeSent(BaseModel):
    sent: int = Field(description="the members who got a notice: the number of recipients")
    recipients: list[str] = Field(description="their logins")
    skipped: list[str] = Field(description="the logins in `to` that hold no grant on the project, who got none")
    duplicate: bool = Field(
        description="the key was used already: nothing new was created and this is the first answer"
    )
