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
(when the project has one), why the graph cannot tell for some (``changes.service.tiers.unknown_paths``: at least tier 1), and
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
(``evo_agents.hub.server.curator.service.threads``). A proposal of kind memory may promote agents' memories in place of a plan
(``evo_agents.hub.server.memories.service.promotion``): accepting it writes a shared memory, and makes no plan. Recording a
proposal and answering it each add a line to its ledger (``evo_agents.hub.server.ledger``), the first with the figures that set it off. A proposal of kind ``revert`` with
``revert_of`` is the hub's own, made when the figures of a merged change got worse (``evo_agents.hub.server.outcomes``).
The routes live in ``evo_agents.hub.server.proposals.routes`` (``record``, and ``reads``, which read findings and
proposals and answer a proposal through ``service.reads``), the SQL in ``proposals.repository``, the models in
``evo_agents.hub.server.proposals.models``.

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
from types import SimpleNamespace

from fastapi import HTTPException, Request
from sqlalchemy import null
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.harness import load_schema
from evo_agents.hub import judge, review, runs, tiers, webhook_events
from evo_agents.hub.access import has_role
from evo_agents.hub.plans import PLAN_ID, VERIFY_RULE, PlanProblem, check_body, check_json
from evo_agents.hub.probes import probe_paths
from evo_agents.hub.server import (
    earlier,
    facet_figures,
    system_curation,
)
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server.changes.models.views import Change
from evo_agents.hub.server.changes.service import tiers as change_tiers
from evo_agents.hub.server.changes.service.tiers import graph_view
from evo_agents.hub.server.history.service import search as history_search
from evo_agents.hub.server.kg_audience import person_audience
from evo_agents.hub.server.memories.service import promotion as memory_promotion
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.probes.service import fixes as probe_fixes
from evo_agents.hub.server.probes.service import probes as stored_probes
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.proposals.models import EvidenceIn, Finding, FindingIn, NightFiguresView
from evo_agents.hub.server.proposals.repository import record as store
from evo_agents.hub.server.proposals.repository.record import project_repos
from evo_agents.hub.server.proposals.service.shapes import Proposal, ProposalIn
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.views import visible_plans
from evo_agents.hub.server.security import MACHINE, WEB, Principal
from evo_agents.hub.verify_words import shell_problem
from evo_agents.schema import errors, validate

log = logging.getLogger(__name__)

SHOWN_PROBLEMS = 5


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
    row = await store.review_run_row(conn, run_id)
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
    row = await store.local_run_row(conn, run_id)
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


def night_only(body) -> None:
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
            row = await store.cited_run(conn, item.run_id, run.project_id)
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


def finding_view(project: str, row) -> Finding:
    data = {key: value for key, value in row._mapping.items() if key != "label"}
    return Finding(project=project, **data)


async def record_finding(request: Request, body: FindingIn, held) -> Finding:
    """Record finding ``body`` of the run ``held`` finds (a review run, or a session's local run), with evidence the
    hub finds."""
    async with request.app.state.engine.begin() as conn:
        run = await held(conn)
        run_id = run.id
        access = await writer_of(conn, run)
        label = access.push_label()
        recorded = await store.findings_of_run(conn, run_id)
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
        finding_id = await store.insert_finding(conn, values)
        await write_event(
            conn,
            run_id,
            {"text": f"finding #{finding_id} ({body.lens}, {body.severity}): {body.title}", "finding": finding_id},
        )
        row = await store.finding_row(conn, finding_id)
    log.info("finding recorded", extra={"run_id": run_id, "finding_id": finding_id, "lens": body.lens})
    return finding_view(run.project, row)


# Proposals


def check_draft(plan: dict) -> None:
    """422 unless ``plan`` is a draft plan of plan.schema.json written in outcome steps."""
    where = "the draft plan"
    try:
        check_json(plan)
        check_body(plan)
    except PlanProblem as exc:
        raise _refused(where, str(exc)) from None
    if len(json.dumps(plan, ensure_ascii=False).encode()) > review.MAX_DRAFT_BYTES:
        raise _refused(where, f"it is over {review.MAX_DRAFT_BYTES // 1024} KiB as JSON")
    if not isinstance(plan.get("id"), str) or not re.match(PLAN_ID, plan["id"]):
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


async def planned_tiers_of(conn: AsyncConnection, project_id: int) -> tuple[int, ...]:
    """The tiers whose accepted proposals become the Curator's plans in the project, as its newest charter says
    (``judge.planned_tiers``): tiers 0 to 2 for a project without a charter or a charter that names none."""
    return judge.planned_tiers(await store.charter_body(conn, project_id))


async def _finding_evidence(conn: AsyncConnection, access: ProjectAccess, ids: list[int]) -> int:
    """How many pieces of evidence findings ``ids`` of the project hold; 422 for one the owner cannot see."""
    if not ids:
        return 0
    rows = await store.finding_pieces(conn, access.project_id, ids)
    through = access.rules.hub_sink.sink if access.rules.hub_sink else None
    seen = {row.id: row.pieces for row in rows if through is not None and access.visible(row.label, through)}
    missing = [str(item) for item in ids if item not in seen]
    if missing:
        raise _refused("finding_ids", f"project {access.name} has no finding {', '.join(missing)}")
    return sum(seen.values())


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


async def _announce(conn, proposal_id: int, values: dict) -> None:
    """The ledger's line of proposal ``proposal_id`` just stored with ``values``, and its curator.proposal for the
    project's webhooks."""
    await ledger_lines.proposal_recorded(conn, values["project_id"], proposal_id)
    said = {key: values[key] for key in ("run_id", "kind", "tier", "state")}
    await webhook_events.enqueue(conn, "curator.proposal", values["project_id"], {"proposal_id": proposal_id, **said})


async def record_proposal(request: Request, body: ProposalIn, held) -> Proposal:
    """Record proposal ``body`` of the run ``held`` finds: the hub resolves its evidence, checks its draft plan,
    computes its tier and drops it when it repeats a proposal rejected lately."""
    if body.plan is not None:
        check_draft(body.plan)
    async with request.app.state.engine.begin() as conn:
        run = await held(conn)
        run_id = run.id
        access = await writer_of(conn, run)
        label = access.push_label()
        recorded = await store.proposals_of_run(conn, run_id)
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
        charter = await store.charter_body(conn, run.project_id)
        tier = tiers.tier_of(
            body.kind,
            paths,
            list(charter.get("protected_paths") or []),
            impacted or [],
            unknown=unknown,
            fix_of=body.fix_of,
            evolvable=tiers.evolvable_paths(charter),
        )  # no diff yet (api None): a protected file reached makes it at most tier 2
        fingerprint = tiers.fingerprint(body.kind, paths, body.title)
        rejected = await store.rejected_like(conn, run.project_id, fingerprint)
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
        proposal_id = await store.insert_proposal(conn, values)
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
        await _announce(conn, proposal_id, values)
        await memory_promotion.recorded(conn, run, access, proposal_id, tier.tier, values["state"], promotion)
        what = f"dropped as a repeat of rejected proposal #{rejected.id}" if dropped else f"tier {tier.tier}"
        event = {"text": f"proposal #{proposal_id} ({body.kind}, {what}): {body.title}", "proposal": proposal_id}
        await write_event(conn, run_id, event)
        from evo_agents.hub.server.curator.service.threads import on_open  # it answers through this module

        await on_open(conn, proposal_id)
        row = await store.proposal_row(conn, proposal_id)
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


async def night_figures(engine, user: Principal, project: str, night, sink: str | None) -> NightFiguresView:
    """The figures curator.collect counted for a night of the project: the latest, or ``night``'s."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        row = await store.night_figures(conn, access.project_id, night)
    if row is None or not plan_service.sees(access, access.rules.default_label, sink):
        which = f"for the night of {night.isoformat()}" if night else "yet"
        raise HTTPException(404, f"project {project} has no figures {which}: curator.collect counts them at night")
    return NightFiguresView(project=access.name, **row._mapping)
