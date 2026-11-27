"""The decisions on a project's knowledge graph: who may read and take them (``READERS``, ``WRITERS``), what the
member sees of them (``listing``), a writer's decision or an agent's proposal (``create``), a step on one
(``take_step``), the fixes a loop of the review queue takes (``apply_fixes``) and the MCP tool ``kg_propose``
(``mcp_answer``)."""

from __future__ import annotations

from fastapi import Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_decisions
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.kg_decisions import ABSENT, HIDDEN, MAX_REASON_CHARS, TRANSITIONS, plugin_actor, seen_state
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable
from evo_agents.hub.server.base.scope import Noun, ProjectScope
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg_decisions import repository
from evo_agents.hub.server.kg_decisions.models import (
    Fix,
    KgDecision,
    KgDecisionEvent,
    KgDecisionStep,
    NewKgDecision,
)
from evo_agents.hub.server.kg_web import GraphRef, no_graph, read_graph
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal, holds
from evo_agents.kg.schema import known_relations


class KgDecisionList(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    rev: int = Field(description="the project's decision revision: what a write names as base_rev")
    can_decide: bool = Field(description="true for a writer of the project")
    decisions: list[KgDecision] = Field(description="newest first")


class GraphScope(ProjectScope):
    """A caller's role on a project's knowledge graph: a reader needs a grant on the project, a writer the writer
    role too; the agent of a run proposes with its owner's role (``kg_propose``)."""

    def refusal(self, user: Principal, access: ProjectAccess) -> str | None:
        if access.role is None:
            return f"reading the knowledge graph of project {access.name} needs a grant on it"
        if self.role != "reader" and not holds(access.role, self.role):
            return f"deciding on the knowledge graph of project {access.name} needs the {self.role} role on it"
        return None


NOUN = Noun("a decision on the knowledge graph", "the decisions on the knowledge graph")
READERS = GraphScope("reader", NOUN)
WRITERS = GraphScope("writer", NOUN)


def _stale(project: str, rev: int) -> Conflict:
    return Conflict(f"the decisions of project {project} changed since you read them: their revision is {rev}")


async def _access(request: Request, user, project: str, scope: GraphScope) -> ProjectAccess:
    async with UnitOfWork(request.app.state.engine) as conn:
        return await scope.check(conn, user, project)


async def _rows(conn: AsyncConnection, project_id: int) -> tuple[list, dict[int, list[KgDecisionEvent]], int]:
    rows = await repository.decisions_of(conn, project_id)
    history: dict[int, list[KgDecisionEvent]] = {}
    rev = 0
    for ev in await repository.events_of(conn, project_id):
        rev = ev.rev
        history.setdefault(ev.decision_id, []).append(
            KgDecisionEvent(
                rev=ev.rev,
                action=ev.action,
                by=ev.login or plugin_actor(ev.actor_plugin),
                at=ev.at,
                reason=ev.reason,
                build_id=ev.build_id,
                origin=ev.origin,
                run_id=ev.run_id,
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
    found, ref = await read_graph(request, user, project, lambda session: session.decision_subjects(nodes, edges))
    return found or {}, ref


async def listing(request: Request, user, project: str, state: str | None = None) -> KgDecisionList:
    """The decisions ``user`` can see, newest first, of ``state`` when given."""
    async with UnitOfWork(request.app.state.engine) as conn:
        access = await READERS.check(conn, user, project)
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
                    state=seen,  # pyright: ignore[reportArgumentType]
                    src=row.src,
                    rel=row.rel,
                    dst=row.dst,
                    history=history.get(row.id, []),
                )
            )
    can_decide = WRITERS.refusal(user, access) is None
    return KgDecisionList(graph=ref, rev=rev, can_decide=can_decide, decisions=shown)


async def _locked_rev(conn: AsyncConnection, project_id: int, project: str, base_rev: int) -> int:
    """Lock the project's decisions for this transaction; their revision, which must be ``base_rev`` (else 409)."""
    rev = await repository.locked_rev(conn, project_id)
    if rev != base_rev:
        raise _stale(project, rev)
    return rev


async def _event(
    conn: AsyncConnection,
    request: Request,
    user,
    access: ProjectAccess,
    row,
    action: str,
    *,
    rev,
    reason,
    build_id,
    origin: str | None = None,
    run_id: int | None = None,
    build: bool = True,
) -> None:
    """Write step ``action`` of decision ``row`` and its audit row; queue a build when the active decisions changed,
    unless ``build`` is false."""
    await repository.insert_event(
        conn,
        access.project_id,
        row.id,
        action,
        rev=rev,
        actor_id=user.user_id,
        reason=reason,
        build_id=build_id,
        origin=origin,
        run_id=run_id,
    )
    came = f" origin={origin}" + (f" run={run_id}" if run_id is not None else "") if origin else ""
    target = f"{access.name} decision={row.number} action={action}{came}"
    await Auditor.of(conn, user, access).record(audit.KG_DECISION, target)
    if build and action in ("decide", "accept", "undo"):  # the active decisions changed: the next build applies them
        await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)


async def _visible_subject(request: Request, user, project: str, body: NewKgDecision) -> GraphRef:
    """The latest build of the project's graph, once every node and edge ``body`` names is visible to ``user`` in it;
    404 otherwise, and without a build."""
    found, ref = await _found(request, user, project, [body])
    if ref is None:
        raise no_graph(project)
    nodes, edge = kg_decisions.subject(body.kind, body.src, body.rel, body.dst)
    missing = [key for key in [*nodes, *([edge] if edge else [])] if found.get(key, ABSENT) in (ABSENT, HIDDEN)]
    if missing:
        what = " ".join(missing[0]) if isinstance(missing[0], tuple) else missing[0]
        raise NotFound(f"{what} is not visible to you in the latest graph of project {project}")
    return ref


async def create(request: Request, user, project: str, body: NewKgDecision, *, propose: bool) -> KgDecision:
    """A writer's decision, active at once, or an agent's proposal (``propose``)."""
    access = await _access(request, user, project, WRITERS)
    ref = await _visible_subject(request, user, project, body)
    async with UnitOfWork(request.app.state.engine) as conn:
        if body.kind == "link":
            ontology = await repository.ontology(conn, access.project_id)
            if body.rel not in known_relations(ontology):
                raise Unprocessable(f"the ontology of project {project} has no relation {body.rel}")
        rev = await _locked_rev(conn, access.project_id, project, body.base_rev)
        taken = await repository.saying(conn, access.project_id, body.kind, body.src, body.rel, body.dst)
        if taken is not None:
            raise Conflict(f"decision {taken} of project {project} already says that")
        number = await repository.last_number(conn, access.project_id) + 1
        state, action = ("proposed", "propose") if propose else ("active", "decide")
        row = await repository.insert(conn, access.project_id, number, body.kind, state, body.src, body.rel, body.dst)
        await _event(conn, request, user, access, row, action, rev=rev + 1, reason=body.reason, build_id=ref.build_id)
        at = await repository.now(conn)
    event = KgDecisionEvent(rev=rev + 1, action=action, by=user.login, at=at, reason=body.reason, build_id=ref.build_id)  # pyright: ignore[reportCallIssue]
    return KgDecision(
        number=number, kind=body.kind, state=state, src=body.src, rel=body.rel, dst=body.dst, history=[event]
    )


async def apply_fixes(
    request: Request,
    user,
    access: ProjectAccess,
    fixes: list[Fix],
    *,
    cap: int,
    origin: str,
    run_id: int | None,
    build_id: int | None,
    dry_run: bool,
) -> tuple[list[tuple[Fix, KgDecision | None]], list[Fix], int]:
    """Take the first ``cap`` of ``fixes`` that no proposed or active decision already says, in one transaction under
    the decisions' lock, each active at once with a step that names ``origin`` and ``run_id``, and queue one build.
    Their subjects are the caller's to check. (taken, each with its decision, None on a dry run; skipped, as said
    already or a link the ontology lacks; the revision after)."""
    taken: list[tuple[Fix, KgDecision | None]] = []
    skipped: list[Fix] = []
    async with UnitOfWork(request.app.state.engine) as conn:
        rev = await repository.locked_rev(conn, access.project_id)
        relations = known_relations(await repository.ontology(conn, access.project_id))
        said = await repository.said(conn, access.project_id)
        number = await repository.last_number(conn, access.project_id)
        at = await repository.now(conn)
        for fix in fixes:
            if len(taken) >= cap:
                break
            key = (fix.kind, fix.src, fix.rel, fix.dst)
            if key in said or (fix.kind == "link" and fix.rel not in relations):
                skipped.append(fix)
                continue
            said.add(key)
            if dry_run:
                taken.append((fix, None))
                continue
            number, rev = number + 1, rev + 1
            reason = fix.reason[:MAX_REASON_CHARS]
            row = await repository.insert(
                conn, access.project_id, number, fix.kind, "active", fix.src, fix.rel, fix.dst
            )
            steps = {"rev": rev, "reason": reason, "build_id": build_id, "origin": origin, "run_id": run_id}
            await _event(conn, request, user, access, row, "decide", **steps, build=False)
            event = KgDecisionEvent(action="decide", by=user.login, at=at, **steps)  # pyright: ignore[reportArgumentType]
            made = KgDecision(
                number=number, kind=fix.kind, state="active", src=fix.src, rel=fix.rel, dst=fix.dst, history=[event]
            )
            taken.append((fix, made))
        if taken and not dry_run:  # the active decisions changed: the next build applies them
            await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)
    return taken, skipped, rev


async def take_step(request: Request, user, project: str, number: int, step: str, body: KgDecisionStep) -> KgDecision:
    """Accept or reject a proposal, or undo an active decision, with a reason."""
    access = await _access(request, user, project, WRITERS)
    seen = await listing(request, user, project)
    decision = next((x for x in seen.decisions if x.number == number), None)
    if decision is None:
        raise NotFound(f"no decision {number} is visible to you in project {project}")
    needs, leaves = TRANSITIONS[step]
    async with UnitOfWork(request.app.state.engine) as conn:
        rev = await _locked_rev(conn, access.project_id, project, body.base_rev)
        row = await repository.numbered(conn, access.project_id, number)
        if row.state != needs:
            raise Conflict(f"decision {number} of project {project} is {row.state}; {step} needs {needs}")
        await repository.set_state(conn, row.id, leaves)
        build_id = seen.graph.build_id if seen.graph else None
        await _event(conn, request, user, access, row, step, rev=rev + 1, reason=body.reason, build_id=build_id)
        at = await repository.now(conn)
    event = KgDecisionEvent(rev=rev + 1, action=step, by=user.login, at=at, reason=body.reason, build_id=build_id)  # pyright: ignore[reportCallIssue]
    return decision.model_copy(update={"state": leaves, "history": [*decision.history, event]})


async def mcp_answer(request: Request, project: str, user, arguments: dict, *, sink: str) -> tuple[str, dict]:
    """kg_propose: the agent's proposal, for a writer to accept or reject; an HTTPException for a refusal."""
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
        raise Unprocessable("; ".join(err["msg"] for err in exc.errors())) from None
    made = await create(request, user, project, body, propose=True)
    rev = made.history[-1].rev
    text = f"proposed decision {made.number} of project {project} ({made.kind}); a writer accepts or rejects it"
    return text, {"decision": made.model_dump(mode="json"), "rev": rev}
