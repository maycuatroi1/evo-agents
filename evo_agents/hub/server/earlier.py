"""What earlier review nights of a project found, and what became of it: so a Reviewer looks through a lens knowing
what was found through it before, which of it is closed, and which proposals the owner rejected lately, and does not
record a closed finding again without new evidence.

A finding's status comes from the proposals that name it (``finding_ids``), those the reader reads by their label
through the sink alone, and their changes and ledgers (``proposal_status``): ``merged`` once its change merged
(``kept`` or ``reverted`` once the ledger's outcome says so), ``rejected`` for a proposal its owner rejected or that
was dropped as a repeat, ``proposed`` otherwise. A finding takes the first of kept, merged, proposed, reverted and
rejected its proposals have, and ``open`` with none. CLOSED are the statuses whose work is done: a change kept or
merged, or a proposal rejected; a reverted change opens its finding again.

``for_prompt`` gives the prompt of a review run (``evo_agents.hub.review.build_review_prompt``), as the run's owner
reads them through the project's hub sink: for each lens of the night its latest PER_LENS findings with their status,
and the proposals rejected in the last REJECTED_DAYS days with the owner's note. ``closed_like`` is what the hub checks
before it records a finding (``evo_agents.hub.server.proposals.record_finding``): a closed finding of the same lens
whose title is like the new one's (SIMILAR of the words, or the same evidence) and which cited every piece of the new
one's evidence already; the hub then refuses the new finding (409), naming the closed one.

GET /v1/projects/{p}/curator/earlier (reader) lists the findings the caller may read through its sink, newest first,
filtered by lens and status, with their proposals, and the proposals rejected lately; the MCP tool curator_findings and
``evo-agents hub curator earlier`` read it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub import review
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.runs.models.earlier import (  # noqa: F401 (proposals.service.record and the tests)
    CLOSED,
    MAX_LIMIT,
    REPEATED,
    STATUSES,
)
from evo_agents.hub.server.runs.models.earlier import EarlierFinding as EarlierFinding
from evo_agents.hub.server.runs.models.earlier import EarlierProposal as EarlierProposal
from evo_agents.hub.server.runs.models.earlier import EarlierView as EarlierView
from evo_agents.hub.server.runs.models.earlier import RejectedProposal as RejectedProposal
from evo_agents.hub.server.runs.service import earlier as service
from evo_agents.hub.server.runs.service.earlier import (  # noqa: F401 (collect, history.mcp, proposals, the tests)
    closed_like,
    earlier_view,
    finding_status,
    findings_of,
    for_prompt,
    proposal_status,
    rejected_lately,
    similar,
    texts_of,
    title_words,
)
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}


@router.get("/{project}/curator/earlier", response_model=EarlierView, responses=READ_REFUSALS)
async def show_earlier(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    lens: Annotated[Literal[tuple(review.LENSES)] | None, Query()] = None,
    status: Annotated[Literal[STATUSES] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> EarlierView:
    """What earlier review nights found, newest first, with what became of it, and the proposals rejected lately."""
    return await service.show(request.app.state.engine, user, project, sink, lens=lens, status=status, limit=limit)
