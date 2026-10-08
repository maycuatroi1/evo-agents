"""What a review run writes, and the owner's answers: findings, proposals, their tiers, the Inbox items of tier 2, and
the night's figures as members read them. ``evo_agents.hub.review`` holds the model, ``evo_agents.hub.tiers`` the
tier rules, and ``evo_agents.hub.server.collect`` the figures and the review run.

POST /v1/worker/runs/{id}/findings and POST /v1/worker/runs/{id}/proposals take what the agent of a review run the
worker holds records (404 for any other run, a plan run or a run of one step included), as the member who dispatched
it, who must still hold writer on the project; each is stored with the project's default label. A finding has a lens of
``review.LENSES``, a severity, a title, a markdown body, and 1 to MAX_EVIDENCE pieces of evidence. The hub resolves
each piece before it stores anything (422 naming the first it cannot find): a session digest of the project, and the
entry of its list that ``field`` and ``index`` name; an event of a run of the project, of a plan the owner may read or
a review run, whose ``seq`` the run has reached; a file of a repo of the project, which, when the project has a
knowledge graph, the graph must have (``graph_view``). Each piece is kept with how the hub found it (``resolved``).

A proposal names a lens, a kind of change (``tiers.CHANGE_KINDS``), a title, a markdown summary, the paths it would
edit (``{repo, path}``, each a repo of the project), the findings it rests on (of the project) and evidence of its own,
at least one of the two, and a draft plan: a plan of plan.schema.json in outcome steps, each step with ``what``,
``verify`` and a non-empty ``acceptance`` list (422 otherwise, with what the schema says). The hub computes its tier
with ``tiers.tier_of`` from the charter's protected paths, the paths, the paths the knowledge graph says those reach
(when the project has one), and the kind, and keeps the reasons. A proposal whose fingerprint is that of a proposal
rejected in the last REJECTED_DAYS days is stored ``dropped``, with ``duplicate_of`` naming it, unless its evidence (its
own and its findings') counts at least EVIDENCE_FACTOR times the rejected one's. A review run records at most
MAX_FINDINGS findings and MAX_PROPOSALS proposals (409 after that).

When a review run ends, its open tier 2 proposals become Inbox items of its owner (notifications of kind proposal),
the most evidence first, as long as the day has room under the charter's ``max_decisions_per_day``: the proposals that
became Inbox items on the same local day of the charter's time zone count against it; the others stay in the list
alone. A review run that fails sends its owner the notice run_failed (``review_ended``).

Readers of the project read the findings and proposals whose label their grant reaches through the sink X-Evo-Sink
names (else the project's hub sink): GET /v1/projects/{p}/curator/findings and .../findings/{id},
.../curator/proposals (filtered by state, tier, lens and run, newest first, with what each value of those filters
would list) and .../proposals/{id}, and the night's
figures with GET .../curator/figures (the latest night, or ``?night``). POST .../curator/proposals/{id}/answer is for
the project's admins: accept, reject, or defer (for ``defer_days``, DEFAULT_DEFER_DAYS by default), with a note, while
the proposal is open or deferred (409 otherwise); it reads the proposal's notifications and is audited as
curator.proposal. A deferred proposal opens again once its time has passed (the job curator.collect).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Date, cast, func, insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.harness import load_schema
from evo_agents.hub import review, runs, tables, tiers
from evo_agents.hub.access import has_role
from evo_agents.hub.plans import PlanProblem, check_body, check_json
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import notify, run_link
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import RunStep, write_event
from evo_agents.hub.server.runs import LINE, MAX_ID, NOT_HELD, OBJECT_NAME, RunId, _worker_of, visible_plans
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal
from evo_agents.schema import errors, validate

log = logging.getLogger(__name__)

PROPOSAL = "curator.proposal"  # the owner answered: "<project> proposal:<id> run:<id> answer=<accept|reject|defer>"
MAX_LIST = 200
MAX_OFFSET = 100_000
SHOWN_PROBLEMS = 5
GRAPH_DEPTH = 2  # hops of kg_impact a proposal's paths are followed

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

Title = Annotated[str, Field(min_length=1, max_length=review.MAX_TITLE_CHARS, pattern=LINE)]
RepoName = Annotated[str, Field(pattern=review.REPO_NAME)]
ItemId = Annotated[int, Path(ge=1, le=MAX_ID)]


# Models


class EvidenceIn(BaseModel):
    """One piece of evidence, as ``review.parse_evidence`` reads it from the command line."""

    kind: Literal[review.EVIDENCE_KINDS]
    session_id: str | None = Field(None, pattern=review.SESSION_ID, description="session: the digest's session")
    field: Literal[review.DIGEST_FIELDS] | None = Field(None, description="session: the list of the digest")
    index: int | None = Field(None, ge=0, le=10_000, description="session: the entry of that list, from 0")
    run_id: int | None = Field(None, ge=1, le=MAX_ID, description="run: the run")
    seq: int | None = Field(None, ge=1, le=2**31 - 1, description="run: the seq of its event")
    repo: RepoName | None = Field(None, description="code: a repo of the project")
    path: str | None = Field(None, min_length=1, max_length=tiers.MAX_PATH_CHARS, description="code: within the repo")
    line: int | None = Field(None, ge=1, le=review.MAX_LINE, description="code: the line")
    commit: str | None = Field(None, pattern=OBJECT_NAME, description="code: the commit the worktree was at")

    @model_validator(mode="after")
    def _complete(self):
        needed = {"session": ("session_id",), "run": ("run_id", "seq"), "code": ("repo", "path")}[self.kind]
        missing = [name for name in needed if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.kind} evidence needs {', '.join(missing)}")
        if (self.field is None) != (self.index is None):
            raise ValueError("a digest's entry is named by field and index together")
        if self.path is not None:
            found = tiers.normalize_path(self.path)
            if found is None:
                raise ValueError(f"{self.path!r} is not a path within a repo")
            self.path = found
        return self

    def stored(self) -> dict:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class FindingIn(BaseModel):
    lens: Literal[tuple(review.LENSES)]
    severity: Literal[review.SEVERITIES] = "medium"
    title: Title
    body: str | None = Field(None, min_length=1, max_length=review.MAX_BODY_BYTES, description="markdown, 16 KiB")
    evidence: list[EvidenceIn] = Field(min_length=1, max_length=review.MAX_EVIDENCE)

    @model_validator(mode="after")
    def _bounded(self):
        if self.body is not None and len(self.body.encode()) > review.MAX_BODY_BYTES:
            raise ValueError(f"a finding's body is at most {review.MAX_BODY_BYTES} bytes of UTF-8")
        return self


class Finding(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the review run that recorded it")
    lens: str
    severity: Literal[review.SEVERITIES]
    title: str
    body: str | None
    evidence: list[dict] = Field(description="each piece as the hub resolved it, with how (resolved)")
    created_at: datetime


class FindingList(BaseModel):
    findings: list[Finding] = Field(description="newest first")
    total: int
    limit: int
    offset: int


class PathIn(BaseModel):
    repo: RepoName
    path: str = Field(min_length=1, max_length=tiers.MAX_PATH_CHARS)

    @model_validator(mode="after")
    def _normal(self):
        found = tiers.normalize_path(self.path)
        if found is None:
            raise ValueError(f"{self.path!r} is not a path within a repo")
        self.path = found
        return self


class ProposalIn(BaseModel):
    lens: Literal[tuple(review.LENSES)]
    kind: Literal[tuple(tiers.CHANGE_KINDS)] = Field(description="the kind of change, which gives its first tier")
    title: Title
    summary: str | None = Field(None, min_length=1, max_length=review.MAX_BODY_BYTES, description="markdown, 16 KiB")
    paths: list[PathIn] = Field(default_factory=list, max_length=tiers.MAX_PATHS, description="what it would edit")
    finding_ids: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        default_factory=list, max_length=review.MAX_FINDING_IDS, description="findings of the project it rests on"
    )
    evidence: list[EvidenceIn] = Field(default_factory=list, max_length=review.MAX_EVIDENCE)
    plan: dict = Field(description="the draft plan: plan.schema.json, in outcome steps")

    @model_validator(mode="after")
    def _supported(self):
        if not self.finding_ids and not self.evidence:
            raise ValueError("a proposal rests on findings, evidence of its own, or both: give at least one")
        if self.summary is not None and len(self.summary.encode()) > review.MAX_BODY_BYTES:
            raise ValueError(f"a proposal's summary is at most {review.MAX_BODY_BYTES} bytes of UTF-8")
        self.finding_ids = list(dict.fromkeys(self.finding_ids))
        return self


class RepoPath(BaseModel):
    repo: str
    path: str


class ProposalSummary(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the review run that proposed it")
    lens: str
    kind: str
    title: str
    tier: int = Field(ge=0, le=3, description="as the hub's tier rules computed it")
    state: Literal[review.PROPOSAL_STATES]
    evidence_count: int = Field(description="the pieces of evidence it rests on, its findings' included")
    duplicate_of: int | None = Field(description="the rejected proposal a dropped one repeats")
    answered_by: str | None
    answered_at: datetime | None
    deferred_until: datetime | None
    inbox_at: datetime | None = Field(description="when it became an Inbox item of the owner; tier 2 only")
    created_at: datetime


class Proposal(ProposalSummary):
    summary: str | None
    paths: list[RepoPath]
    impacted: list[RepoPath] | None = Field(description="what the paths reach in the knowledge graph; null without one")
    tier_reasons: list[str] = Field(description="why it has its tier, rule by rule")
    finding_ids: list[int]
    evidence: list[dict] = Field(description="its own evidence, as the hub resolved it")
    plan: dict = Field(description="the draft plan")
    note: str | None = Field(description="the owner's note with the answer")


class ProposalCounts(BaseModel):
    """How many proposals each value of a filter would list, the other filters applied: every state, tier and lens
    named, with 0 where none is."""

    state: dict[str, int]
    tier: dict[str, int] = Field(description='by tier, "0" to "3"')
    lens: dict[str, int]


class ProposalList(BaseModel):
    proposals: list[ProposalSummary] = Field(description="newest first")
    total: int
    limit: int
    offset: int
    counts: ProposalCounts | None = Field(None, description="for the list's filters: what each of their values holds")


class ProposalAnswer(BaseModel):
    action: Literal[review.ANSWERS]
    note: str | None = Field(None, min_length=1, max_length=review.MAX_NOTE_CHARS, pattern=LINE)
    defer_days: int | None = Field(
        None, ge=review.DEFER_DAYS[0], le=review.DEFER_DAYS[1], description="with defer; 7 by default"
    )

    @model_validator(mode="after")
    def _days(self):
        if self.defer_days is not None and self.action != "defer":
            raise ValueError("defer_days goes with the action defer")
        return self


class NightFiguresView(BaseModel):
    project: str
    night: date
    since: datetime
    until: datetime
    run_id: int | None = Field(description="the review run queued on them, if any")
    figures: dict = Field(description="the figures, as curator.collect counted them")
    created_at: datetime


# The run and its owner


@dataclass(frozen=True)
class _ReviewRun:
    id: int
    project_id: int
    project: str
    owner: Principal


async def _held_review_run(conn: AsyncConnection, user: Principal, run_id: int) -> _ReviewRun:
    """The review run ``run_id`` the worker of ``user`` holds, its row locked; 404 for any other run."""
    worker_id = (await _worker_of(conn, user))[0]
    r, p, u = tables.runs, tables.projects, tables.users
    query = (
        select(r.c.state, r.c.worker_id, r.c.kind, r.c.project_id, p.c.name, r.c.dispatched_by, u.c.login)
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
        .with_for_update(of=r)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind != "review":
        raise HTTPException(
            404, f"run {run_id} is not a review run: only the agent of a review run records findings and proposals"
        )
    owner = Principal(row.dispatched_by, row.login, False, user.token_id, MACHINE, "")
    return _ReviewRun(run_id, row.project_id, row.name, owner)


async def _writer(conn: AsyncConnection, run: _ReviewRun) -> ProjectAccess:
    access = await project_access(conn, run.owner, run.project)
    if not has_role(access.role, "writer"):
        raise HTTPException(
            403, f"{run.owner.login}, who owns run {run.id}, no longer holds the writer role on project {run.project}"
        )
    return access


# The knowledge graph


@dataclass(frozen=True)
class GraphView:
    """What the project's knowledge graph says of some paths: those it has no file for, and the files their edits
    reach."""

    unmatched: frozenset[str]
    impacted: frozenset[tuple[str, str]]


async def graph_view(request: Request, owner: Principal, access: ProjectAccess, targets: list[str]) -> GraphView | None:
    """``kg_impact`` of ``targets`` (``repo:path`` each) in the project's latest graph, read as ``owner`` through the
    project's hub sink; None when there is no target, or the project has no graph or it cannot be read now."""
    if not targets:
        return None
    from evo_agents.hub.server import kg  # its routes import the plans and runs modules

    sink = access.rules.hub_sink.sink if access.rules.hub_sink else kg.CLI_SINK
    arguments = {"paths": targets, "depth": GRAPH_DEPTH}
    try:
        result = await kg.tool_result(request.app.state, owner, access.name, "kg_impact", arguments, sink)
    except HTTPException as exc:
        log.info("no knowledge graph for the review", extra={"project": access.name, "status": exc.status_code})
        return None
    data = result.get("structuredContent")
    if result.get("isError") or not isinstance(data, dict):
        return None
    reached = set()
    for item in data.get("impacted") or []:
        props = ((item or {}).get("node") or {}).get("props") or {}
        if isinstance(props.get("repo"), str) and isinstance(props.get("path"), str):
            reached.add((props["repo"], props["path"]))
    unmatched = {str(path) for path in data.get("unmatched") or []}
    return GraphView(frozenset(unmatched), frozenset(reached))


# Evidence


def _refused(where: str, why: str) -> HTTPException:
    return HTTPException(422, f"{where}: {why}; nothing was recorded")


async def _project_repos(conn: AsyncConnection, project_id: int) -> set[str]:
    pr = tables.project_repos
    return set((await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id))).scalars())


async def resolve(
    conn: AsyncConnection, request: Request, run: _ReviewRun, access: ProjectAccess, items: list[EvidenceIn]
) -> list[dict]:
    """Each piece of ``items`` as the hub found it, with ``resolved`` saying how; 422 for the first it cannot find."""
    d, r = tables.session_digests, tables.runs
    plans = await visible_plans(conn, access, None)
    repos = await _project_repos(conn, run.project_id)
    code = [f"{item.repo}:{item.path}" for item in items if item.kind == "code"]
    graph = await graph_view(request, run.owner, access, sorted(set(code))) if code else None
    found = []
    for position, item in enumerate(items, start=1):
        where = f"evidence {position} ({review.evidence_text(item.stored())})"
        stored = item.stored()
        if item.kind == "session":
            query = select(d.c.id, d.c.body).where(d.c.project_id == run.project_id, d.c.session_id == item.session_id)
            row = (await conn.execute(query)).one_or_none()
            if row is None:
                raise _refused(where, f"project {run.project} has no digest of session {item.session_id}")
            if item.field is not None:
                listed = row.body.get(item.field) if isinstance(row.body, dict) else None
                if not isinstance(listed, list) or item.index >= len(listed):
                    size = len(listed) if isinstance(listed, list) else 0
                    raise _refused(
                        where, f"the digest's {item.field} has {size} entries, so none at index {item.index}"
                    )
            found.append({**stored, "resolved": "digest"})
        elif item.kind == "run":
            query = select(r.c.event_seq, r.c.kind, r.c.plan_id).where(
                r.c.id == item.run_id, r.c.project_id == run.project_id
            )
            row = (await conn.execute(query)).one_or_none()
            if row is None or (row.kind != "review" and row.plan_id not in plans):
                raise _refused(where, f"project {run.project} has no run {item.run_id}")
            if item.seq > row.event_seq:
                raise _refused(where, f"run {item.run_id} has {row.event_seq} events, so none with seq {item.seq}")
            found.append({**stored, "resolved": "run_event"})
        else:
            if item.repo not in repos:
                known = ", ".join(sorted(repos)) or "none"
                raise _refused(where, f"project {run.project} has no repo {item.repo} (its repos: {known})")
            target = f"{item.repo}:{item.path}"
            if graph is not None and target in graph.unmatched:
                raise _refused(where, f"the knowledge graph of project {run.project} has no file {target}")
            found.append({**stored, "resolved": "repo" if graph is None else "graph"})
    return found


# Findings


def _findings():
    f = tables.findings
    return select(
        f.c.id,
        f.c.run_id,
        f.c.lens,
        f.c.severity,
        f.c.title,
        f.c.body,
        f.c.evidence,
        f.c.created_at,
        f.c.label,
    )


def _finding(project: str, row) -> Finding:
    data = {key: value for key, value in row._mapping.items() if key != "label"}
    return Finding(project=project, **data)


@worker_router.post("/runs/{run_id}/findings", status_code=201, response_model=Finding, responses=REFUSALS)
async def record_finding(request: Request, run_id: RunId, body: FindingIn, user: CurrentUser) -> Finding:
    """Record a finding of the review run this worker holds, with evidence the hub finds."""
    f = tables.findings
    async with request.app.state.engine.begin() as conn:
        run = await _held_review_run(conn, user, run_id)
        access = await _writer(conn, run)
        label = access.push_label()
        recorded = (await conn.execute(select(func.count()).where(f.c.run_id == run_id))).scalar_one()
        if recorded >= review.MAX_FINDINGS:
            raise HTTPException(
                409, f"run {run_id} recorded {review.MAX_FINDINGS} findings already, the most a run may"
            )
        evidence = await resolve(conn, request, run, access, body.evidence)
        values = {
            "project_id": run.project_id,
            "run_id": run_id,
            "lens": body.lens,
            "severity": body.severity,
            "title": body.title,
            "body": body.body,
            "evidence": evidence,
            "label": label,
        }
        finding_id = (await conn.execute(insert(f).values(**values).returning(f.c.id))).scalar_one()
        await write_event(
            conn,
            run_id,
            {"text": f"finding #{finding_id} ({body.lens}, {body.severity}): {body.title}", "finding": finding_id},
        )
        row = (await conn.execute(_findings().where(f.c.id == finding_id))).one()
    log.info("finding recorded", extra={"run_id": run_id, "finding_id": finding_id, "lens": body.lens})
    return _finding(run.project, row)


# Proposals


def _check_draft(plan: dict) -> None:
    """422 unless ``plan`` is a draft plan of plan.schema.json written in outcome steps."""
    where = "the draft plan"
    try:
        check_json(plan)
        check_body(plan)
    except PlanProblem as exc:
        raise _refused(where, str(exc)) from None
    if len(json.dumps(plan, ensure_ascii=False).encode()) > review.MAX_DRAFT_BYTES:
        raise _refused(where, f"it is over {review.MAX_DRAFT_BYTES // 1024} KiB as JSON")
    if not isinstance(plan.get("id"), str) or not re.match(plan_routes.PLAN_ID, plan["id"]):
        raise _refused(where, "its id must be a plan id: lower case letters, digits and -, at most 100")
    found = errors(validate(plan, load_schema("plan")))
    if found:
        shown = "; ".join(f"{issue.path or '<root>'}: {issue.message}" for issue in found[:SHOWN_PROBLEMS])
        raise _refused(where, f"it does not match plan.schema.json: {shown}")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise _refused(where, "it has no steps")
    for index, step in enumerate(steps, start=1):
        key = step.get("id", index) if isinstance(step, dict) else index
        if not isinstance(step, dict):
            raise _refused(where, f"step {key} is not a mapping")
        acceptance = step.get("acceptance")
        if (
            not isinstance(acceptance, list)
            or not acceptance
            or not all(isinstance(a, str) and a.strip() for a in acceptance)
        ):
            raise _refused(
                where,
                f"step {key} is not an outcome step: give it acceptance, a list of what is true once it is done",
            )
        if not isinstance(step.get("verify"), str) or not step["verify"].strip():
            raise _refused(where, f"step {key} has no verify: an outcome step says how it is checked")


async def _protected(conn: AsyncConnection, project_id: int) -> list[str]:
    c = tables.charters
    body = (
        await conn.execute(select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1))
    ).scalar_one_or_none()
    return list((body or {}).get("protected_paths") or [])


async def _finding_evidence(conn: AsyncConnection, access: ProjectAccess, ids: list[int]) -> int:
    """How many pieces of evidence findings ``ids`` of the project hold; 422 for one the owner cannot see."""
    if not ids:
        return 0
    f = tables.findings
    rows = (
        await conn.execute(
            select(f.c.id, f.c.label, func.jsonb_array_length(f.c.evidence).label("pieces")).where(
                f.c.project_id == access.project_id, f.c.id.in_(ids)
            )
        )
    ).all()
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    seen = {row.id: row.pieces for row in rows if through is not None and access.visible(row.label, through)}
    missing = [str(item) for item in ids if item not in seen]
    if missing:
        raise _refused("finding_ids", f"project {access.name} has no finding {', '.join(missing)}")
    return sum(seen.values())


async def _rejected_like(conn: AsyncConnection, project_id: int, fingerprint: str):
    p = tables.proposals
    query = (
        select(p.c.id, p.c.evidence_count)
        .where(
            p.c.project_id == project_id,
            p.c.fingerprint == fingerprint,
            p.c.state == "rejected",
            p.c.answered_at >= func.now() - timedelta(days=tiers.REJECTED_DAYS),
        )
        .order_by(p.c.answered_at.desc())
        .limit(1)
    )
    return (await conn.execute(query)).one_or_none()


def _proposals():
    p, u = tables.proposals, tables.users.alias("answerer")
    return select(
        p.c.id,
        p.c.run_id,
        p.c.lens,
        p.c.kind,
        p.c.title,
        p.c.tier,
        p.c.state,
        p.c.evidence_count,
        p.c.duplicate_of,
        u.c.login.label("answered_by"),
        p.c.answered_at,
        p.c.deferred_until,
        p.c.inbox_at,
        p.c.created_at,
        p.c.summary,
        p.c.paths,
        p.c.impacted,
        p.c.tier_reasons,
        p.c.finding_ids,
        p.c.evidence,
        p.c.draft.label("plan"),
        p.c.note,
        p.c.label,
    ).select_from(p.outerjoin(u, u.c.id == p.c.answered_by))


def _proposal(project: str, row) -> Proposal:
    data = {key: value for key, value in row._mapping.items() if key != "label"}
    data["finding_ids"] = list(data["finding_ids"] or [])
    return Proposal(project=project, **data)


def _summary(project: str, row) -> ProposalSummary:
    data = {key: row._mapping[key] for key in ProposalSummary.model_fields if key != "project"}
    return ProposalSummary(project=project, **data)


@worker_router.post("/runs/{run_id}/proposals", status_code=201, response_model=Proposal, responses=REFUSALS)
async def record_proposal(request: Request, run_id: RunId, body: ProposalIn, user: CurrentUser) -> Proposal:
    """Record a proposal of the review run this worker holds: the hub resolves its evidence, checks its draft plan,
    computes its tier and drops it when it repeats a proposal rejected lately."""
    _check_draft(body.plan)
    p = tables.proposals
    async with request.app.state.engine.begin() as conn:
        run = await _held_review_run(conn, user, run_id)
        access = await _writer(conn, run)
        label = access.push_label()
        recorded = (await conn.execute(select(func.count()).where(p.c.run_id == run_id))).scalar_one()
        if recorded >= review.MAX_PROPOSALS:
            raise HTTPException(
                409, f"run {run_id} recorded {review.MAX_PROPOSALS} proposals already, the most a run may"
            )
        repos = await _project_repos(conn, run.project_id)
        unknown = sorted({item.repo for item in body.paths} - repos)
        if unknown:
            raise _refused("paths", f"project {run.project} has no repo {', '.join(unknown)}")
        evidence = await resolve(conn, request, run, access, body.evidence)
        count = len(evidence) + await _finding_evidence(conn, access, body.finding_ids)
        paths = list(dict.fromkeys((item.repo, item.path) for item in body.paths))
        graph = await graph_view(request, run.owner, access, [f"{repo}:{path}" for repo, path in paths])
        impacted = sorted(graph.impacted) if graph is not None else None
        tier = tiers.tier_of(body.kind, paths, await _protected(conn, run.project_id), impacted or [])
        fingerprint = tiers.fingerprint(body.kind, paths, body.title)
        rejected = await _rejected_like(conn, run.project_id, fingerprint)
        dropped = rejected is not None and tiers.drops(count, rejected.evidence_count)
        values = {
            "project_id": run.project_id,
            "run_id": run_id,
            "lens": body.lens,
            "kind": body.kind,
            "title": body.title,
            "summary": body.summary,
            "paths": [{"repo": repo, "path": path} for repo, path in paths],
            "impacted": null() if impacted is None else [{"repo": repo, "path": path} for repo, path in impacted],
            "tier": tier.tier,
            "tier_reasons": tier.reasons,
            "finding_ids": body.finding_ids,
            "evidence": evidence,
            "evidence_count": count,
            "draft": body.plan,
            "fingerprint": fingerprint,
            "state": "dropped" if dropped else "open",
            "duplicate_of": rejected.id if dropped else None,
            "label": label,
        }
        proposal_id = (await conn.execute(insert(p).values(**values).returning(p.c.id))).scalar_one()
        what = f"dropped as a repeat of rejected proposal #{rejected.id}" if dropped else f"tier {tier.tier}"
        event = {"text": f"proposal #{proposal_id} ({body.kind}, {what}): {body.title}", "proposal": proposal_id}
        await write_event(conn, run_id, event)
        row = (await conn.execute(_proposals().where(p.c.id == proposal_id))).one()
    log.info(
        "proposal recorded",
        extra={"run_id": run_id, "proposal_id": proposal_id, "tier": tier.tier, "dropped": dropped},
    )
    return _proposal(run.project, row)


# When a review run ends


async def _inbox_room(conn: AsyncConnection, project_id: int, charter: dict) -> int:
    """How many more tier 2 proposals of the project may become Inbox items today, in the charter's time zone."""
    p = tables.proposals
    zone = charter["window"]["timezone"]
    today = cast(func.timezone(zone, func.now()), Date)
    used = (
        await conn.execute(
            select(func.count()).where(
                p.c.project_id == project_id,
                p.c.inbox_at.is_not(None),
                cast(func.timezone(zone, p.c.inbox_at), Date) == today,
            )
        )
    ).scalar_one()
    return max(0, int(charter.get("max_decisions_per_day", 0)) - used)


async def review_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> list[int]:
    """What the end of review run ``found`` does: the notice run_failed when it failed, and its open tier 2
    proposals as Inbox items of its owner while the day has room (see the module's docstring); their ids."""
    if new not in runs.TERMINAL_STATES:
        return []
    project_id = found.project_id
    if new == "failed":
        error = found.error or reason
        await notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=project_id,
            run_id=found.run_id,
            title=f"Review run #{found.run_id} of {found.project} failed",
            body=error,
            details={"run_kind": "review", "error": error},
            link=run_link(found.project, found.run_id),
        )
    c, p = tables.charters, tables.proposals
    charter = (
        await conn.execute(select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1))
    ).scalar_one_or_none()
    if charter is None:
        return []
    room = await _inbox_room(conn, project_id, charter)
    if room <= 0:
        return []
    waiting = (
        await conn.execute(
            select(p.c.id, p.c.title, p.c.kind, p.c.lens, p.c.summary, p.c.evidence_count)
            .where(p.c.run_id == found.run_id, p.c.tier == 2, p.c.state == "open", p.c.inbox_at.is_(None))
            .order_by(p.c.evidence_count.desc(), p.c.id)
            .limit(room)
        )
    ).all()
    for row in waiting:
        await conn.execute(update(p).values(inbox_at=func.now()).where(p.c.id == row.id))
        await notify(
            conn,
            user_id=found.dispatcher_id,
            kind="proposal",
            project_id=project_id,
            run_id=found.run_id,
            proposal_id=row.id,
            title=f"Proposal #{row.id} (tier 2): {row.title}",
            body=row.summary,
            details={"tier": 2, "kind": row.kind, "lens": row.lens, "evidence": row.evidence_count},
            link=proposal_link(row.id),
        )
    if waiting:
        log.info("proposals in the inbox", extra={"run_id": found.run_id, "proposals": [row.id for row in waiting]})
    return [row.id for row in waiting]


def proposal_link(proposal_id: int) -> str:
    """Where the web shows a proposal and its answer buttons."""
    return f"/inbox?proposal={proposal_id}"


# Reading


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the Curator of project {access.name} needs a grant on it")


@router.get("/{project}/curator/findings", response_model=FindingList, responses=READ_REFUSALS)
async def list_findings(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    run_id: Annotated[int | None, Query(ge=1, le=MAX_ID, description="of this review run")] = None,
    lens: Annotated[Literal[tuple(review.LENSES)] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> FindingList:
    """The findings of the project's review runs the caller may read, newest first."""
    f = tables.findings
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = _findings().where(f.c.project_id == access.project_id)
        if run_id is not None:
            query = query.where(f.c.run_id == run_id)
        if lens is not None:
            query = query.where(f.c.lens == lens)
        rows = (await conn.execute(query.order_by(f.c.id.desc()).limit(MAX_OFFSET))).all()
    through = plan_routes._sink(access, sink)
    visible = [_finding(access.name, row) for row in rows if access.visible(row.label, through)]
    return FindingList(findings=visible[offset : offset + limit], total=len(visible), limit=limit, offset=offset)


@router.get("/{project}/curator/findings/{finding_id}", response_model=Finding, responses=READ_REFUSALS)
async def show_finding(
    request: Request,
    project: ProjectName,
    finding_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> Finding:
    f = tables.findings
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = _findings().where(f.c.project_id == access.project_id, f.c.id == finding_id)
        row = (await conn.execute(query)).one_or_none()
    if row is None or not access.visible(row.label, plan_routes._sink(access, sink)):
        raise HTTPException(404, f"project {project} has no finding {finding_id}")
    return _finding(access.name, row)


@router.get("/{project}/curator/proposals", response_model=ProposalList, responses=READ_REFUSALS)
async def list_proposals(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[list[Literal[review.PROPOSAL_STATES]], Query(description="any of these; repeat it")] = [],  # noqa: B006
    tier: Annotated[list[Annotated[int, Field(ge=0, le=3)]], Query(description="any of these tiers; repeat it")] = [],  # noqa: B006
    lens: Annotated[Literal[tuple(review.LENSES)] | None, Query()] = None,
    run_id: Annotated[int | None, Query(ge=1, le=MAX_ID, description="of this review run")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> ProposalList:
    """The proposals of the project's review runs the caller may read, newest first."""
    p = tables.proposals
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = _proposals().where(p.c.project_id == access.project_id)
        if run_id is not None:
            query = query.where(p.c.run_id == run_id)
        rows = (await conn.execute(query.order_by(p.c.id.desc()).limit(MAX_OFFSET))).all()
    through = plan_routes._sink(access, sink)
    readable = [row for row in rows if access.visible(row.label, through)]
    states, tiers_asked = set(state), set(tier)

    def kept(row, but: str | None = None) -> bool:
        return (
            (but == "state" or not states or row.state in states)
            and (but == "tier" or not tiers_asked or row.tier in tiers_asked)
            and (but == "lens" or lens is None or row.lens == lens)
        )

    def counted(facet: str, values) -> dict[str, int]:
        found = Counter(str(getattr(row, facet)) for row in readable if kept(row, facet))
        return {str(value): found.get(str(value), 0) for value in values}

    counts = ProposalCounts(
        state=counted("state", review.PROPOSAL_STATES),
        tier=counted("tier", range(4)),
        lens=counted("lens", review.LENSES),
    )
    visible = [_summary(access.name, row) for row in readable if kept(row)]
    return ProposalList(
        proposals=visible[offset : offset + limit], total=len(visible), limit=limit, offset=offset, counts=counts
    )


async def _readable_proposal(conn, access: ProjectAccess, proposal_id: int, sink: str | None, *, lock: bool = False):
    p = tables.proposals
    query = _proposals().where(p.c.project_id == access.project_id, p.c.id == proposal_id)
    if lock:
        query = query.with_for_update(of=p)
    row = (await conn.execute(query)).one_or_none()
    if row is None or not access.visible(row.label, plan_routes._sink(access, sink)):
        raise HTTPException(404, f"project {access.name} has no proposal {proposal_id}")
    return row


@router.get("/{project}/curator/proposals/{proposal_id}", response_model=Proposal, responses=READ_REFUSALS)
async def show_proposal(
    request: Request,
    project: ProjectName,
    proposal_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> Proposal:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        row = await _readable_proposal(conn, access, proposal_id, sink)
    return _proposal(access.name, row)


async def answer_proposal_as(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    proposal_id: int,
    body: ProposalAnswer,
    *,
    via: str | None = None,
) -> Proposal:
    """Accept, reject or defer proposal ``proposal_id`` of ``project`` as ``user``, an admin of it, in the caller's
    transaction, and read its notifications. The answer route and the Telegram channel (``via="telegram"``, which the
    audit row names) both answer through it; it raises the HTTPException the route answers with."""
    p, n = tables.proposals, tables.notifications
    access = await project_access(conn, user, project)
    _reader(access)
    if not has_role(access.role, "admin"):
        raise HTTPException(
            403, f"answering a proposal of project {access.name} needs the admin role on it; you hold {access.role}"
        )
    row = await _readable_proposal(conn, access, proposal_id, None, lock=True)
    if row.state not in ("open", "deferred"):
        raise HTTPException(409, f"proposal {proposal_id} is {row.state}: only an open or deferred one is answered")
    state = review.ANSWERED[body.action]
    days = body.defer_days or review.DEFAULT_DEFER_DAYS
    values = {
        "state": state,
        "answered_by": user.user_id,
        "answered_at": func.now(),
        "note": body.note,
        "deferred_until": func.now() + timedelta(days=days) if body.action == "defer" else None,
    }
    await conn.execute(update(p).values(**values).where(p.c.id == proposal_id))
    await conn.execute(
        update(n).values(read_at=func.now()).where(n.c.proposal_id == proposal_id, n.c.read_at.is_(None))
    )
    target = f"{access.name} proposal:{proposal_id} run:{row.run_id} answer={body.action}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=PROPOSAL,
        target=target if via is None else f"{target} via={via}",
        project_id=access.project_id,
    )
    row = await _readable_proposal(conn, access, proposal_id, None)
    return _proposal(access.name, row)


@router.post("/{project}/curator/proposals/{proposal_id}/answer", response_model=Proposal, responses=REFUSALS)
async def answer_proposal(
    request: Request, project: ProjectName, proposal_id: ItemId, body: ProposalAnswer, user: CurrentUser
) -> Proposal:
    """Accept, reject or defer a proposal; the admins of the project alone may."""
    async with request.app.state.engine.begin() as conn:
        answered = await answer_proposal_as(conn, user, project, proposal_id, body)
    log.info("proposal answered", extra={"proposal_id": proposal_id, "action": body.action, "login": user.login})
    return answered


@router.get("/{project}/curator/figures", response_model=NightFiguresView, responses=READ_REFUSALS)
async def show_figures(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    night: Annotated[date | None, Query(description="the night, the local date its window opened; the latest")] = None,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> NightFiguresView:
    """The figures curator.collect counted for a night of the project: the latest, or ``night``'s."""
    cf = tables.curator_figures
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = select(cf.c.night, cf.c.since, cf.c.until, cf.c.run_id, cf.c.figures, cf.c.created_at).where(
            cf.c.project_id == access.project_id
        )
        if night is not None:
            query = query.where(cf.c.night == night)
        row = (await conn.execute(query.order_by(cf.c.night.desc()).limit(1))).one_or_none()
    if row is None or not access.visible(access.rules.default_label, plan_routes._sink(access, sink)):
        which = f"for the night of {night.isoformat()}" if night else "yet"
        raise HTTPException(404, f"project {project} has no figures {which}: curator.collect counts them at night")
    return NightFiguresView(project=access.name, **row._mapping)
