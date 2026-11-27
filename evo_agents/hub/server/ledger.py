"""The Curator's ledger on the hub: its one writer, the lines the hub adds as a proposal moves on, and the route that
reads a proposal's ledger. ``evo_agents.hub.ledger`` holds the model; ``evo_agents.hub.server.outcomes`` adds the
outcome of a merged change and the revert it proposes.

``append`` is the one statement that writes curator_ledger, an INSERT: nothing of the hub updates or deletes a line
(``tests/hub/test_curator_ledger.py`` reads the hub's code for one). The hub adds a line when a review run records a
proposal (``proposal_recorded``: ``proposed``, or ``dropped`` as a repeat of a rejected one, with the figures that set
it off, ``trigger_for``), when an admin answers it (``proposal_answered``), when its draft becomes the Curator's plan or
cannot (``change_recorded``), and at each move of its change (``change_moved``, from
``changes.service.moves.set_change``): built by its Builder, its pull request opened, judged (by the Judge, or by the
hub's own detector of score hacking), merged (by the hub, by the agent of the proposal's thread for its owner, or by
hand), left open for its owner with why, or closed (by a member, by the hub for a branch that changes no file, or by its
Builder, which drops it with a reason and keeps what it measured under ``details.note``); a Builder that ends otherwise
adds ``build_failed`` (``build_failed``). The hub's merge of a change whose replays won keeps their verdict under
``details.replay`` and, when it releases a global skill, the skill under ``details.release``;
``evo_agents.hub.server.releases.service.skill_release`` adds the line ``skill_release`` once it published the skill, or its version
before again after a revert.

GET /v1/projects/{p}/curator/proposals/{id}/ledger lists the lines of a proposal the caller may read (as GET
.../proposals/{id} reads it), oldest first, with when the hub counts the figures of its merged change again
(``outcome_due_at``, null before the merge and once the outcome is in).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, Path, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.curator.models.ledger import SHA as SHA
from evo_agents.hub.server.curator.models.ledger import Ledger
from evo_agents.hub.server.curator.models.ledger import LedgerLine as LedgerLine
from evo_agents.hub.server.curator.service import ledger as service
from evo_agents.hub.server.curator.service.ledger import append as append
from evo_agents.hub.server.curator.service.ledger import build_failed as build_failed
from evo_agents.hub.server.curator.service.ledger import change_moved as change_moved
from evo_agents.hub.server.curator.service.ledger import change_recorded as change_recorded
from evo_agents.hub.server.curator.service.ledger import cites_corrections as cites_corrections
from evo_agents.hub.server.curator.service.ledger import outcome_due_at as outcome_due_at
from evo_agents.hub.server.curator.service.ledger import proposal_answered as proposal_answered
from evo_agents.hub.server.curator.service.ledger import proposal_recorded as proposal_recorded
from evo_agents.hub.server.curator.service.ledger import trigger_for as trigger_for
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
ItemId = Annotated[int, Path(ge=1, le=MAX_ID)]


@router.get("/{project}/curator/proposals/{proposal_id}/ledger", response_model=Ledger, responses=READ_REFUSALS)
async def show_ledger(
    request: Request,
    project: ProjectName,
    proposal_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> Ledger:
    """The ledger of a proposal: what happened to it, line by line, oldest first."""
    return await service.show_ledger(request.app.state.engine, user, project, proposal_id, sink)
