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
from evo_agents.hub.kg_decisions import plugin_actor
from evo_agents.hub.kg_web import WebSession
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg_decisions import Fix, KgDecision, apply_fixes
from evo_agents.hub.server.kg_web import GraphRef, _no_graph, _read, _reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg.review import RULES

MAX_REASON_CHARS = 1000  # as migration 0141 checks
MAX_DECISIONS, MAX_DECISIONS_CAP = 25, 200  # the decisions a call to apply takes, by default and at most
LOOP = "kg-optimize"
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
    by: str = Field(description="the login of the writer who dismissed it; plugin:<id> for a core plugin")
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
        select(d.c.fingerprint, d.c.reason, u.c.login, d.c.dismissed_by_plugin, d.c.dismissed_at, d.c.build_id)
        .join(u, u.c.id == d.c.dismissed_by, isouter=True)
        .where(d.c.project_id == project_id)
    )
    return {
        row.fingerprint: Dismissal(
            reason=row.reason,
            by=row.login or plugin_actor(row.dismissed_by_plugin),
            at=row.dismissed_at,
            build_id=row.build_id,
        )
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
    data, ref = await _read(request, user, project, WebSession.review)
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
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
        _reader(access)
        if not has_role(access.role, "writer"):
            raise HTTPException(403, f"dismissing a review item of project {access.name} needs the writer role on it")
    data, ref = await _read(request, user, project, WebSession.review)
    if ref is None:
        raise _no_graph(project)
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
                        "dismissed_by_plugin": None,
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


class ApplyBody(BaseModel):
    origin: str = Field(LOOP, pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$", description="the loop that applies the fixes")
    run_id: int | None = Field(None, ge=1, description="the run of the loop, a run of this project")
    max_decisions: int = Field(MAX_DECISIONS, ge=1, le=MAX_DECISIONS_CAP, description="the cap of this call")
    rules: list[Rule] | None = Field(None, description="only the items of these rules; every rule when null")
    dry_run: bool = Field(False, description="say what would be decided and decide nothing")


class AppliedDecision(BaseModel):
    number: int | None = Field(description="the decision's number; null on a dry run")
    kind: str
    src: str
    rel: str | None
    dst: str | None
    reason: str


class AppliedItem(BaseModel):
    fingerprint: str
    rule: Rule
    whole: bool = Field(description="true when every decision of the item's fix is taken or was said already")
    decisions: list[AppliedDecision]


class ReportItem(BaseModel):
    fingerprint: str
    rule: Rule
    cluster: str | None
    cluster_label: str | None
    nodes: list[ReviewNode]


class ApplyResult(BaseModel):
    graph: GraphRef
    rev: int = Field(description="the project's decision revision after the call")
    dry_run: bool
    max_decisions: int
    decided: int = Field(description="the decisions taken (or, on a dry run, that would be)")
    skipped: int = Field(description="the decisions of fixes a proposed or active decision already says")
    items: list[AppliedItem] = Field(description="the items whose fix took a decision, in the queue's order")
    report: list[ReportItem] = Field(description="the open items no decision fixes, for a person to read")
    left: dict[str, int] = Field(description="per rule, the open items with a fix not wholly taken")


@router.post("/{project}/review/apply", response_model=ApplyResult)
async def apply_review(request: Request, project: ProjectName, body: ApplyBody, user: CurrentUser) -> ApplyResult:
    """Apply the fixes of the open review items you can see as decisions, at most max_decisions, naming your loop."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        if not has_role(access.role, "writer"):
            raise HTTPException(403, f"applying the review queue of project {access.name} needs the writer role on it")
        if body.run_id is not None:
            r = tables.runs
            owner = await conn.scalar(select(r.c.project_id).where(r.c.id == body.run_id))
            if owner != access.project_id:
                raise HTTPException(404, f"no run {body.run_id} in project {access.name}")
        dismissed = await _dismissals(conn, access.project_id)
    data, ref = await _read(request, user, project, WebSession.review)
    if ref is None:
        raise _no_graph(project)
    asked = [
        i
        for i in data["items"]
        if i["fingerprint"] not in dismissed and (body.rules is None or i["rule"] in body.rules)
    ]
    fixes = [Fix(**d, fingerprint=i["fingerprint"]) for i in asked for d in i["detail"].get("fix") or []]
    taken, skipped, rev = await apply_fixes(
        request,
        user,
        access,
        fixes,
        cap=body.max_decisions,
        origin=body.origin,
        run_id=body.run_id,
        build_id=ref.build_id,
        dry_run=body.dry_run,
    )
    done: dict[str, list[AppliedDecision]] = {}
    for fix, made in taken:
        number = made.number if isinstance(made, KgDecision) else None
        shown = fix.model_dump(exclude={"fingerprint"})
        done.setdefault(fix.fingerprint, []).append(AppliedDecision(number=number, **shown))
    said: dict[str, int] = {}
    for fix in skipped:
        said[fix.fingerprint] = said.get(fix.fingerprint, 0) + 1
    items, left = [], {}
    for i in asked:
        fix = i["detail"].get("fix") or []
        whole = bool(fix) and len(done.get(i["fingerprint"], [])) + said.get(i["fingerprint"], 0) == len(fix)
        if i["fingerprint"] in done:
            items.append(
                AppliedItem(fingerprint=i["fingerprint"], rule=i["rule"], whole=whole, decisions=done[i["fingerprint"]])
            )
        if fix and not whole:
            left[i["rule"]] = left.get(i["rule"], 0) + 1
    report = [
        ReportItem(
            fingerprint=i["fingerprint"],
            rule=i["rule"],
            cluster=i["cluster"],
            cluster_label=i["cluster_label"],
            nodes=i["nodes"],
        )
        for i in asked
        if i["detail"].get("report")
    ]
    return ApplyResult(
        graph=ref,
        rev=rev,
        dry_run=body.dry_run,
        max_decisions=body.max_decisions,
        decided=len(taken),
        skipped=len(skipped),
        items=items,
        report=report,
        left=left,
    )


class Undismissed(BaseModel):
    fingerprint: str
    was_by: str = Field(description="who had dismissed it: a login, or plugin:<id> for a core plugin")


@router.delete("/{project}/review/{fingerprint}/dismiss", response_model=Undismissed)
async def undismiss_item(
    request: Request, project: ProjectName, fingerprint: Fingerprint, user: CurrentUser
) -> Undismissed:
    """Take a dismissal of an item of the review queue you can see back: the item is open again."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        if not has_role(access.role, "writer"):
            raise HTTPException(403, f"reopening a review item of project {access.name} needs the writer role on it")
    data, ref = await _read(request, user, project, WebSession.review)
    if ref is None:
        raise _no_graph(project)
    if not any(i["fingerprint"] == fingerprint for i in data["items"]):
        raise HTTPException(404, f"no review item with that fingerprint is visible to you in project {project}")
    d, u = tables.kg_review_dismissals, tables.users
    async with request.app.state.engine.begin() as conn:
        row = (
            await conn.execute(
                select(u.c.login, d.c.dismissed_by_plugin)
                .select_from(d.join(u, u.c.id == d.c.dismissed_by, isouter=True))
                .where(d.c.project_id == access.project_id, d.c.fingerprint == fingerprint)
                .with_for_update(of=d)
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(404, f"no review item with that fingerprint is dismissed in project {project}")
        by = row.login or plugin_actor(row.dismissed_by_plugin)
        await conn.execute(d.delete().where(d.c.project_id == access.project_id, d.c.fingerprint == fingerprint))
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.KG_REVIEW,
            target=f"{project} item={fingerprint} reopened dismissed_by={by}",
            project_id=access.project_id,
        )
    return Undismissed(fingerprint=fingerprint, was_by=by)
