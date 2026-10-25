"""The review queue of a project's knowledge graph for the hub's web page and ``evo-agents hub kg review``.

- GET /v1/kg/{project}/review?rule=&cluster=&state=: the items of the latest build's review queue the member can see
  (``evo_agents.hub.kg_review``), open ones by default, ``dismissed`` or ``all`` on asking, of one rule or one cluster
  when given, with how many each rule has open and dismissed, and whether the member may dismiss.
- POST /v1/kg/{project}/review/{fingerprint}/dismiss {reason}: a writer dismisses an item they can see, with a reason.
  The dismissal is kept by the item's fingerprint, so it holds in every later build that finds the same item;
  dismissing it again replaces the reason. 404 for an item the writer cannot see, as for one that does not exist.

Access and visibility are those of the graph's other web reads (``evo_agents.hub.server.kg_web``): a grant on the
project, the grant's ceiling with the member's audience, and every node through ``Session.visible``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_web import WebSession
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg_web import GraphRef, no_graph, read_graph, require_reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg.review import RULES

MAX_REASON_CHARS = 1000  # as migration 0141 checks
NO_BUILD = "no build of this project succeeded yet"

Rule = Literal[  # RULES of evo_agents.kg.review, in order
    "dangling_mention",
    "unmentioned_document",
    "cluster_without_knowledge",
    "possible_duplicate",
    "ambiguous_match",
    "bulk_noise",
]

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 502, 503)},
)


class ReviewNode(BaseModel):
    id: str
    kind: str
    name: str | None


class Dismissal(BaseModel):
    reason: str
    by: str = Field(description="the login of the writer who dismissed it")
    at: datetime
    build_id: int | None = Field(description="the build they saw the item in; null once that build is gone")


class ReviewItem(BaseModel):
    fingerprint: str = Field(description="the item's id, the same in every build that finds it")
    rule: Rule
    action: str = Field(
        description="what the rule suggests: fix_mention, link_or_deprecate, add_knowledge, merge, disambiguate or "
        "exclude"
    )
    cluster: str | None = Field(description="the level-1 cluster of its first node you can see, if any")
    cluster_label: str | None = Field(description="that cluster's label, as your map shows it")
    nodes: list[ReviewNode] = Field(description="the nodes the item is about that you can see")
    detail: dict = Field(description="what the rule found: count, and per rule mentions, samples, path, kind, where")
    weight: int = Field(description="the most edges you can see any of its nodes has; the queue's order")
    dismissal: Dismissal | None = Field(description="null while the item is open")


class RuleCount(BaseModel):
    rule: Rule
    open: int
    dismissed: int


class ReviewQueue(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    computed: bool = Field(description="false when the build has no review queue; reason says why")
    reason: str | None
    can_dismiss: bool = Field(description="true for a writer of the project")
    counts: list[RuleCount] = Field(description="every rule, with the items you can see open and dismissed")
    items: list[ReviewItem] = Field(description="by rule, then most connected first")


class DismissBody(BaseModel):
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a reason must say something")
        return value


Fingerprint = Annotated[str, PathParam(pattern=r"^r:[0-9a-f]{16}$", description="an item's fingerprint")]


async def _dismissals(conn, project_id: int) -> dict[str, Dismissal]:
    d, u = tables.kg_review_dismissals, tables.users
    rows = await conn.execute(
        select(d.c.fingerprint, d.c.reason, u.c.login, d.c.dismissed_at, d.c.build_id)
        .join(u, u.c.id == d.c.dismissed_by)
        .where(d.c.project_id == project_id)
    )
    return {
        row.fingerprint: Dismissal(reason=row.reason, by=row.login, at=row.dismissed_at, build_id=row.build_id)
        for row in rows
    }


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
    data, ref = await read_graph(request, user, project, WebSession.review)
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        dismissed = await _dismissals(conn, access.project_id) if ref is not None else {}
    can_dismiss = has_role(access.role, "writer")
    if ref is None:
        empty = [RuleCount(rule=r, open=0, dismissed=0) for r in RULES]
        return ReviewQueue(graph=None, computed=False, reason=NO_BUILD, can_dismiss=can_dismiss, counts=empty, items=[])
    items = [ReviewItem(**item, dismissal=dismissed.get(item["fingerprint"])) for item in data["items"]]
    counts = [
        RuleCount(
            rule=r,
            open=sum(1 for i in items if i.rule == r and i.dismissal is None),
            dismissed=sum(1 for i in items if i.rule == r and i.dismissal is not None),
        )
        for r in RULES
    ]
    shown = [
        i
        for i in items
        if (rule is None or i.rule == rule)
        and (cluster is None or i.cluster == cluster)
        and (state == "all" or (i.dismissal is None) == (state == "open"))
    ]
    return ReviewQueue(
        graph=ref,
        computed=data["computed"],
        reason=data["reason"],
        can_dismiss=can_dismiss,
        counts=counts,
        items=shown,
    )


@router.post("/{project}/review/{fingerprint}/dismiss", response_model=Dismissal)
async def dismiss_item(
    request: Request, project: ProjectName, fingerprint: Fingerprint, body: DismissBody, user: CurrentUser
) -> Dismissal:
    """Dismiss an item of the review queue you can see, with a reason; it stays dismissed in later builds."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        if not has_role(access.role, "writer"):
            raise HTTPException(403, f"dismissing a review item of project {access.name} needs the writer role on it")
    data, ref = await read_graph(request, user, project, WebSession.review)
    if ref is None:
        raise no_graph(project)
    item = next((i for i in data["items"] if i["fingerprint"] == fingerprint), None)
    if item is None:
        raise HTTPException(404, f"no review item with that fingerprint is visible to you in project {project}")
    d = tables.kg_review_dismissals
    async with request.app.state.engine.begin() as conn:
        saved = pg_insert(d).values(
            project_id=access.project_id,
            fingerprint=fingerprint,
            rule=item["rule"],
            reason=body.reason,
            dismissed_by=user.user_id,
            build_id=ref.build_id,
        )
        row = (
            await conn.execute(
                saved.on_conflict_do_update(
                    index_elements=[d.c.project_id, d.c.fingerprint],
                    set_={
                        "rule": saved.excluded.rule,
                        "reason": saved.excluded.reason,
                        "dismissed_by": saved.excluded.dismissed_by,
                        "build_id": saved.excluded.build_id,
                        "dismissed_at": func.now(),
                    },
                ).returning(d.c.dismissed_at)
            )
        ).one()
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.KG_REVIEW,
            target=f"{project} item={fingerprint}",
            project_id=access.project_id,
        )
    return Dismissal(reason=body.reason, by=user.login, at=row.dismissed_at, build_id=ref.build_id)
