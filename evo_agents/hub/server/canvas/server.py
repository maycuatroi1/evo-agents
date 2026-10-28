"""The routes of the plugin canvas, under /v1/x/canvas/<project>/ for a member with a grant on a project that enabled
it (the hub checks both first, ``evo_agents.hub.server.plugin_routes``; a project that did not enable it answers 404).

Readers read the canvases, a canvas whole with its nine cells, a claim whole with every rev, its provenance and diff
and its evidence, the diff of two revs, what judging runs of @strategist had for a claim with each verdict and why,
the proposals, and the export. Writers make a canvas, edit its claims (each
named with the rev it was read at, else a 409 that holds what changed), undo a claim's current rev, add evidence and
withdraw it, confirm evidence an agent added, accept or reject a proposal (or reject several together, with one
reason), hand @strategist a brainstorm, import
``evo-canvas/1``, read the baselines and decide their items (never one they changed the status of, unless the
config's ``self_approval`` is on), and open a baseline of one claim at once. The agent of a run gets none of these routes: it works through the plugin's tools
(``agent``), and a run's token is refused on every decision. Admins give a new canvas a label of its own, and
archive a canvas or unarchive it. Reads go through the sink X-Evo-Sink names, else the project's hub sink. A refusal
is the hub's error body, with the claims of a conflict or the problems of an import in ``detail``; nothing is written.
A write that changes a canvas or its claims queues a build of the project's knowledge graph (``kg``) in its
transaction, joining one that waits.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.canvas import baselines, decisions, judging, notices, proposals, store, transfer, writes
from evo_agents.hub.server.canvas.baseline_models import (
    CanvasBaseline,
    CanvasBaselineDecisionIn,
    CanvasBaselineOpenIn,
    CanvasBaselineSummary,
    CanvasSnapshotClaim,
    item_model,
)
from evo_agents.hub.server.canvas.baseline_models import summary_values as baseline_values
from evo_agents.hub.server.canvas.brainstorm import hand_brainstorm
from evo_agents.hub.server.canvas.content import CanvasError, code, number_of
from evo_agents.hub.server.canvas.models import (
    CanvasBrainstorm,
    CanvasBrainstormIn,
    CanvasClaim,
    CanvasClaimDetail,
    CanvasDetail,
    CanvasDiff,
    CanvasEditIn,
    CanvasEditResult,
    CanvasEvidence,
    CanvasEvidenceIn,
    CanvasExport,
    CanvasImportIn,
    CanvasImportResult,
    CanvasIn,
    CanvasJudging,
    CanvasProposal,
    CanvasProposalLeft,
    CanvasRejected,
    CanvasRejectIn,
    CanvasRejectManyIn,
    CanvasSummary,
    CanvasUndoIn,
    CanvasWithdrawIn,
    changes_model,
    claim_model,
    content_model,
    detail_model,
    evidence_model,
    import_model,
    judging_model,
    proposal_model,
    rev_models,
    summary_values,
)
from evo_agents.hub.server.canvas.store import CanvasRow, ClaimRow
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.tables_canvas import PROPOSAL_STATUSES, SLUG

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422)}
UNIQUE_VIOLATION = "23505"  # the SQLSTATE of a unique key two writes took at once
DEADLOCK = "40P01"  # two writes of a project that waited on each other (a claim's number, the project's events)
RACES = (UNIQUE_VIOLATION, DEADLOCK)
RACE = "another write in the project took a claim's number or a canvas's slug at the same moment: try again"
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
Slug = Annotated[str, Path(pattern=SLUG, description="the canvas's slug")]
Ref = Annotated[str, Path(min_length=1, max_length=20, description="the claim's code, as CL-3 or 3")]
EvidenceId = Annotated[int, Path(ge=1)]
ProposalId = Annotated[int, Path(ge=1)]
BaselineNumber = Annotated[int, Path(ge=1, description="the baseline's number in the canvas")]
Sink = Annotated[str | None, Header(alias=store.SINK_HEADER)]
Archived = Annotated[bool, Query(description="archived ones too")]

router = APIRouter(tags=["plugins"], responses={401: {"model": ErrorBody}, **REFUSALS})


async def _answer(request: Request, work):
    """``work(conn)`` in one transaction; a refusal as the hub's error body, nothing written."""
    try:
        async with request.app.state.engine.begin() as conn:
            return await work(conn)
    except CanvasError as exc:
        return error_response(request, exc.status, exc.message, detail=exc.detail)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in RACES:
            raise
        return error_response(request, 409, RACE)


async def _graph_changed(request: Request, conn: AsyncConnection, access: ProjectAccess) -> None:
    """Queue a build of the project's knowledge graph, whose canvases and claims the plugin's source gives (``kg``)."""
    await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)


async def _opened(conn: AsyncConnection, user, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    store.reader(access)
    return access


async def _writing(conn: AsyncConnection, user, project: str, slug: str, sink: str | None, what: str):
    """The access, the canvas ``slug`` locked and open to writes, and the writer, for a writer of the project."""
    access = await _opened(conn, user, project)
    store.writer(access, what)
    canvas = store.writable(access, await store.canvas(conn, access, slug, sink, lock=True))
    return access, canvas, writes.person(access, user.user_id, sink)


async def _claim_models(conn, access: ProjectAccess, rows: list[ClaimRow], sink) -> list[CanvasClaim]:
    evidence = await store.evidence(conn, [row.id for row in rows])
    return [
        claim_model(row, [item for item in evidence[row.id] if store.visible(access, item.label, sink)]) for row in rows
    ]


async def _detail(conn, access: ProjectAccess, canvas: CanvasRow, with_archived: bool, sink) -> CanvasDetail:
    rows = await store.claims(conn, canvas.id, with_archived=with_archived)
    statuses = (await store.counts(conn, [canvas.id]))[canvas.id]
    return detail_model(canvas, statuses, await _claim_models(conn, access, rows, sink))


async def _claim_detail(conn, access: ProjectAccess, canvas: CanvasRow, row: ClaimRow, sink) -> CanvasClaimDetail:
    evidence = (await store.evidence(conn, [row.id]))[row.id]
    shown = [item for item in evidence if store.visible(access, item.label, sink)]
    return CanvasClaimDetail(
        **claim_model(row, shown).model_dump(),
        canvas=canvas.slug,
        revs=rev_models(await store.revs(conn, row.id)),
        evidence=[evidence_model(item) for item in shown],
        hidden_evidence=len(evidence) - len(shown),
    )


async def _edited(conn, access: ProjectAccess, canvas: CanvasRow, outcomes, sink) -> CanvasEditResult:
    rows = [await store.claim(conn, access, canvas, outcome.number) for outcome in outcomes]
    written = [code(outcome.number) for outcome in outcomes if outcome.written]
    return CanvasEditResult(claims=await _claim_models(conn, access, rows, sink), written=written)


def _label(access: ProjectAccess, label) -> dict | None:
    if label is None:
        return None
    if not has_role(access.role, "admin"):
        raise CanvasError(403, f"giving a canvas a label of its own in project {access.name} takes an admin")
    return label.model_dump(exclude_none=True)


# Reading


@router.get("/{project}/canvases", response_model=list[CanvasSummary])
async def list_canvases(
    request: Request, project: ProjectName, user: CurrentUser, archived: Archived = False, sink: Sink = None
):
    """The canvases of the project the caller sees, by slug, with how many claims hold each status."""

    async def work(conn):
        access = await _opened(conn, user, project)
        rows = await store.canvases(conn, access, sink, with_archived=archived)
        statuses = await store.counts(conn, [row.id for row in rows])
        return [CanvasSummary(**summary_values(row, statuses[row.id])) for row in rows]

    return await _answer(request, work)


@router.get("/{project}/canvases/{slug}", response_model=CanvasDetail)
async def show_canvas(
    request: Request, project: ProjectName, slug: Slug, user: CurrentUser, archived: Archived = False, sink: Sink = None
):
    """A canvas whole: its nine cells in order, each with its claims by place; archived claims with ``archived``."""

    async def work(conn):
        access = await _opened(conn, user, project)
        canvas = await store.canvas(conn, access, slug, sink)
        return await _detail(conn, access, canvas, archived, sink)

    return await _answer(request, work)


@router.get("/{project}/canvases/{slug}/claims/{ref}", response_model=CanvasClaimDetail)
async def show_claim(
    request: Request, project: ProjectName, slug: Slug, ref: Ref, user: CurrentUser, sink: Sink = None
):
    """A claim whole: every rev with its content, provenance and what it changed, and its evidence, each piece with
    whether it is real; pieces above what the caller may read are counted only."""

    async def work(conn):
        access = await _opened(conn, user, project)
        canvas = await store.canvas(conn, access, slug, sink)
        row = await store.claim(conn, access, canvas, number_of(ref))
        return await _claim_detail(conn, access, canvas, row, sink)

    return await _answer(request, work)


@router.get("/{project}/canvases/{slug}/claims/{ref}/judgings", response_model=list[CanvasJudging])
async def list_judgings(
    request: Request, project: ProjectName, slug: Slug, ref: Ref, user: CurrentUser, sink: Sink = None
):
    """What judging runs of @strategist had for a claim, newest first: each piece of feedback or evidence, and once
    judged its verdict and why. Items above what the caller may read are left out."""

    async def work(conn):
        access = await _opened(conn, user, project)
        canvas = await store.canvas(conn, access, slug, sink)
        row = await store.claim(conn, access, canvas, number_of(ref))
        found = await judging.judgings(conn, row.id)
        return [judging_model(item) for item in found if store.visible(access, item.label, sink)]

    return await _answer(request, work)


@router.get("/{project}/canvases/{slug}/claims/{ref}/diff", response_model=CanvasDiff)
async def diff_claim(
    request: Request,
    project: ProjectName,
    slug: Slug,
    ref: Ref,
    user: CurrentUser,
    base: Annotated[int | None, Query(ge=1, description="a rev; the one before head when left out")] = None,
    head: Annotated[int | None, Query(ge=1, description="a rev; the claim's current when left out")] = None,
    sink: Sink = None,
):
    """What differs between two revs of a claim, field by field."""

    async def work(conn):
        access = await _opened(conn, user, project)
        row = await store.claim(conn, access, await store.canvas(conn, access, slug, sink), number_of(ref))
        to = head or row.rev
        since = base or max(to - 1, 1)
        if since > to:
            raise CanvasError(422, f"base rev {since} comes after head rev {to}")
        found = {rev.rev: rev for rev in await store.revs(conn, row.id, upto=to, since=since)}
        if since not in found or to not in found:
            raise CanvasError(404, f"{row.code} is at rev {row.rev}: it has no rev {to if to not in found else since}")
        old, new = found[since].content, found[to].content
        return CanvasDiff(
            claim=row.code,
            base=since,
            head=to,
            base_content=content_model(old),
            head_content=content_model(new),
            changes=changes_model(old, new),
        )

    return await _answer(request, work)


@router.get("/{project}/canvases/{slug}/export", response_model=CanvasExport)
async def export_canvas(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """The canvas as evo-canvas/1: every claim with its code, rev and content, and the evidence the caller may
    read."""

    async def work(conn):
        access = await _opened(conn, user, project)
        return await transfer.export(conn, access, await store.canvas(conn, access, slug, sink), sink)

    return await _answer(request, work)


# Writing


@router.post("/{project}/canvases", response_model=CanvasDetail, status_code=201)
async def create_canvas(request: Request, project: ProjectName, body: CanvasIn, user: CurrentUser, sink: Sink = None):
    """A new canvas of a product, with nine empty cells, under the project's label unless an admin gives another."""

    async def work(conn):
        access = await _opened(conn, user, project)
        store.writer(access, "making a canvas")
        writer = writes.person(access, user.user_id, sink)
        await writes.create_canvas(conn, writer, body.slug, body.name, body.description, _label(access, body.label))
        await _graph_changed(request, conn, access)
        return await _detail(conn, access, await store.canvas(conn, access, body.slug, sink), False, sink)

    return await _answer(request, work)


async def _archived(request: Request, project: str, slug: str, user, sink: str | None, archived: bool):
    async def work(conn):
        access = await _opened(conn, user, project)
        store.admin(access, "archiving a canvas" if archived else "unarchiving a canvas")
        canvas = await store.canvas(conn, access, slug, sink, lock=True)
        await writes.set_archived(conn, access, user.user_id, canvas, archived)
        await _graph_changed(request, conn, access)
        row = await store.canvas(conn, access, slug, sink)
        return CanvasSummary(**summary_values(row, (await store.counts(conn, [row.id]))[row.id]))

    return await _answer(request, work)


@router.post("/{project}/canvases/{slug}/archive", response_model=CanvasSummary)
async def archive_canvas(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """Archive a canvas, as an admin of the project: it is read still, and takes no change until unarchived."""
    return await _archived(request, project, slug, user, sink, True)


@router.post("/{project}/canvases/{slug}/unarchive", response_model=CanvasSummary)
async def unarchive_canvas(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """Open an archived canvas to changes again, as an admin of the project."""
    return await _archived(request, project, slug, user, sink, False)


@router.post("/{project}/canvases/{slug}/edit", response_model=CanvasEditResult)
async def edit_claims(
    request: Request, project: ProjectName, slug: Slug, body: CanvasEditIn, user: CurrentUser, sink: Sink = None
):
    """Change claims of the canvas and make new ones, all or nothing. A change of a claim names the rev it was read
    at (``base_rev``): when the claim changed since, the answer is a 409 whose detail holds, per claim, both contents,
    who changed it and the diff, and nothing is written. Each change writes a rev with the caller as its author; a
    claim goes to validated only with real evidence that supports it, to testing only with a test card that holds a
    hypothesis, a test and a deadline."""

    async def work(conn):
        access, canvas, writer = await _writing(conn, user, project, slug, sink, "editing claims")
        changes = [writes.Change(item.claim, item.base_rev, item.values()) for item in body.changes]
        outcomes = await writes.edit(conn, writer, canvas, changes, body.reason)
        if any(outcome.written for outcome in outcomes):
            await _graph_changed(request, conn, access)
        return await _edited(conn, access, canvas, outcomes, sink)

    return await _answer(request, work)


@router.post("/{project}/canvases/{slug}/undo", response_model=CanvasEditResult)
async def undo_claims(
    request: Request, project: ProjectName, slug: Slug, body: CanvasUndoIn, user: CurrentUser, sink: Sink = None
):
    """Take back the current rev of each claim named, all or nothing: a new rev with the content of the rev before
    it (undoing a first rev archives the claim). A rev that is no longer the claim's current is a 409."""

    async def work(conn):
        access, canvas, writer = await _writing(conn, user, project, slug, sink, "undoing a change")
        targets = [(item.claim, item.rev) for item in body.revs]
        outcomes = await writes.undo(conn, writer, canvas, targets, body.reason)
        await _graph_changed(request, conn, access)
        return await _edited(conn, access, canvas, outcomes, sink)

    return await _answer(request, work)


@router.post("/{project}/canvases/{slug}/claims/{ref}/evidence", response_model=CanvasEvidence, status_code=201)
async def add_evidence(
    request: Request,
    project: ProjectName,
    slug: Slug,
    ref: Ref,
    body: CanvasEvidenceIn,
    user: CurrentUser,
    sink: Sink = None,
):
    """Add a piece of evidence to a claim, as the caller's; the claim's rev stays. A simulation is always synthetic."""

    async def work(conn):
        access, canvas, writer = await _writing(conn, user, project, slug, sink, "adding evidence")
        row = await store.claim(conn, access, canvas, number_of(ref))
        given = writes.EvidenceInput(**body.model_dump())
        evidence_id = await writes.add_evidence(conn, writer, row, given)
        [made] = [item for item in (await store.evidence(conn, [row.id]))[row.id] if item.id == evidence_id]
        return evidence_model(made)

    return await _answer(request, work)


async def _evidence_change(request: Request, project: str, slug: str, ref: str, evidence_id: int, user, sink, act):
    async def work(conn):
        access, canvas, writer = await _writing(conn, user, project, slug, sink, "changing evidence")
        row = await store.claim(conn, access, canvas, number_of(ref))
        await act(conn, writer, row)
        [changed] = [item for item in (await store.evidence(conn, [row.id]))[row.id] if item.id == evidence_id]
        return evidence_model(changed)

    return await _answer(request, work)


@router.post("/{project}/canvases/{slug}/claims/{ref}/evidence/{evidence_id}/withdraw", response_model=CanvasEvidence)
async def withdraw_evidence(
    request: Request,
    project: ProjectName,
    slug: Slug,
    ref: Ref,
    evidence_id: EvidenceId,
    body: CanvasWithdrawIn,
    user: CurrentUser,
    sink: Sink = None,
):
    """Withdraw a piece of evidence, with why: it stays on the claim, marked, and counts no more."""

    async def act(conn, writer, row):
        await writes.withdraw_evidence(conn, writer, row, evidence_id, body.reason)

    return await _evidence_change(request, project, slug, ref, evidence_id, user, sink, act)


@router.post("/{project}/canvases/{slug}/claims/{ref}/evidence/{evidence_id}/confirm", response_model=CanvasEvidence)
async def confirm_evidence(
    request: Request,
    project: ProjectName,
    slug: Slug,
    ref: Ref,
    evidence_id: EvidenceId,
    user: CurrentUser,
    sink: Sink = None,
):
    """Vouch for a piece of evidence an agent added, as a writer, so it may validate the claim."""

    async def act(conn, writer, row):
        await writes.confirm_evidence(conn, writer, row, evidence_id)

    return await _evidence_change(request, project, slug, ref, evidence_id, user, sink, act)


# Proposals and brainstorms


@router.get("/{project}/canvases/{slug}/proposals", response_model=list[CanvasProposal])
async def list_proposals(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    status: Annotated[
        list[Literal[PROPOSAL_STATUSES]] | None, Query(description="the statuses to list; open when left out")
    ] = None,
    sink: Sink = None,
):
    """The proposals of a canvas, newest first, each with what it changes from the rev it was made at."""

    async def work(conn):
        access = await _opened(conn, user, project)
        canvas = await store.canvas(conn, access, slug, sink)
        rows = await proposals.proposals(conn, canvas, tuple(status or ("open",)))
        return [proposal_model(row, await proposals.changes_of(conn, row)) for row in rows]

    return await _answer(request, work)


async def _decide(request: Request, project: str, slug: str, proposal_id: int, user, sink, act, *, writes_claim=False):
    async def work(conn):
        access, canvas, writer = await _writing(conn, user, project, slug, sink, "deciding a proposal")
        await act(conn, writer, canvas)
        if writes_claim:
            await _graph_changed(request, conn, access)
        row = await proposals.proposal(conn, canvas, proposal_id)
        return proposal_model(row, await proposals.changes_of(conn, row))

    return await _answer(request, work)


@router.post("/{project}/canvases/{slug}/proposals/{proposal_id}/accept", response_model=CanvasProposal)
async def accept_proposal(
    request: Request, project: ProjectName, slug: Slug, proposal_id: ProposalId, user: CurrentUser, sink: Sink = None
):
    """Accept an open proposal: its change is written as the caller's rev, under the rules of every edit (a claim
    goes to validated only with real evidence that supports it). A proposal whose claim changed since is stale."""

    async def act(conn, writer, canvas):
        await proposals.accept(conn, writer, canvas, proposal_id)

    return await _decide(request, project, slug, proposal_id, user, sink, act, writes_claim=True)


@router.post("/{project}/canvases/{slug}/proposals/{proposal_id}/reject", response_model=CanvasProposal)
async def reject_proposal(
    request: Request,
    project: ProjectName,
    slug: Slug,
    proposal_id: ProposalId,
    body: CanvasRejectIn,
    user: CurrentUser,
    sink: Sink = None,
):
    """Reject an open proposal, with why: nothing of it is written."""

    async def act(conn, writer, canvas):
        await proposals.reject(conn, writer, canvas, proposal_id, body.reason)

    return await _decide(request, project, slug, proposal_id, user, sink, act)


@router.post("/{project}/canvases/{slug}/proposals/reject", response_model=CanvasRejected)
async def reject_proposals(
    request: Request, project: ProjectName, slug: Slug, body: CanvasRejectManyIn, user: CurrentUser, sink: Sink = None
):
    """Reject open proposals of a canvas together, with one reason for all: nothing of them is written. One no longer
    open is left as it is and named; a 404 when the canvas has no proposal of an id, and nothing is rejected then."""

    async def work(conn):
        _, canvas, writer = await _writing(conn, user, project, slug, sink, "deciding a proposal")
        rejected, left = await proposals.reject_many(conn, writer, canvas, body.proposals, body.reason)
        return CanvasRejected(rejected=rejected, left=[CanvasProposalLeft(id=i, status=s) for i, s in left])

    return await _answer(request, work)


@router.post("/{project}/canvases/{slug}/brainstorm", response_model=CanvasBrainstorm, status_code=202)
async def brainstorm(
    request: Request, project: ProjectName, slug: Slug, body: CanvasBrainstormIn, user: CurrentUser, sink: Sink = None
):
    """Hand @strategist a brainstorm of the canvas as a task run, which proposes new claims, each an assumption
    with a test card, and changes none. 409, with why, when the project has not enabled the agent type strategist,
    turned it off, or its runs spent their cap of the day."""

    async def work(conn):
        access, canvas, _ = await _writing(conn, user, project, slug, sink, "asking for a brainstorm")
        run_id, why = await hand_brainstorm(conn, access, canvas, user.login, body.focus, tuple(body.cells))
        if run_id is None:
            raise CanvasError(409, f"no brainstorm of canvas {slug}: {why}")
        return CanvasBrainstorm(run_id=run_id, title=f"@strategist brainstorms {slug}")

    return await _answer(request, work)


@router.post("/{project}/canvases/import", response_model=CanvasImportResult)
async def import_canvas(
    request: Request, project: ProjectName, body: CanvasImportIn, user: CurrentUser, sink: Sink = None
):
    """Import evo-canvas/1: the canvas it names (made when the project has none of that slug) with its claims, each
    keeping its code; a claim the canvas holds changes as an edit at the rev the file gives, so one changed since is a
    409. ``dry_run`` reports what it would do and writes nothing."""

    async def work(conn):
        access = await _opened(conn, user, project)
        store.writer(access, "importing a canvas")
        writer = writes.person(access, user.user_id, sink)
        label = _label(access, body.label)
        report = await transfer.run_import(conn, writer, body.data, label=label, dry_run=body.dry_run)
        if not body.dry_run:
            await _graph_changed(request, conn, access)
        return import_model(report, body.dry_run)

    return await _answer(request, work)


# Baselines


def _no_run(user, what: str) -> None:
    if user.acts_for_run is not None:
        raise CanvasError(403, f"the agent of a run never {what}: a member of the project does")


def _may_decide(
    access: ProjectAccess, user_id: int, row: baselines.BaselineRow, item: baselines.ItemRow, barred: list[int]
) -> list[str]:
    """What ``user_id`` may decide of ``item`` now: nothing once it is decided, nor while the member is ``barred``."""
    if row.status != "proposed" or not item.waiting or user_id in barred:
        return []
    return list(baselines.DECIDES[item.kind]) if has_role(access.role, "writer") else []


async def _baseline_model(conn, access: ProjectAccess, user, canvas: CanvasRow, row, sink) -> CanvasBaseline:
    found = await baselines.items(conn, row.id)
    evidence = await store.evidence(conn, [item.claim_id for item in found])
    anyone = await baselines.self_approval(conn, access.project_id)
    barred = {item.id: item.barred(anyone) for item in found}
    names = await baselines.logins(conn, [user_id for item in found for user_id in barred[item.id]])
    models = []
    for item in found:
        wanted = set(item.evidence_ids) | {piece.id for piece in evidence[item.claim_id] if piece.withdrawn_at is None}
        pieces = [piece for piece in evidence[item.claim_id] if piece.id in wanted]
        shown = [piece for piece in pieces if store.visible(access, piece.label, sink)]
        decides = _may_decide(access, user.user_id, row, item, barred[item.id]) if user.acts_for_run is None else []
        models.append(item_model(item, shown, len(pieces) - len(shown), names, decides, barred[item.id]))
    snapshot = [CanvasSnapshotClaim(**entry) for entry in row.snapshot] if row.snapshot is not None else None
    approved_by = sorted({item.decided_by for item in found if item.decided_by}) if row.status == "approved" else []
    return CanvasBaseline(
        **baseline_values(row, found), canvas=canvas.slug, items=models, approved_by=approved_by, snapshot=snapshot
    )


@router.get("/{project}/canvases/{slug}/baselines", response_model=list[CanvasBaselineSummary])
async def list_baselines(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """The baselines of a canvas, newest first: weekly ones and those a writer opened at once, each proposed, approved
    or expired, with how many items wait for a member."""

    async def work(conn):
        access = await _opened(conn, user, project)
        canvas = await store.canvas(conn, access, slug, sink)
        rows = await baselines.baselines(conn, canvas)
        return [CanvasBaselineSummary(**baseline_values(row, await baselines.items(conn, row.id))) for row in rows]

    return await _answer(request, work)


@router.get("/{project}/canvases/{slug}/baselines/{number}", response_model=CanvasBaseline)
async def show_baseline(
    request: Request, project: ProjectName, slug: Slug, number: BaselineNumber, user: CurrentUser, sink: Sink = None
):
    """A baseline whole: what it asks of each claim with the claim's evidence and the decision on it, what the caller
    may decide, and once approved its snapshot (each claim's rev, the status in effect and the SHA-256 of its
    content), which never changes."""

    async def work(conn):
        access = await _opened(conn, user, project)
        canvas = await store.canvas(conn, access, slug, sink)
        row = await baselines.baseline(conn, canvas, number)
        return await _baseline_model(conn, access, user, canvas, row, sink)

    return await _answer(request, work)


@router.post("/{project}/canvases/{slug}/baselines", response_model=CanvasBaseline, status_code=201)
async def open_baseline(
    request: Request, project: ProjectName, slug: Slug, body: CanvasBaselineOpenIn, user: CurrentUser, sink: Sink = None
):
    """Open a baseline of one claim at once, without waiting for the week's: its change of status, the value check's
    proposal or its overdue test card. Another writer decides it (the caller too, with the config's self_approval on);
    the project's writers get the notice. 409 when the claim asks nothing of a baseline, or a baseline still proposed
    holds it."""

    async def work(conn):
        _no_run(user, "opens a baseline")
        access, canvas, _ = await _writing(conn, user, project, slug, sink, "opening a baseline")
        claim = await store.claim(conn, access, canvas, number_of(body.claim), lock=True)
        today = await baselines.today(conn)
        row = await baselines.open_now(conn, access.project_id, canvas, claim, user.user_id, today)
        if row.status == "proposed":
            await notices.tell_writers(conn, access.project_id, canvas, row, await baselines.items(conn, row.id))
        return await _baseline_model(conn, access, user, canvas, row, sink)

    return await _answer(request, work)


@router.post("/{project}/canvases/{slug}/baselines/{number}/items/{ref}/decide", response_model=CanvasBaseline)
async def decide_item(
    request: Request,
    project: ProjectName,
    slug: Slug,
    number: BaselineNumber,
    ref: Ref,
    body: CanvasBaselineDecisionIn,
    user: CurrentUser,
    sink: Sink = None,
):
    """Decide the item of one claim of a baseline still proposed: accept, reject or defer a change of status; extend
    (with a new deadline) or retire a claim whose test card is overdue, or defer it. Never one whose status the caller
    changed or whose proposal they accepted, nor in a baseline they opened, unless the config's self_approval is on;
    a rev it writes is the caller's, of kind baseline. Once no item waits, the baseline is approved and its snapshot
    taken."""

    async def work(conn):
        _no_run(user, "decides an item of a baseline")
        access, canvas, writer = await _writing(conn, user, project, slug, sink, "deciding an item of a baseline")
        decision = decisions.Decision(ref, body.decision, body.deadline, body.reason)
        row = await decisions.decide(conn, writer, canvas, number, decision, await baselines.today(conn))
        await _graph_changed(request, conn, access)
        return await _baseline_model(conn, access, user, canvas, row, sink)

    return await _answer(request, work)


__all__ = ["router"]
