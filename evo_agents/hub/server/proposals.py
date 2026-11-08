"""What a review run writes, and the owner's answers: findings, proposals, their tiers, the Inbox items of tier 2, and
the night's figures as members read them. ``evo_agents.hub.review`` holds the model, ``evo_agents.hub.tiers`` the
tier rules, and ``evo_agents.hub.server.collect`` the figures and the review run.

POST /v1/worker/runs/{id}/findings and POST /v1/worker/runs/{id}/proposals take what the agent of a review run the
worker holds records (404 for any other run, a plan run or a run of one step included), as the member who dispatched
it, who must still hold writer on the project; each is stored with the project's default label. A finding has a lens of
``review.LENSES``, a severity, a title, a markdown body, and 1 to MAX_EVIDENCE pieces of evidence. The hub resolves
each piece before it stores anything (422 naming the first it cannot find): a session of the project, and the entry of
the list that ``field`` and ``index`` name, in the session's facet when the hub made one (``evo_agents.hub.facet_job``),
else in its digest; a line of a transcript of the project the hub holds and the owner may read (``transcript``); an
event of a run of the project, of a plan the owner may read, a review run or a local run of the owner, whose ``seq`` the
run has reached; a file of a repo of the project, ``graph`` or ``unmatched`` (kept: the graph may lag) when the project
has a knowledge graph (``graph_view``). Each piece is kept with how the hub found it. A finding like a closed one of
earlier nights whose evidence that one cited already is refused (409, ``earlier.closed_like``).

A member's own session records findings and proposals the same way, outside any night (the curator skill): POST
/v1/projects/{p}/curator/findings and .../curator/proposals take the same bodies with ``run_id``, the session's own
local run (``runs.LOCAL_KIND``, registered by the evo-hub hooks), which must be the caller's, of that project, and not
ended (404 otherwise); a machine or web credential only, never a worker's or a run's (403), and the writer role. The
session's local run takes the place of the review run: its log gets the event, the caps per run hold, and its proposals
get their tier, their ledger line and, with ``answer_by`` agent, their thread, as a review run's do. ``fix_of`` is the
night's alone (422): a fix proposal answers a probe the night's review run was assigned. A session's open tier 2
proposals become no Inbox items, since no review run of theirs ends; the member who recorded them answers them.

A proposal names a lens, a kind of change (``tiers.CHANGE_KINDS``), a title, a markdown summary, the paths it would
edit (``{repo, path}``, each a repo of the project), the findings it rests on (of the project) and evidence of its own,
at least one of the two, and a draft plan: a plan of plan.schema.json in outcome steps, each step with ``what``,
``verify`` and a non-empty ``acceptance`` list (422 otherwise, with what the schema says). The hub computes its tier
with ``tiers.tier_of`` from the charter's protected paths, the paths, the paths the knowledge graph says those reach
(when the project has one), why the graph cannot tell for some (``change_tiers.unknown_paths``: at least tier 1), and
the kind, and keeps the reasons. A proposal whose fingerprint is that of a proposal
rejected in the last REJECTED_DAYS days is stored ``dropped``, with ``duplicate_of`` naming it, unless its evidence (its
own and its findings') counts at least EVIDENCE_FACTOR times the rejected one's. A review run records at most
MAX_FINDINGS findings and MAX_PROPOSALS proposals (409 after that).

When a review run ends, its open tier 2 proposals become Inbox items of its owner (notifications of kind proposal),
the most evidence first, as long as the day has room under the charter's ``max_decisions_per_day``: the proposals that
became Inbox items on the same local day of the charter's time zone count against it; the others stay in the list
alone. A review run that fails sends its owner the notice run_failed (``review_inbox.review_ended``).

Readers of the project read the findings and proposals whose label their grant reaches through the sink X-Evo-Sink
names (else the project's hub sink): GET /v1/projects/{p}/curator/findings and .../findings/{id},
.../curator/proposals (filtered by state, tier, lens and run, newest first, with what each value of those filters
would list) and .../proposals/{id}, whose ``change`` is the change accepting it made (``changes.Change``: its plan,
branch, pull request and why it waits) when the caller may read that change's plan, and the night's figures with GET
.../curator/figures (the latest night, or ``?night``). POST .../curator/proposals/{id}/answer is for
the project's admins: accept, reject, or defer (for ``defer_days``, DEFAULT_DEFER_DAYS by default), with a note, while
the proposal is open or deferred (409 otherwise); it reads the proposal's notifications and is audited as
curator.proposal. Accepting a proposal of a tier the charter's ``planned_tiers`` names (``planned_tiers_of``: tiers 0
to 2 unless it names others, tier 3 included) also makes its draft the Curator's plan, which the night shift builds on
a branch of its own (``evo_agents.hub.server.proposal_plans``, whose POST .../proposals/{id}/plan does the same for an
accepted proposal that has no change yet); a draft that cannot become one leaves a change open with the reason, and
nothing of it runs. Accepting one of another tier records the answer alone. The hub merges a change of a tier the
charter's ``auto_merge`` names alone (``evo_agents.hub.server.changes``). A deferred proposal opens again once its time
has passed (the job curator.collect). With the charter's ``answer_by`` agent, a proposal recorded open, or open
again, gets a thread in the project's triage chat whose agent answers it for the review run's owner
(``evo_agents.hub.server.curator_threads``). A proposal of kind memory may promote agents' memories in place of a plan
(``evo_agents.hub.server.memory_promotion``): accepting it writes a shared memory, and makes no plan. Recording a
proposal and answering it each add a line to its ledger (``evo_agents.hub.server.ledger``), the first with the figures that set it off. A proposal of kind ``revert`` with
``revert_of`` is the hub's own, made when the figures of a merged change got worse (``evo_agents.hub.server.outcomes``).
The routes that read findings and proposals and answer a proposal live in ``evo_agents.hub.server.proposal_reads``,
the models in ``evo_agents.hub.server.proposal_models``.

A proposal may carry a probe (``evo_agents.hub.server.probes``): a task in a repo it changes, from a base commit, and a
one-line verify that exits 0 when an agent given the task does not repeat the mistake. One whose evidence cites an entry
of ``corrections`` in the night's figures and whose paths include a file a probe arm can place
(``evo_agents.hub.probes.probe_path``) is refused without one (422). Accepting it gives the probe its change, and a
proposal's view shows its active probe, the verify to the project's admins alone.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import func, insert, null, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.harness import load_schema
from evo_agents.hub import judge, review, runs, tables, tiers
from evo_agents.hub.access import has_role
from evo_agents.hub.plans import VERIFY_RULE, PlanProblem, check_body, check_json
from evo_agents.hub.probes import probe_paths
from evo_agents.hub.server import (
    change_tiers,
    earlier,
    facet_figures,
    history_search,
    memory_promotion,
    probe_fixes,
    system_curation,
)
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server import probes as stored_probes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.change_tiers import GraphView, graph_view  # noqa: F401 (tests read proposals.GraphView)
from evo_agents.hub.server.change_views import Change
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg_audience import person_audience
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.proposal_models import (
    EvidenceIn,
    Finding,
    FindingIn,
    NightFiguresView,
    Proposal,
    ProposalIn,
)
from evo_agents.hub.server.review_inbox import proposal_link, review_ended  # noqa: F401 (imported from here)
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import MAX_ID, NOT_HELD, RunId
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.views import visible_plans
from evo_agents.hub.server.security import MACHINE, WEB, CurrentUser, Principal
from evo_agents.hub.verify_words import shell_problem
from evo_agents.schema import errors, validate

log = logging.getLogger(__name__)

SHOWN_PROBLEMS = 5

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}


# The run and its owner


@dataclass(frozen=True)
class _ReviewRun:
    id: int
    project_id: int
    project: str
    owner: Principal


async def held_review_run(conn: AsyncConnection, user: Principal, run_id: int) -> _ReviewRun:
    """The review run ``run_id`` the worker of ``user`` holds, its row locked; 404 for any other run."""
    worker_id = (await worker_of(conn, user))[0]
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


SESSION_KINDS = (MACHINE, WEB)  # the credentials a member's session records findings and proposals with


async def session_review_run(conn: AsyncConnection, user: Principal, project: str, run_id: int) -> _ReviewRun:
    """The local run ``run_id`` of ``user``'s own session in ``project``, its row locked, standing for a review run;
    403 for a worker's or a run's credential, 404 for any other run or one that ended."""
    if user.kind not in SESSION_KINDS:
        raise HTTPException(
            403,
            "a session records findings and proposals with a member's credential; a review run's agent uses "
            "POST /v1/worker/runs/{id}/findings and .../proposals",
        )
    r, p = tables.runs, tables.projects
    query = (
        select(r.c.state, r.c.kind, r.c.dispatched_by, r.c.project_id, p.c.name)
        .join_from(r, p, p.c.id == r.c.project_id)
        .where(r.c.id == run_id)
        .with_for_update(of=r)
    )
    row = (await conn.execute(query)).one_or_none()
    if (
        row is None
        or row.name != project
        or row.kind != runs.LOCAL_KIND
        or row.dispatched_by != user.user_id
        or row.state == "ended"
    ):
        raise HTTPException(
            404,
            f"run {run_id} is no live local run of yours in project {project}: a session records findings and "
            "proposals in its own local run (`evo-agents hub curator finding|propose` finds it)",
        )
    owner = Principal(user.user_id, user.login, False, user.token_id, user.kind, "")
    return _ReviewRun(run_id, row.project_id, row.name, owner)


def _night_only(body) -> None:
    if body.fix_of is not None:
        raise _refused(
            "fix_of", "a fix proposal answers a probe assigned to the night's review run; a session gives none"
        )


async def writer_of(conn: AsyncConnection, run: _ReviewRun) -> ProjectAccess:
    access = await project_access(conn, run.owner, run.project)
    if not has_role(access.role, "writer"):
        raise HTTPException(
            403, f"{run.owner.login}, who owns run {run.id}, no longer holds the writer role on project {run.project}"
        )
    return access


# Evidence


def _refused(where: str, why: str) -> HTTPException:
    return HTTPException(422, f"{where}: {why}; nothing was recorded")


async def project_repos(conn: AsyncConnection, project_id: int) -> set[str]:
    pr = tables.project_repos
    return set((await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id))).scalars())


def _readable_run(row, plans: list[str], owner_id: int) -> bool:
    """A review run, a run of a plan the owner may read, or the owner's own local run (the figures count its facet)."""
    return row.kind == "review" or row.plan_id in plans or (row.kind == "local" and row.dispatched_by == owner_id)


async def _session_evidence(conn: AsyncConnection, run: _ReviewRun, item: EvidenceIn, where: str) -> str:
    """Where session evidence ``item`` is: its facet, else its digest, the digest first for its skills
    (``facet_figures.lists_of``); 422 otherwise."""
    held = await facet_figures.lists_of(conn, run.project_id, item.session_id, item.field)
    if held is None:
        raise _refused(where, f"project {run.project} has no digest of session {item.session_id}, nor a facet of it")
    what, body = held
    listed = body.get(item.field) if item.field is not None and isinstance(body, dict) else None
    if item.field is not None and (not isinstance(listed, list) or item.index >= len(listed)):
        size = len(listed) if isinstance(listed, list) else 0
        raise _refused(where, f"the {what}'s {item.field} has {size} entries, so none at index {item.index}")
    return what


async def resolve(
    conn: AsyncConnection, request: Request, run: _ReviewRun, access: ProjectAccess, items: list[EvidenceIn]
) -> list[dict]:
    """Each piece of ``items`` as the hub found it, with ``resolved`` saying how; 422 for the first it cannot find."""
    r = tables.runs
    plans = await visible_plans(conn, access, None)
    repos = await project_repos(conn, run.project_id)
    code = [f"{item.repo}:{item.path}" for item in items if item.kind == "code"]
    graph = await graph_view(request, run.owner, access, sorted(set(code))) if code else None
    found = []
    held = await person_audience(conn, run.owner.user_id) if any(item.kind == "memory" for item in items) else None
    for position, item in enumerate(items, start=1):
        where = f"evidence {position} ({review.evidence_text(item.stored())})"
        stored = item.stored()
        if item.kind == "session":
            found.append({**stored, "resolved": await _session_evidence(conn, run, item, where)})
        elif item.kind == "transcript":
            why = await history_search.line_missing(conn, access, item.session_id, item.file, item.line)
            if why is not None:
                raise _refused(where, why)
            found.append({**stored, "resolved": "transcript"})
        elif item.kind == "memory":
            found.append(await memory_promotion.evidence(conn, run.project_id, run.project, access, held, item, where))
        elif item.kind == "run":
            query = select(r.c.event_seq, r.c.kind, r.c.plan_id, r.c.dispatched_by).where(
                r.c.id == item.run_id, r.c.project_id == run.project_id
            )
            row = (await conn.execute(query)).one_or_none()
            if row is None or not _readable_run(row, plans, run.owner.user_id):
                raise _refused(where, f"project {run.project} has no run {item.run_id}")
            if item.seq > row.event_seq:
                raise _refused(where, f"run {item.run_id} has {row.event_seq} events, so none with seq {item.seq}")
            found.append({**stored, "resolved": "run_event"})
        elif item.kind == "span":
            assert item.span_id is not None  # the model asks a span reference for its span_id
            system = await system_curation.span_system(conn, run.project_id, item.span_id)
            if system is None:
                raise _refused(where, f"no trace of an agent system of project {run.project} has span {item.span_id}")
            found.append({**stored, "resolved": "span", "system": system})
        else:
            if item.repo not in repos:
                known = ", ".join(sorted(repos)) or "none"
                raise _refused(where, f"project {run.project} has no repo {item.repo} (its repos: {known})")
            target = f"{item.repo}:{item.path}"
            held = "repo" if graph is None else "unmatched" if target in graph.unmatched else "graph"
            found.append({**stored, "resolved": held})  # a file the graph lacks yet is kept, not refused
    return found


# Findings


def findings_query():
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
        f.c.fix_of,
        f.c.label,
    )


def finding_view(project: str, row) -> Finding:
    data = {key: value for key, value in row._mapping.items() if key != "label"}
    return Finding(project=project, **data)


@worker_router.post("/runs/{run_id}/findings", status_code=201, response_model=Finding, responses=REFUSALS)
async def record_finding(request: Request, run_id: RunId, body: FindingIn, user: CurrentUser) -> Finding:
    """Record a finding of the review run this worker holds, with evidence the hub finds."""
    return await _record_finding(request, body, lambda conn: held_review_run(conn, user, run_id))


class SessionFindingIn(FindingIn):
    run_id: int = Field(ge=1, le=MAX_ID, description="the local run of your session that records it")


@router.post("/{project}/curator/findings", status_code=201, response_model=Finding, responses=REFUSALS)
async def record_session_finding(
    request: Request, project: ProjectName, body: SessionFindingIn, user: CurrentUser
) -> Finding:
    """Record a finding of your own session, in its local run, as a review run's agent records one."""
    _night_only(body)
    return await _record_finding(request, body, lambda conn: session_review_run(conn, user, project, body.run_id))


async def _record_finding(request: Request, body: FindingIn, held) -> Finding:
    f = tables.findings
    async with request.app.state.engine.begin() as conn:
        run = await held(conn)
        run_id = run.id
        access = await writer_of(conn, run)
        label = access.push_label()
        recorded = (await conn.execute(select(func.count()).where(f.c.run_id == run_id))).scalar_one()
        if recorded >= review.MAX_FINDINGS:
            raise HTTPException(
                409, f"run {run_id} recorded {review.MAX_FINDINGS} findings already, the most a run may"
            )
        evidence = await resolve(conn, request, run, access, body.evidence)
        if body.fix_of is not None:
            evidence += (await _fix_assigned(conn, run, access, body.fix_of)).results
        closed = await earlier.closed_like(conn, access, body.lens, body.title, evidence)
        if closed is not None:
            raise HTTPException(409, earlier.REPEATED.format(id=closed.id, lens=closed.lens, status=closed.status))
        values = {
            "project_id": run.project_id,
            "run_id": run_id,
            "lens": body.lens,
            "severity": body.severity,
            "title": body.title,
            "body": body.body,
            "evidence": evidence,
            "label": label,
            "fix_of": body.fix_of,
        }
        finding_id = (await conn.execute(insert(f).values(**values).returning(f.c.id))).scalar_one()
        await write_event(
            conn,
            run_id,
            {"text": f"finding #{finding_id} ({body.lens}, {body.severity}): {body.title}", "finding": finding_id},
        )
        row = (await conn.execute(findings_query().where(f.c.id == finding_id))).one()
    log.info("finding recorded", extra={"run_id": run_id, "finding_id": finding_id, "lens": body.lens})
    return finding_view(run.project, row)


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
        unrunnable = shell_problem(step["verify"])
        if unrunnable is not None:
            raise _refused(where, f"the verify of step {key} {unrunnable}; {VERIFY_RULE}")


async def _charter_body(conn: AsyncConnection, project_id: int) -> dict:
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def _protected(conn: AsyncConnection, project_id: int) -> list[str]:
    return list((await _charter_body(conn, project_id)).get("protected_paths") or [])


async def planned_tiers_of(conn: AsyncConnection, project_id: int) -> tuple[int, ...]:
    """The tiers whose accepted proposals become the Curator's plans in the project, as its newest charter says
    (``judge.planned_tiers``): tiers 0 to 2 for a project without a charter or a charter that names none."""
    return judge.planned_tiers(await _charter_body(conn, project_id))


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


def proposals_query():
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
        p.c.revert_of,
        p.c.fix_of,
        p.c.promotion,
        p.c.system_change,
    ).select_from(p.outerjoin(u, u.c.id == p.c.answered_by))


def proposal_view(
    project: str, row, change: Change | None = None, probe: stored_probes.Probe | None = None
) -> Proposal:
    data = {key: value for key, value in row._mapping.items() if key != "label"}
    data["finding_ids"] = list(data["finding_ids"] or [])
    return Proposal(project=project, change=change, probe=probe, **data)


async def _probe_needed(conn: AsyncConnection, run_id: int, body: ProposalIn, evidence: list, paths: list) -> None:
    """422 when the proposal carries no probe, though its evidence cites a correction of the night's figures and its
    paths include a file a probe arm can place (``probes.probe_path``); and when its probe names a repo it does not
    change."""
    if body.probe is not None:
        named = {repo for repo, _ in paths}
        if named and body.probe.repo not in named:
            raise _refused("probe", f"its repo {body.probe.repo} is none of the repos the proposal changes")
        return
    placed = probe_paths(path for _, path in paths)
    if not placed:
        return
    cited = SimpleNamespace(run_id=run_id, evidence=evidence, finding_ids=body.finding_ids)
    if await ledger_lines.cites_corrections(conn, cited):
        raise _refused(
            "probe",
            "the proposal's evidence cites a correction of the session figures and it changes "
            f"{', '.join(placed)}, which a probe arm can place: give it a probe (repo, commit, task, verify) that "
            "says whether an agent repeats the mistake",
        )


async def _fix_assigned(conn: AsyncConnection, run: _ReviewRun, access: ProjectAccess, change_id: int):
    """The fix assignment of change ``change_id`` for review run ``run`` (``probe_fixes.fix_assignment``); 422
    without one."""
    night = await probe_fixes.run_night(conn, run.id)
    found = await probe_fixes.fix_assignment(conn, access, run.owner, night, change_id)
    if found is None:
        raise _refused(
            "fix_of",
            f"change {change_id} of project {run.project} has no probe that recurred on one model in the two weekly "
            "reports before this run's week: the hub asks for a fix proposal only then",
        )
    return found


@worker_router.post("/runs/{run_id}/proposals", status_code=201, response_model=Proposal, responses=REFUSALS)
async def record_proposal(request: Request, run_id: RunId, body: ProposalIn, user: CurrentUser) -> Proposal:
    """Record a proposal of the review run this worker holds: the hub resolves its evidence, checks its draft plan,
    computes its tier and drops it when it repeats a proposal rejected lately."""
    return await _record_proposal(request, body, lambda conn: held_review_run(conn, user, run_id))


class SessionProposalIn(ProposalIn):
    run_id: int = Field(ge=1, le=MAX_ID, description="the local run of your session that records it")


@router.post("/{project}/curator/proposals", status_code=201, response_model=Proposal, responses=REFUSALS)
async def record_session_proposal(
    request: Request, project: ProjectName, body: SessionProposalIn, user: CurrentUser
) -> Proposal:
    """Record a proposal of your own session, in its local run, as a review run's agent records one: the same
    evidence, draft plan, tier and repeat checks."""
    _night_only(body)
    return await _record_proposal(request, body, lambda conn: session_review_run(conn, user, project, body.run_id))


async def _record_proposal(request: Request, body: ProposalIn, held) -> Proposal:
    if body.plan is not None:
        _check_draft(body.plan)
    p = tables.proposals
    async with request.app.state.engine.begin() as conn:
        run = await held(conn)
        run_id = run.id
        access = await writer_of(conn, run)
        label = access.push_label()
        recorded = (await conn.execute(select(func.count()).where(p.c.run_id == run_id))).scalar_one()
        if recorded >= review.MAX_PROPOSALS:
            raise HTTPException(
                409, f"run {run_id} recorded {review.MAX_PROPOSALS} proposals already, the most a run may"
            )
        repos = await project_repos(conn, run.project_id)
        unknown = sorted({item.repo for item in body.paths} - repos)
        if unknown:
            raise _refused("paths", f"project {run.project} has no repo {', '.join(unknown)}")
        if body.probe is not None and body.probe.repo not in repos:
            raise _refused("probe", f"project {run.project} has no repo {body.probe.repo}")
        evidence = await resolve(conn, request, run, access, body.evidence)
        if body.fix_of is not None:
            evidence += (await _fix_assigned(conn, run, access, body.fix_of)).results
            if (found := await probe_fixes.open_fix(conn, body.fix_of)) is not None:
                raise HTTPException(
                    409,
                    f"change {body.fix_of} has open fix proposal #{found} already: a change has one open fix proposal "
                    "at a time",
                )
        promotion, label = await memory_promotion.checked(conn, run, access, body.promotion, evidence, label)
        change = await system_curation.checked(conn, run.project_id, body.lens, body.kind, body.system_change, evidence)
        count = len(evidence) + await _finding_evidence(conn, access, body.finding_ids)
        paths = list(dict.fromkeys((item.repo, item.path) for item in body.paths))
        await _probe_needed(conn, run_id, body, evidence, paths)
        graph = await graph_view(request, run.owner, access, [f"{repo}:{path}" for repo, path in paths])
        impacted = sorted(graph.impacted) if graph is not None else None
        unknown = await change_tiers.unknown_paths(conn, run.project_id, paths, graph)
        tier = tiers.tier_of(
            body.kind,
            paths,
            await _protected(conn, run.project_id),
            impacted or [],
            unknown=unknown,
            fix_of=body.fix_of,
        )
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
            "draft": body.plan or {},
            "promotion": promotion,
            "fingerprint": fingerprint,
            "state": "dropped" if dropped else "open",
            "duplicate_of": rejected.id if dropped else None,
            "label": label,
            "fix_of": body.fix_of,
            "system_change": null() if change is None else change,
        }
        proposal_id = (await conn.execute(insert(p).values(**values).returning(p.c.id))).scalar_one()
        if body.probe is not None:
            await stored_probes.store(
                conn,
                project_id=run.project_id,
                probe=body.probe,
                source="proposal",
                label=label,
                proposal_id=proposal_id,
                user_id=run.owner.user_id,
                run_id=run_id,
            )
        await ledger_lines.proposal_recorded(conn, run.project_id, proposal_id)
        await memory_promotion.recorded(conn, run, access, proposal_id, tier.tier, values["state"], promotion)
        what = f"dropped as a repeat of rejected proposal #{rejected.id}" if dropped else f"tier {tier.tier}"
        event = {"text": f"proposal #{proposal_id} ({body.kind}, {what}): {body.title}", "proposal": proposal_id}
        await write_event(conn, run_id, event)
        from evo_agents.hub.server.curator_threads import on_open  # it answers through this module

        await on_open(conn, proposal_id)
        row = (await conn.execute(proposals_query().where(p.c.id == proposal_id))).one()
        probe = await stored_probes.of_proposal(conn, run.project, proposal_id, shown=False)
    log.info(
        "proposal recorded",
        extra={"run_id": run_id, "proposal_id": proposal_id, "tier": tier.tier, "dropped": dropped},
    )
    return proposal_view(run.project, row, probe=probe)


# Reading


def require_reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the Curator of project {access.name} needs a grant on it")


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
        require_reader(access)
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
