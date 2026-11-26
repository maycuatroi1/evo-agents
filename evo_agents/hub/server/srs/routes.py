"""The routes of the plugin srs, under /v1/x/srs/<project>/ for a member with a grant on a project that enabled it
(the hub checks both first, ``evo_agents.hub.server.plugin_routes``; a project that did not enable it answers 404).

Readers read: the documents, a document with its versions, a version whole with its problems, an item, the difference
between two versions, a version as Markdown, the comments. Writers import Markdown, save versions, submit them (each
submit, in every round, hands @ba its review, ``auto_review``), review them (never one they wrote a version of since
the one approved last, unless the config's ``self_approval`` is on) and comment; dropping a coded item takes an admin.
An admin also retires items by their codes (POST .../documents/{slug}/retire, ``dry_run`` to see what would go): the
next version, a draft saved as any other, without those items and their subtrees. The author of a version in review,
or an admin, withdraws it (POST .../versions/{n}/withdraw), for good. An admin archives a document (POST
.../documents/{slug}/archive) and unarchives it (POST .../unarchive): an archived one is out of the list of documents
unless ``all`` asks for it, still read by its slug, and refuses every write with 409 (``store.writable``). Approving a
version, archiving a document and unarchiving it queue a build of the project's knowledge graph, which reads the
approved versions of the documents not archived. Readers read the conflicts @ba found in a document (GET
.../documents/{slug}/findings, ``status`` to keep one status), and an admin waives an open one (POST
.../findings/{id}/waive) with a reason. Approving a version answers 409 while the document has an open finding of level
decide, or @ba's review of the version's round is queued or running (``findings.approval_hold``), unless the config's
``wait_for_ba`` is off; a request for changes is never held. With the config's ``owners``, a use case of a group named
there is reviewed by its owners and the project's admins alone. Approving a version emits ``srs.version.approved`` in the same transaction (``events``); in a project
that enables the plugin tasks, the plugin's handler of that event keeps its tasks in step when the hub delivers it
(``handlers.on_version_approved``, then ``auto_tasks.approved``, with the version approved before it and the
approver): frame tasks of its new use cases, comments and cancellations, and @ba's split run. A writer sends one
use case to review on its own (POST
.../documents/{slug}/use-cases/{ref}/submit): the revision it has in the latest version, which @ba reviews alone and
reviewers approve into the approved baseline without waiting for the rest of the version. Every use case answered
carries its revision (``rev``), the revision the baseline holds (``released``) and the link of its review. Reads go
through the sink X-Evo-Sink names, else the project's hub sink. A refusal is the hub's error body, with the problems
that refused it (or a conflict's two texts) in ``detail``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy.exc import IntegrityError

from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.srs.models.api import (
    SrsComment,
    SrsCommentIn,
    SrsDiff,
    SrsDocument,
    SrsDocumentSummary,
    SrsExport,
    SrsFinding,
    SrsImport,
    SrsImportResult,
    SrsItemDetail,
    SrsQueuedVersion,
    SrsRetireIn,
    SrsRetireResult,
    SrsReviewIn,
    SrsSave,
    SrsSettleIn,
    SrsUseCaseSubmitted,
    SrsVersion,
    SrsVersionCases,
    SrsVersionDetail,
    SrsWaiveIn,
    SrsWithdrawIn,
)
from evo_agents.hub.server.srs.models.doc import SrsError
from evo_agents.hub.server.srs.repository import store
from evo_agents.hub.server.srs.service import reading, writing
from evo_agents.hub.tables_srs import FINDING_STATUSES, SLUG

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422)}
RACE = "another save in the project took a code or a version number at the same moment: nothing was written; try again"
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
Slug = Annotated[str, Path(pattern=SLUG)]
Number = Annotated[int, Path(ge=1)]
Sink = Annotated[str | None, Header(alias=store.SINK_HEADER)]
Version = Annotated[int | None, Query(ge=1, description="a version's number; the latest when left out")]

router = APIRouter(tags=["plugins"], responses={401: {"model": ErrorBody}, **REFUSALS})


async def _answer(request: Request, work):
    """``work(conn)`` in one transaction; a refusal as the hub's error body, nothing written."""
    try:
        async with UnitOfWork(request.app.state.engine) as conn:
            return await work(conn)
    except SrsError as exc:
        detail = [problem.as_json() for problem in exc.problems] or exc.detail
        return error_response(request, exc.status, exc.message, detail=detail)
    except IntegrityError:
        return error_response(request, 409, RACE)


# Reading


@router.get("/{project}/documents", response_model=list[SrsDocumentSummary])
async def list_documents(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    with_archived: Annotated[bool, Query(alias="all", description="the archived documents too")] = False,
    sink: Sink = None,
):
    """The BA documents of the project the caller sees, by slug, each with its latest version; the archived ones only
    with ``all``."""

    async def work(conn):
        return await reading.list_documents(conn, user, project, sink, with_archived)

    return await _answer(request, work)


@router.get("/{project}/reviews", response_model=list[SrsQueuedVersion])
async def review_queue(request: Request, project: ProjectName, user: CurrentUser, sink: Sink = None):
    """The versions in review of the documents the caller sees, the one submitted first first: each with the use cases
    it needs reviewed, their state and the reviews that count, what holds its approval now (@ba's review of the round,
    then open findings of level decide) and whether the caller may review it."""

    async def work(conn):
        return await reading.review_queue(conn, user, project, sink)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}", response_model=SrsDocument)
async def show_document(
    request: Request, project: ProjectName, slug: Slug, user: CurrentUser, version: Version = None, sink: Sink = None
):
    """A document with every version, the newest first, and one version whole: the latest, or ``version``."""

    async def work(conn):
        return await reading.show_document(conn, user, project, slug, version, sink)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/versions", response_model=list[SrsVersion])
async def list_versions(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """Every version of a document, the newest first, with its reviews."""

    async def work(conn):
        return await reading.list_versions(conn, user, project, slug, sink)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/versions/{number}", response_model=SrsVersionDetail)
async def show_version(
    request: Request, project: ProjectName, slug: Slug, number: Number, user: CurrentUser, sink: Sink = None
):
    """A version whole: its items and what is wrong with it."""

    async def work(conn):
        return await reading.show_version(conn, user, project, slug, number, sink)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/items/{ref}", response_model=SrsItemDetail)
async def show_item(
    request: Request,
    project: ProjectName,
    slug: Slug,
    ref: Annotated[str, Path(min_length=1, max_length=100, description="the item's code, an alias or its key")],
    user: CurrentUser,
    version: Version = None,
    sink: Sink = None,
):
    """An item of a version (the latest unless asked), by code, alias or key, with its children."""

    async def work(conn):
        return await reading.show_item(conn, user, project, slug, ref, version, sink)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/diff", response_model=SrsDiff)
async def compare_versions(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    low: Annotated[int, Query(alias="from", ge=1)],
    high: Annotated[int, Query(alias="to", ge=1)],
    sink: Sink = None,
):
    """What changed from version ``from`` to version ``to``, item by item."""

    async def work(conn):
        return await reading.compare_versions(conn, user, project, slug, low, high, sink)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/export", response_model=SrsExport)
async def export_version(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    version: Version = None,
    anchors: Annotated[bool, Query(description="write each screen's code as a comment after its heading")] = True,
    sink: Sink = None,
):
    """A version (the latest unless asked) as Markdown, in the template's structure, with the same codes."""

    async def work(conn):
        return await reading.export_version(conn, user, project, slug, version, anchors, sink)

    return await _answer(request, work)


# Writing


@router.post("/{project}/import", response_model=SrsImportResult)
async def import_document(
    request: Request, project: ProjectName, body: SrsImport, user: CurrentUser, sink: Sink = None
):
    """Import Markdown as a new document, or as the next version of ``document``, after a preview; ``dry_run`` stops
    at the preview."""

    async def work(conn):
        return await writing.import_document(conn, user, project, body, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/versions", response_model=SrsVersionDetail, status_code=201)
async def save_version(
    request: Request, project: ProjectName, slug: Slug, body: SrsSave, user: CurrentUser, sink: Sink = None
):
    """Save changes made on version ``base_version`` as the document's next version, built on its latest; 409 with
    both texts of an item that changed since ``base_version``."""

    async def work(conn):
        return await writing.save_version(conn, user, project, slug, body, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/retire", response_model=SrsRetireResult)
async def retire_items(
    request: Request, project: ProjectName, slug: Slug, body: SrsRetireIn, user: CurrentUser, sink: Sink = None
):
    """Drop the items of the latest version that hold ``codes`` (codes or aliases), each with its subtree, as the
    document's next version, a draft built on the latest through the same save as any other, so its checks hold and a
    code dropped is never given again; as an admin of the project, never the agent of a run. 422 listing each code no
    item of the latest version holds, one dropped before included; ``dry_run`` answers what would be dropped and writes
    nothing."""

    async def work(conn):
        return await writing.retire_items(conn, user, project, slug, body, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/versions/{number}/submit", response_model=SrsVersionCases)
async def submit_version(
    request: Request, project: ProjectName, slug: Slug, number: Number, user: CurrentUser, sink: Sink = None
):
    """Send a draft to review, once it has no error. Each time a version goes to review, in every round, @ba reviews it
    when the project enabled the agent type ba: its comments block nothing, and an open finding of level decide it
    records holds approval. The reviewers get the notice srs_review once nothing holds approval: at once when @ba
    reviews nothing or the config's wait_for_ba is off, else when its review ends (``notices``)."""

    async def work(conn):
        return await writing.submit_version(conn, user, project, slug, number, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/use-cases/{ref}/submit", response_model=SrsUseCaseSubmitted)
async def submit_use_case(
    request: Request,
    project: ProjectName,
    slug: Slug,
    ref: Annotated[str, Path(min_length=1, max_length=100, description="the use case's code, an alias or its key")],
    user: CurrentUser,
    sink: Sink = None,
):
    """Send one use case to review on its own: the revision it has in the latest version, while that version is not in
    review. 409 when the revision is approved already or in review, 422 when the use case or a business rule under it
    has an error that blocks submitting. @ba reviews that use case alone when the project enabled the agent type ba;
    its reviewers (not whoever wrote a revision of it since its newest release) get the notice srs_review once nothing
    holds its approval, with the link of its review. Answers the latest version with its use cases and this one."""

    async def work(conn):
        return await writing.submit_use_case(conn, user, project, slug, ref, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/versions/{number}/reviews", response_model=SrsVersion)
async def review_version(
    request: Request,
    project: ProjectName,
    slug: Slug,
    number: Number,
    body: SrsReviewIn,
    user: CurrentUser,
    sink: Sink = None,
):
    """Approve a version in review or ask for changes. Nobody who wrote a version since the one approved last reviews
    it, and the agent of a run never does. An approval answers 409 while the document has an open finding of level
    decide, naming each, or while @ba's review of the version's round is queued or running. An approval emits the
    event srs.version.approved; once the hub delivers it, in a project that enables the plugin tasks, each use case new
    in the version gets a frame task, the open tasks of a use case that changed a comment and those of one it dropped
    are cancelled, and @ba splits the frame tasks into child tasks."""

    async def work(conn):
        return await writing.review_version(conn, request.app.state.jobs, user, project, slug, number, body, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/versions/{number}/items/{ref}/reviews", response_model=SrsVersionCases)
async def review_use_case(
    request: Request,
    project: ProjectName,
    slug: Slug,
    number: Number,
    ref: Annotated[str, Path(min_length=1, max_length=100, description="the use case's code, an alias or its key")],
    body: SrsReviewIn,
    user: CurrentUser,
    sink: Sink = None,
):
    """Approve one use case of a version in review or ask for changes on it, under the conditions of a review of the
    version; a later review of the same member on the use case replaces the earlier one when they are counted. 422 for
    an item that is no use case the version needs reviewed (one it adds, changes or drops since the version approved
    last) and for a request for changes with no note. An approval goes through the gate of a version's (409 while a
    finding of level decide is open or @ba's review runs); a request for changes leaves the version in review and
    blocks every approval of it. The version is approved by the review that makes every use case to review approved,
    as its version-wide reviews do: it emits srs.version.approved as any approval. Answers the version with its use
    cases."""

    async def work(conn):
        return await writing.review_use_case(conn, request.app.state.jobs, user, project, slug, number, ref, body, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/versions/{number}/withdraw", response_model=SrsVersion)
async def withdraw_version(
    request: Request,
    project: ProjectName,
    slug: Slug,
    number: Number,
    user: CurrentUser,
    body: SrsWithdrawIn | None = None,
    sink: Sink = None,
):
    """Take a version out of review for good, as its author or an admin of the project; ``comment`` is kept as a
    comment on it. A withdrawn version is never submitted, reviewed or approved again, and approving a later one leaves
    it withdrawn. The agent of a run never withdraws one."""

    async def work(conn):
        return await writing.withdraw_version(
            conn, user, project, slug, number, body.comment if body is not None else None, sink
        )

    return await _answer(request, work)


async def _archived(request: Request, project: str, slug: str, user: Principal, sink: str | None, archived: bool):
    """Archive document ``slug`` of ``project`` or unarchive it, queue a build of the knowledge graph, which leaves
    archived documents out, and answer the document."""

    async def work(conn):
        return await writing.archive(conn, request.app.state.jobs, user, project, slug, sink, archived)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/archive", response_model=SrsDocumentSummary)
async def archive_document(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """Archive a document, as an admin of the project: it leaves the list of documents (``all`` lists it), @ba's
    search and the knowledge graph, is still read by its slug, and takes no write until it is unarchived; its codes
    stay held and its slug taken. 409 when it is archived already."""
    return await _archived(request, project, slug, user, sink, True)


@router.post("/{project}/documents/{slug}/unarchive", response_model=SrsDocumentSummary)
async def unarchive_document(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """Open an archived document again, as an admin of the project. 409 when it is not archived."""
    return await _archived(request, project, slug, user, sink, False)


# Comments


@router.get("/{project}/documents/{slug}/comments", response_model=list[SrsComment])
async def list_comments(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    version: Version = None,
    item: Annotated[
        str | None, Query(min_length=1, max_length=100, description="the threads of this item and the items under it")
    ] = None,
    status: Annotated[
        Literal["open", "resolved"] | None, Query(description="only the threads open or resolved")
    ] = None,
    sink: Sink = None,
):
    """The comments of a document, oldest first: every thread, or the threads of ``item`` (and the items under it), and
    only those open or resolved with ``status``. A passage's ``anchor`` is read in ``version``, the latest unless
    asked."""

    async def work(conn):
        return await reading.list_comments(conn, user, project, slug, version, item, status, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/comments", response_model=SrsComment, status_code=201)
async def add_comment(
    request: Request, project: ProjectName, slug: Slug, body: SrsCommentIn, user: CurrentUser, sink: Sink = None
):
    """Comment on a document, a version of it or an item, or answer a comment."""

    async def work(conn):
        return await writing.add_comment(conn, user, project, slug, body, sink)

    return await _answer(request, work)


async def _resolved(request: Request, project: str, slug: str, comment_id: int, user, sink, *, reopen: bool):
    async def work(conn):
        return await writing.resolve_comment(conn, user, project, slug, comment_id, sink, reopen=reopen)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/comments/{comment_id}/resolve", response_model=SrsComment)
async def resolve_comment(
    request: Request,
    project: ProjectName,
    slug: Slug,
    comment_id: Annotated[int, Path(ge=1)],
    user: CurrentUser,
    sink: Sink = None,
):
    """Close the thread of a comment (its root, for an answer), as a member; never the agent of a run."""
    return await _resolved(request, project, slug, comment_id, user, sink, reopen=False)


@router.post("/{project}/documents/{slug}/comments/{comment_id}/reopen", response_model=SrsComment)
async def reopen_comment(
    request: Request,
    project: ProjectName,
    slug: Slug,
    comment_id: Annotated[int, Path(ge=1)],
    user: CurrentUser,
    sink: Sink = None,
):
    """Open a resolved thread again (its root, for an answer), as a member; never the agent of a run."""
    return await _resolved(request, project, slug, comment_id, user, sink, reopen=True)


# Findings


@router.get("/{project}/documents/{slug}/findings", response_model=list[SrsFinding])
async def list_findings(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    status: Annotated[Literal[FINDING_STATUSES] | None, Query(description="only the findings in this status")] = None,
    sink: Sink = None,
):
    """The conflicts @ba found in a document, oldest first, each with its sources and how it was settled; a code among
    them names the document whose item holds it, when the member sees that document."""

    async def work(conn):
        return await reading.list_findings(conn, user, project, slug, status, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/findings/{finding_id}/settle", response_model=SrsFinding)
async def settle_finding(
    request: Request,
    project: ProjectName,
    slug: Slug,
    finding_id: Annotated[int, Path(ge=1)],
    body: SrsSettleIn,
    user: CurrentUser,
    sink: Sink = None,
):
    """Settle an open finding as a writer of the project, never the agent of a run, from the review of a use case: by
    picking one of its choices (``option``, 0 for A), which becomes its resolution with ``note``, or, for a finding with
    no choice, by ``note`` alone. The last open finding of level decide settled tells the reviewers of each version in
    review that nothing holds it any more (``notices``). 409 when it is not open, or the document is archived."""

    async def work(conn):
        return await writing.settle_finding(conn, user, project, slug, finding_id, body, sink)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/findings/{finding_id}/waive", response_model=SrsFinding)
async def waive_finding(
    request: Request,
    project: ProjectName,
    slug: Slug,
    finding_id: Annotated[int, Path(ge=1)],
    body: SrsWaiveIn,
    user: CurrentUser,
    sink: Sink = None,
):
    """Waive an open finding as an admin of the project, never the agent of a run: it holds no approval after, and
    ``reason`` is kept as its resolution. 409 when it is not open, or the document is archived."""

    async def work(conn):
        return await writing.waive_finding(conn, user, project, slug, finding_id, body, sink)

    return await _answer(request, work)


__all__ = ["router"]
