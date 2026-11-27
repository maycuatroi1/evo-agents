"""The limits of an agent's read of another project, and the shape of what it reads
(``evo_agents.hub.server.agents.service.reads`` says the rules; the request, which checks a project's name against
``admin.PROJECT_NAME``, is ``evo_agents.hub.server.agents.service.shapes.CrossReadIn``)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

Tool = Literal["kg_search", "kg_context", "memory_search", "memory_get"]
MAX_LIMIT = 20
MAX_IDS = 20
CUT = re.compile(r"\n\[truncated: \d+ more characters; call kg_more with handle \w+\]$")
CUT_NOTE = "\n[cut here: ask again with a narrower query or a lower limit]"


class CrossRead(BaseModel):
    project: str
    tool: str
    text: str = Field(description="what the agent reads: the items the check lets in, then the line on the rest")
    shown: int = Field(description="items shown")
    withheld: int = Field(description="items the person may read that the run's project does not let in")
    why: str | None = Field(description="how many items were left out and why; null when none was")
