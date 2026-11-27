"""How often the confirmed answers are loaded (plan active-context-answers, criteria a3 and a6).

Daily, ``hub.context_answer_reuse`` (``push``) posts, for each project whose policy sets ``context.answers``, the signal
``context.answer_reuse``: the answers loaded into prompts on the day before (UTC), measured at the start of that day with
the external id ``context.answer_reuse:YYYY-MM-DD``, so a rerun posts nothing twice. A project with the key off gets
nothing. The signal goes through the project's ingest key ``context-answers`` (made on first use, owned by its first
admin) and the key must be in the project's registry: where it is not, the hub refuses the signal, logs it and the table
``context_answer_loads`` keeps the figures.

``GET /v1/projects/{project}/context-answers`` (``evo-agents hub context answers``) gives the weeks: the runs queued, the
runs that loaded at least one answer and the answers loaded, to set the weeks after the policy revision that turned the
key on against those before. Any member with a grant on the project reads it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server.base.models import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.answer_reuse import DEFAULT_WEEKS, MAX_WEEKS, ContextAnswerWeeks
from evo_agents.hub.server.kg.service import answer_reuse as service
from evo_agents.hub.server.kg.service.answer_reuse import push as push  # the daily job's (hub.context_answer_reuse)
from evo_agents.hub.server.security import principal

router = APIRouter(prefix="/v1/projects", tags=["context"], responses={401: {"model": ErrorBody}})


@router.get(
    "/{project}/context-answers",
    response_model=ContextAnswerWeeks,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def read_weeks(
    request: Request,
    project: Annotated[str, PROJECT_NAME],
    weeks: Annotated[
        int, Query(ge=1, le=MAX_WEEKS, description="how many weeks, the current one included")
    ] = DEFAULT_WEEKS,
) -> ContextAnswerWeeks:
    """By week (UTC): the runs of the project, those that loaded at least one confirmed answer, and the answers loaded."""
    return await service.read_weeks(request.app.state.engine, principal(request), project, weeks)
