"""The body of a write of a memory, ``MemoryIn``. It is in the service layer, not in ``models``: it takes its label
as the projects take one (``projects.LabelIn``), and the models import nothing of the hub's server that could reach
back into the service (contract ``hub-memories-layers``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub.memory import MAX_AUDIENCE
from evo_agents.hub.memory_files import MAX_BODY, TYPES, body_problem, name_problem
from evo_agents.hub.memory_places import PERSONAL_LOCATION
from evo_agents.hub.profiles import HANDLE_PATTERN
from evo_agents.hub.server.base.models import PROJECT_NAME
from evo_agents.hub.server.memories.models import MAX_REVISION, PrincipalName, printable
from evo_agents.hub.server.projects import LabelIn


class MemoryIn(BaseModel):
    scope: Literal["project", "personal"]
    project: str | None = Field(None, pattern=PROJECT_NAME, description="the hub project; scope project only")
    location: str = Field(
        min_length=1,
        max_length=255,
        description="harness or a repo of the project; for scope personal, what is left of the directory's slug",
    )
    name: str = Field(min_length=4, max_length=255, description="the file name, ending in .md")
    type: Literal[TYPES]  # pyright: ignore[reportInvalidTypeForm]
    body: str = Field(max_length=MAX_BODY, description="the whole file, frontmatter included")
    label: LabelIn | None = Field(
        None, description="scope project only; the current label, else the project's default, when left out"
    )
    if_revision: int | None = Field(
        None, ge=1, le=MAX_REVISION, description="the revision last seen; left out to create the memory"
    )
    audience: list[PrincipalName] | None = Field(
        None,
        max_length=MAX_AUDIENCE,
        description="what the writer read, chat:<id> and grant:<project>:<level>: added to the memory's audience",
    )
    agent: str | None = Field(
        None, pattern=HANDLE_PATTERN, description="scope project only: the agent profile whose memory it is"
    )

    @model_validator(mode="after")
    def _check(self):
        problems = [p for p in (name_problem(self.name), body_problem(self.body)) if p]
        if self.scope == "project":
            if self.project is None:
                problems.append("a project memory names its project")
            if not printable(self.location):
                problems.append("a project location is printable text without a slash or a backslash")
        else:
            if self.project is not None or self.label is not None or self.agent is not None:
                problems.append("a personal memory has no project, no label and no agent")
            if not PERSONAL_LOCATION.fullmatch(self.location):
                problems.append("a personal location is [~A-Za-z0-9-] then [A-Za-z0-9-] characters")
        if problems:
            raise ValueError("; ".join(problems))
        return self
