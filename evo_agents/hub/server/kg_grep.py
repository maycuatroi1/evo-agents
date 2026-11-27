"""POST /v1/kg/{project}/grep-links: what the project's graph links to the lines a grep matched, the note the hook
``evo-agents kg hook post-search`` adds after a Grep, rg or grep (``evo_agents.kg.post_search``,
``evo_agents.hub.kg_grep_hook``).

The body names the files the result named, as (repo, path), and the matched lines, each by the index of its file, its
line number (null for the file alone) and its text, at most ``grep_links.MAX_FILES`` files and
``grep_links.MAX_HITS`` lines. The answer is the note's lines, at most ``grep_links.MAX_NODES``, read from the
project's latest graph the api holds (``kg.snapshot_view``) with the rule of the kg_* tools: a member of the project,
the meet of the grant and the sink's clearance, the audience the database gives the member at this call; a sink the
project does not declare sees nothing. A KG run's token reads through the agents' sink (``kg_run_access.sink``). A
project with no graph yet is 409, a graph the api cannot read now 503.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from evo_agents.hub.kg_graph import GraphUnavailable, reading
from evo_agents.hub.server import kg_run_access
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg import REFUSALS, Sink
from evo_agents.hub.server.kg.service.tools import read_sink, readable_graphs, snapshot_view
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg import grep_links

Text = Annotated[str, Field(max_length=1000)]

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}})


class GrepFile(BaseModel):
    repo: Text = Field(min_length=1, description="the repo's name in the project, as a file node's props.repo")
    path: Text = Field(min_length=1, description="the file's path inside the repo, with /")


class GrepHit(BaseModel):
    file: int = Field(ge=0, description="the index of the line's file in files")
    line: int | None = Field(default=None, ge=0, description="the line number; null for the file alone (-l)")
    text: str = Field(default="", max_length=grep_links.MAX_TEXT, description="the matched line")


class GrepLinks(BaseModel):
    sink: Sink = Field(default="claude-code@anthropic", description="the sink profile whose clearance applies")
    files: list[GrepFile] = Field(max_length=grep_links.MAX_FILES)
    hits: list[GrepHit] = Field(max_length=grep_links.MAX_HITS)

    @model_validator(mode="after")
    def _known_files(self):
        if any(hit.file >= len(self.files) for hit in self.hits):
            raise ValueError("a hit names a file index past the end of files")
        return self


class GrepLinksOut(BaseModel):
    lines: list[str] = Field(description="the note's lines, one per matched node with something to add")


@router.post("/{project}/grep-links", response_model=GrepLinksOut, responses=REFUSALS)
async def grep_links_of(request: Request, project: ProjectName, body: GrepLinks, user: CurrentUser) -> GrepLinksOut:
    """What the project's latest graph links to the lines a grep matched (see the module)."""
    state = request.app.state
    sink = kg_run_access.sink(user, body.sink)  # a KG run's token reads as the agents do, whatever it names
    access, graphs, audience = await readable_graphs(state.engine, user, project)
    if not graphs:
        raise HTTPException(409, f"project {project} has no knowledge graph on the hub yet: build it first")
    ceiling = access.rules.ceiling(access.max_level, read_sink(access, sink), audience)
    if ceiling is None:
        return GrepLinksOut(lines=[])  # a sink the project does not declare sees nothing
    snapshot = await snapshot_view(state, access.project_id, graphs, ceiling)
    files = [(f.repo, f.path) for f in body.files]
    hits = [(files[h.file], h.line, h.text) for h in body.hits]
    clearance = (ceiling.level, ceiling.location)

    def read() -> list[str]:
        with reading(project, snapshot) as (view, _graph):
            return grep_links.link_lines(view, clearance, hits)

    try:
        return GrepLinksOut(lines=await asyncio.to_thread(read))
    except GraphUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
