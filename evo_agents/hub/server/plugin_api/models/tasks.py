"""The shapes of the tasks a plugin keeps (``routes.tasks``)."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evo_agents.hub.server.tasks.models.api import MAX_PEOPLE, TasksItemDetail, TasksLinksIn, TasksRolesIn
from evo_agents.hub.tables_tasks import MAX_DESCRIPTION_CHARS, MAX_TITLE_CHARS, PRIORITIES

PERMISSION = "tasks:write"
FEATURE = "tasks"
MAX_KEY_CHARS = 200
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
RACE = "another write in the project took a number or a key at the same moment: nothing was written; try again"


class PluginTaskIn(BaseModel):
    """A new task of the plugin, under a key of its own; its type and status are the config's first when left out."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=MAX_KEY_CHARS,
        description="the plugin's own key of the task, unique among its tasks in the project: GET ?key= finds it",
    )
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    description_md: str = Field("", max_length=MAX_DESCRIPTION_CHARS, description="Markdown")
    type: str | None = Field(None, max_length=40, description="one of the config's types; default: its first")
    status: str | None = Field(None, max_length=40, description="a key of the config's statuses; default: its first")
    priority: str = Field("medium", max_length=20, description=f"one of {', '.join(PRIORITIES)}")
    assignees: list[str] = Field(default_factory=list, max_length=MAX_PEOPLE)
    roles: TasksRolesIn = Field(default_factory=lambda: TasksRolesIn.model_validate({}))
    links: TasksLinksIn = Field(default_factory=lambda: TasksLinksIn())

    @field_validator("key")
    @classmethod
    def _key(cls, key: str) -> str:
        if CONTROL.search(key):
            raise ValueError("the key has no control characters")
        return key


class PluginTask(TasksItemDetail):
    """A task the plugin made, whole, with the key it made it under."""

    key: str = Field(description="the plugin's own key of the task")


__all__ = ["CONTROL", "FEATURE", "MAX_KEY_CHARS", "PERMISSION", "RACE", "PluginTask", "PluginTaskIn"]
