"""The review queue of a project's knowledge graph for the hub's web page and ``evo-agents hub kg review``.

- GET /v1/kg/{project}/review?rule=&cluster=&state=: the items of the latest build's review queue the member can see
  (``evo_agents.hub.kg_review``), open ones by default, ``dismissed`` or ``all`` on asking, of one rule or one cluster
  when given, with how many each rule has open and dismissed, and whether the member may dismiss.
- POST /v1/kg/{project}/review/{fingerprint}/dismiss {reason}: a writer dismisses an item they can see, with a reason.
  The dismissal is kept by the item's fingerprint, so it holds in every later build that finds the same item;
  dismissing it again replaces the reason. 404 for an item the writer cannot see, as for one that does not exist.
- DELETE /v1/kg/{project}/review/{fingerprint}/dismiss: a writer takes a dismissal back, whoever made it (a member, or
  a core plugin through its API), so the item is open again; 404 for an item the writer cannot see or that is not
  dismissed. The audit row names the item and who had dismissed it.
- POST /v1/kg/{project}/review/apply {origin, run_id, max_decisions, rules, dry_run}: a writer's loop (the KG optimize
  loop) applies the fixes of the open items it can see (``detail.fix``) as active decisions, in the queue's order, at
  most ``max_decisions`` in a call, an item's fix in part when the cap falls inside it; a decision a proposed or active
  one already says is skipped. Each step names the loop and its run; ``hub kg decisions undo`` takes one back. The
  items of cluster_without_knowledge take no decision: they come back as the report. One build is queued.

Access and visibility are those of the graph's other web reads (``evo_agents.hub.server.kg_web``): a grant on the
project, the grant's ceiling with the member's audience, and every node through the session's ``Visibility``.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam

from evo_agents.hub.kg_web import WebSession
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.review import (
    ApplyBody,
    ApplyResult,
    Dismissal,
    DismissBody,
    ReviewQueue,
    Rule,
    Undismissed,
)
from evo_agents.hub.server.kg.service import review as service
from evo_agents.hub.server.kg_decisions.service import apply_fixes
from evo_agents.hub.server.kg_web import no_graph, read_graph
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 502, 503)},
)

Fingerprint = Annotated[str, PathParam(pattern=r"^r:[0-9a-f]{16}$", description="an item's fingerprint")]


async def _read(request: Request, user, project: str, *, required: bool):
    """The review queue of the latest build for ``user`` and that build, as ``kg_web.read_graph`` reads it; 404 when
    no build succeeded yet and ``required``."""
    data, ref = await read_graph(request, user, project, WebSession.review)
    if required and ref is None:
        raise no_graph(project)
    return data, ref


@router.get("/{project}/review", response_model=ReviewQueue)
async def review_queue(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    rule: Annotated[Rule | None, Query(description="only the items of this rule")] = None,
    cluster: Annotated[str | None, Query(max_length=200, description="only the items of this cluster")] = None,
    state: Annotated[Literal["open", "dismissed", "all"], Query(description="which items to list")] = "open",
) -> ReviewQueue:
    """The items of the review queue you can see, grouped by rule, most connected first, with counts per rule."""
    read = partial(_read, request, user, project, required=False)
    return await service.queue(request.app.state.engine, read, user, project, rule, cluster, state)


@router.post("/{project}/review/{fingerprint}/dismiss", response_model=Dismissal)
async def dismiss_item(
    request: Request, project: ProjectName, fingerprint: Fingerprint, body: DismissBody, user: CurrentUser
) -> Dismissal:
    """Dismiss an item of the review queue you can see, with a reason; it stays dismissed in later builds."""
    read = partial(_read, request, user, project, required=True)
    return await service.dismiss(request.app.state.engine, read, user, project, fingerprint, body.reason)


@router.post("/{project}/review/apply", response_model=ApplyResult)
async def apply_review(request: Request, project: ProjectName, body: ApplyBody, user: CurrentUser) -> ApplyResult:
    """Apply the fixes of the open review items you can see as decisions, at most max_decisions, naming your loop."""
    read = partial(_read, request, user, project, required=True)
    decide = partial(apply_fixes, request)
    return await service.apply(request.app.state.engine, read, decide, user, project, body)


@router.delete("/{project}/review/{fingerprint}/dismiss", response_model=Undismissed)
async def undismiss_item(
    request: Request, project: ProjectName, fingerprint: Fingerprint, user: CurrentUser
) -> Undismissed:
    """Take a dismissal of an item of the review queue you can see back: the item is open again."""
    read = partial(_read, request, user, project, required=True)
    return await service.undismiss(request.app.state.engine, read, user, project, fingerprint)
