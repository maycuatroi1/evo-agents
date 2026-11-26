"""An accepted proposal of the Curator as its plan. ``evo_agents.hub.server.changes`` moves the change on from there:
the night shift's Builder, the pull request, the Judge, and the merge of a change of a tier the charter's auto_merge
names.

Accepting a proposal of a tier the charter's ``planned_tiers`` names (``proposals.planned_tiers_of``: tiers 0 to 2
unless the charter names others, tier 3 included; ``proposals.answer_proposal_as``, on the web, the command line or
Telegram) makes its draft a plan on the hub in the same transaction (``plan_from_proposal``), as the admin who
accepted it, labelled as the proposal: the Curator's plan ``curator-<proposal>-<slug>``, in the repo the proposal
names, on the branch ``curator/<proposal>-<slug>``, and a row of curator_changes, state ``planned``, with the line
``planned`` in the proposal's ledger. A proposal that names several repos of the project, each on GitHub, becomes one
plan in them all, owner before consumer by the project's seams (``change_repos``), and its change has a row of
curator_change_repos per repo in that order. A proposal that names a repo the project does not have, one without an
origin, or several repos one of which is not on GitHub cannot become one: its change is recorded ``open``, without a
plan, with the reason and the line ``left_open``, and nothing of it runs. Either way the change is audited as curator.plan. Accepting a proposal of a
tier the charter does not plan, tier 3 unless it names it, makes nothing: its owner alone makes the change.

POST /v1/projects/{p}/curator/proposals/{id}/plan does the same for a proposal accepted without becoming a plan, such as
one of tier 2 accepted while that tier still recorded the answer alone: for the project's admins (403 otherwise), on an
accepted proposal of a tier the charter plans that has no change yet (409 with why for one that is not accepted, is of
a tier the charter does not plan or has a live change already). It answers the change, planned or left open with the
reason. A proposal whose change is stuck, ``open`` or ``closed`` with no plan, branch or pull request (one left open
because its draft could not become a plan then, such as a draft in several repos before a plan could work in them),
is planned again in that same row (``replan_stuck``; curator_changes holds one change a proposal): the change becomes
``planned`` with its plan, its reason gone, and its ledger says which state the plan replaced. When the draft still
cannot become a plan the route answers 409 with why and the change stays as it was.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.change_views import Change
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.proposal_models import ItemId
from evo_agents.hub.server.proposal_plans import service
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}


@router.post(
    "/{project}/curator/proposals/{proposal_id}/plan", status_code=201, response_model=Change, responses=REFUSALS
)
async def plan_proposal(request: Request, project: ProjectName, proposal_id: ItemId, user: CurrentUser) -> Change:
    """Make an accepted proposal of tier 0 to 2 that has no change yet, or a stuck one, the Curator's plan, as
    accepting it does now; the project's admins alone may. The change: planned, or left open with why its draft
    cannot become a plan."""
    return await service.plan_proposal(request.app.state.engine, user, project, proposal_id)
