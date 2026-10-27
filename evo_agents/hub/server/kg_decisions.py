"""The decisions writers take on a project's knowledge graph, and the proposals agents make, for the hub's web pages,
``evo-agents hub kg decisions`` and the MCP tool ``kg_propose`` (``evo_agents.hub.kg_decisions``).

- GET /v1/kg/{project}/decisions?state=: the decisions the member can see, newest first, each with its history (who,
  when, why, the build), its state (proposed, active, rejected, undone, or orphan for an active one whose subject the
  latest build no longer holds), the project's decision revision and whether the member may decide.
- POST /v1/kg/{project}/decisions {kind, src, rel, dst, reason, base_rev}: a writer deprecates a node (src) or an edge
  (src, rel, dst), merges two node ids (src, dst) or links two nodes (src, rel, dst, rel one the ontology knows). The
  decision is active at once and a build is queued.
- POST /v1/kg/{project}/decisions/{number}/{accept|reject|undo} {reason, base_rev}: a writer accepts or rejects a
  proposal, or undoes an active decision (an orphan too).

Each write names the decision revision of the project it read (``base_rev``, 0 before the first decision); when a
decision changed since, the write answers 409 with the revision now, and nothing changes. Writing needs the writer
role (403 for a reader, 404 for a caller without a grant, as every read of the graph). A decision names only nodes and
edges the writer sees in the latest build; a decision that names anything a member cannot see is not listed to them.
An agent's ``kg_propose`` writes the same decision as a proposal, which changes nothing until a writer accepts it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select

from evo_agents.hub import kg_decisions, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.kg_decisions import (
    ABSENT,
    HIDDEN,
    MAX_REASON_CHARS,
    MAX_REF_CHARS,
    TRANSITIONS,
    plugin_actor,
    seen_state,
)
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg_web import GraphRef, _no_graph, _read, _reader
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg.schema import known_relations

Kind = Literal["deprecate", "merge", "link"]  # kg_decisions.KINDS
State = Literal["proposed", "active", "rejected", "undone", "orphan"]  # kg_decisions.STATES and ORPHAN
Step = Literal["accept", "reject", "undo"]  # the keys of kg_decisions.TRANSITIONS
Ref = Annotated[str, Field(min_length=1, max_length=MAX_REF_CHARS)]
MAX_REV = 2**31 - 1

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 409, 502, 503)},
)


class KgDecisionEvent(BaseModel):
    rev: int = Field(description="the project's decision revision this step made")
    action: Literal["propose", "decide", "accept", "reject", "undo"]
    by: str = Field(
        description="the login of the member, or of the agent's owner, who took the step; plugin:<id> for a core "
        "plugin that took it as itself"
    )
    at: datetime
    reason: str
    build_id: int | None = Field(description="the build the graph was at; null once that build is gone")


class KgDecision(BaseModel):
    number: int = Field(description="the decision's number in the project, from 1")
    kind: Kind
    state: State
    src: str = Field(description="the node deprecated, the first node merged, or the edge's source")
    rel: str | None = Field(description="the edge's type, for an edge deprecated or linked")
    dst: str | None = Field(description="the edge's target, or the node merged into src's id")
    history: list[KgDecisionEvent] = Field(description="oldest first; the first step says why it was taken")


class KgDecisionList(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    rev: int = Field(description="the project's decision revision: what a write names as base_rev")
    can_decide: bool = Field(description="true for a writer of the project")
    decisions: list[KgDecision] = Field(description="newest first")


class KgDecisionStep(BaseModel):
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)
    base_rev: int = Field(ge=0, le=MAX_REV, description="the decision revision you read")

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a reason must say something")
        return value


class NewKgDecision(KgDecisionStep):
    kind: Kind
    src: Ref
    rel: Ref | None = None
    dst: Ref | None = None

    @model_validator(mode="after")
    def _shape(self) -> NewKgDecision:
        """What each kind names, as migration 0142 checks."""
        if self.kind == "merge" and (self.rel is not None or self.dst is None or self.dst == self.src):
            raise ValueError("a merge names two different nodes, src and dst, and no rel")
        if self.kind == "link" and (self.rel is None or self.dst is None):
            raise ValueError("a link names src, rel and dst")
        if self.kind == "deprecate" and (self.rel is None) != (self.dst is None):
            raise ValueError("a deprecation names a node (src) or an edge (src, rel and dst)")
        return self


Number = Annotated[int, PathParam(ge=1, le=MAX_REV, description="the decision's number in the project")]


def _stale(project: str, rev: int) -> HTTPException:
    return HTTPException(
        409, f"the decisions of project {project} changed since you read them: their revision is {rev}"
    )


async def _writer(request: Request, user, project: str) -> ProjectAccess:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
    _reader(access)
    if not has_role(access.role, "writer"):
        raise HTTPException(
            403, f"deciding on the knowledge graph of project {access.name} needs the writer role on it"
        )
    return access


async def _rows(conn, project_id: int) -> tuple[list, dict[int, list[KgDecisionEvent]], int]:
    d, e, u = tables.kg_decisions, tables.kg_decision_events, tables.users
    rows = list((await conn.execute(select(d).where(d.c.project_id == project_id).order_by(d.c.number.desc()))).all())
    events = await conn.execute(
        select(e.c.decision_id, e.c.rev, e.c.action, u.c.login, e.c.actor_plugin, e.c.at, e.c.reason, e.c.build_id)
        .join(u, u.c.id == e.c.actor_id, isouter=True)
        .where(e.c.project_id == project_id)
        .order_by(e.c.rev)
    )
    history: dict[int, list[KgDecisionEvent]] = {}
    rev = 0
    for ev in events:
        rev = ev.rev
        history.setdefault(ev.decision_id, []).append(
            KgDecisionEvent(
                rev=ev.rev,
                action=ev.action,
                by=ev.login or plugin_actor(ev.actor_plugin),
                at=ev.at,
                reason=ev.reason,
                build_id=ev.build_id,
            )
        )
    return rows, history, rev


def _asked(rows) -> tuple[list[str], list[tuple]]:
    nodes: set[str] = set()
    edges: set[tuple] = set()
    for row in rows:
        named, edge = kg_decisions.subject(row.kind, row.src, row.rel, row.dst)
        nodes.update(named)
        if edge:
            edges.add(edge)
    return sorted(nodes), sorted(edges)


async def _found(request: Request, user, project: str, rows) -> tuple[dict, GraphRef | None]:
    nodes, edges = _asked(rows)
    found, ref = await _read(request, user, project, lambda session: session.decision_subjects(nodes, edges))
    return found or {}, ref


async def listing(request: Request, user, project: str, state: str | None = None) -> KgDecisionList:
    """The decisions ``user`` can see, newest first, of ``state`` when given."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        rows, history, rev = await _rows(conn, access.project_id)
    found, ref = await _found(request, user, project, rows)
    shown = []
    for row in rows if ref is not None else []:
        seen = seen_state(row.kind, row.state, row.src, row.rel, row.dst, found)
        if seen is not None and (state is None or seen == state):
            shown.append(
                KgDecision(
                    number=row.number,
                    kind=row.kind,
                    state=seen,
                    src=row.src,
                    rel=row.rel,
                    dst=row.dst,
                    history=history.get(row.id, []),
                )
            )
    return KgDecisionList(graph=ref, rev=rev, can_decide=has_role(access.role, "writer"), decisions=shown)


@router.get("/{project}/decisions", response_model=KgDecisionList)
async def list_decisions(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[State | None, Query(description="only the decisions in this state")] = None,
) -> KgDecisionList:
    """The decisions on the graph you can see, newest first, with their history and the revision to write against."""
    return await listing(request, user, project, state)


async def _locked_rev(conn, project_id: int, project: str, base_rev: int) -> int:
    """Lock the project's decisions for this transaction; their revision, which must be ``base_rev`` (else 409)."""
    p, e = tables.projects, tables.kg_decision_events
    await conn.execute(select(p.c.id).where(p.c.id == project_id).with_for_update())
    rev = await conn.scalar(select(func.coalesce(func.max(e.c.rev), 0)).where(e.c.project_id == project_id))
    if rev != base_rev:
        raise _stale(project, rev)
    return rev


async def _event(conn, request, user, access: ProjectAccess, row, action: str, *, rev, reason, build_id) -> None:
    await conn.execute(
        tables.kg_decision_events.insert().values(
            project_id=access.project_id,
            rev=rev,
            decision_id=row.id,
            action=action,
            actor_id=user.user_id,
            reason=reason,
            build_id=build_id,
        )
    )
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.KG_DECISION,
        target=f"{access.name} decision={row.number} action={action}",
        project_id=access.project_id,
    )
    if action in ("decide", "accept", "undo"):  # the active decisions changed: the next build applies them
        await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)


async def create(request: Request, user, project: str, body: NewKgDecision, *, propose: bool) -> KgDecision:
    """A writer's decision, active at once, or an agent's proposal (``propose``)."""
    access = await _writer(request, user, project)
    found, ref = await _found(request, user, project, [body])
    if ref is None:
        raise _no_graph(project)
    nodes, edge = kg_decisions.subject(body.kind, body.src, body.rel, body.dst)
    missing = [key for key in [*nodes, *([edge] if edge else [])] if found.get(key, ABSENT) in (ABSENT, HIDDEN)]
    if missing:
        what = " ".join(missing[0]) if isinstance(missing[0], tuple) else missing[0]
        raise HTTPException(404, f"{what} is not visible to you in the latest graph of project {project}")
    d = tables.kg_decisions
    async with request.app.state.engine.begin() as conn:
        if body.kind == "link":
            configs = tables.kg_configs
            ontology = await conn.scalar(select(configs.c.ontology).where(configs.c.project_id == access.project_id))
            if body.rel not in known_relations(ontology):
                raise HTTPException(422, f"the ontology of project {project} has no relation {body.rel}")
        rev = await _locked_rev(conn, access.project_id, project, body.base_rev)
        same = select(d.c.number).where(
            d.c.project_id == access.project_id,
            d.c.kind == body.kind,
            d.c.src == body.src,
            d.c.rel.is_not_distinct_from(body.rel),
            d.c.dst.is_not_distinct_from(body.dst),
            d.c.state.in_(("proposed", "active")),
        )
        taken = await conn.scalar(same.limit(1))
        if taken is not None:
            raise HTTPException(409, f"decision {taken} of project {project} already says that")
        number = await conn.scalar(
            select(func.coalesce(func.max(d.c.number), 0) + 1).where(d.c.project_id == access.project_id)
        )
        state, action = ("proposed", "propose") if propose else ("active", "decide")
        row = (
            await conn.execute(
                d.insert()
                .values(
                    project_id=access.project_id,
                    number=number,
                    kind=body.kind,
                    state=state,
                    src=body.src,
                    rel=body.rel,
                    dst=body.dst,
                )
                .returning(d.c.id, d.c.number)
            )
        ).one()
        await _event(conn, request, user, access, row, action, rev=rev + 1, reason=body.reason, build_id=ref.build_id)
        at = await conn.scalar(select(func.now()))
    event = KgDecisionEvent(rev=rev + 1, action=action, by=user.login, at=at, reason=body.reason, build_id=ref.build_id)
    return KgDecision(
        number=number, kind=body.kind, state=state, src=body.src, rel=body.rel, dst=body.dst, history=[event]
    )


@router.post("/{project}/decisions", response_model=KgDecision, status_code=201)
async def take_decision(request: Request, project: ProjectName, body: NewKgDecision, user: CurrentUser) -> KgDecision:
    """Deprecate a node or an edge, merge two node ids or link two nodes, with a reason: active at once."""
    return await create(request, user, project, body, propose=False)


@router.post("/{project}/decisions/{number}/{step}", response_model=KgDecision)
async def step_decision(
    request: Request,
    project: ProjectName,
    number: Number,
    step: Annotated[Step, PathParam(description="accept or reject a proposal, or undo an active decision")],
    body: KgDecisionStep,
    user: CurrentUser,
) -> KgDecision:
    """Accept or reject a proposal, or undo an active decision, with a reason."""
    access = await _writer(request, user, project)
    seen = await listing(request, user, project)
    decision = next((x for x in seen.decisions if x.number == number), None)
    if decision is None:
        raise HTTPException(404, f"no decision {number} is visible to you in project {project}")
    needs, leaves = TRANSITIONS[step]
    d = tables.kg_decisions
    async with request.app.state.engine.begin() as conn:
        rev = await _locked_rev(conn, access.project_id, project, body.base_rev)
        row = (
            await conn.execute(
                select(d.c.id, d.c.number, d.c.state).where(d.c.project_id == access.project_id, d.c.number == number)
            )
        ).one()
        if row.state != needs:
            raise HTTPException(409, f"decision {number} of project {project} is {row.state}; {step} needs {needs}")
        await conn.execute(d.update().where(d.c.id == row.id).values(state=leaves))
        build_id = seen.graph.build_id if seen.graph else None
        await _event(conn, request, user, access, row, step, rev=rev + 1, reason=body.reason, build_id=build_id)
        at = await conn.scalar(select(func.now()))
    event = KgDecisionEvent(rev=rev + 1, action=step, by=user.login, at=at, reason=body.reason, build_id=build_id)
    return decision.model_copy(update={"state": leaves, "history": [*decision.history, event]})


async def mcp_answer(request: Request, project: str, user, arguments: dict, *, sink: str) -> tuple[str, dict]:
    """kg_propose: the agent's proposal, for a writer to accept or reject; HTTPException for a refusal."""
    del sink  # the proposal names ids the agent gave and shows no graph data
    try:
        body = NewKgDecision(
            kind=arguments["kind"],
            src=arguments["id"],
            rel=arguments.get("rel"),
            dst=arguments.get("to"),
            reason=arguments["reason"],
            base_rev=arguments["base_rev"],
        )
    except ValidationError as exc:
        raise HTTPException(422, "; ".join(err["msg"] for err in exc.errors())) from None
    made = await create(request, user, project, body, propose=True)
    rev = made.history[-1].rev
    text = f"proposed decision {made.number} of project {project} ({made.kind}); a writer accepts or rejects it"
    return text, {"decision": made.model_dump(mode="json"), "rev": rev}


__all__ = ["KgDecision", "KgDecisionList", "NewKgDecision", "create", "listing", "mcp_answer", "router"]
