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
decide, or @ba's review of the version's round is queued or running (``findings.approval_hold``); a request for changes
is never held. A version approved in a project that enables the plugin tasks keeps its tasks in step, in the same
transaction (``auto_tasks.approved``, with the version approved before it and the approver): frame tasks of its new use
cases, comments and cancellations, and @ba's split run. Reads go through the sink X-Evo-Sink names, else the project's
hub sink. A refusal is the hub's error body, with the problems that refused it (or a conflict's two texts) in
``detail``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import auto_tasks, checks, edits, findings, imports, markdown, reviews, store
from evo_agents.core_plugins.srs.auto_review import review_entered
from evo_agents.core_plugins.srs.diff import diff, lines_between
from evo_agents.core_plugins.srs.doc import SrsError
from evo_agents.core_plugins.srs.models import (
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
    SrsRetireIn,
    SrsRetireResult,
    SrsReviewIn,
    SrsSave,
    SrsVersion,
    SrsVersionDetail,
    SrsWaiveIn,
    SrsWithdrawIn,
    comment_model,
    detail_model,
    diff_model,
    edit_changes,
    finding_model,
    item_detail_model,
    preview_model,
    retire_model,
    summary_model,
    version_model,
)
from evo_agents.core_plugins.srs.template import code_key
from evo_agents.core_plugins.srs.writes import Writer, dropped, give_screen_codes, unheld_problems, write_version
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.tables_srs import FINDING_STATUSES, MAX_SUMMARY_CHARS, SLUG

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
        async with request.app.state.engine.begin() as conn:
            return await work(conn)
    except SrsError as exc:
        detail = [problem.as_json() for problem in exc.problems] or exc.detail
        return error_response(request, exc.status, exc.message, detail=detail)
    except IntegrityError:
        return error_response(request, 409, RACE)


async def _detail(conn: AsyncConnection, access: ProjectAccess, doc: store.DocumentRow, number: int | None):
    row = await store.version(conn, doc, number)
    snap = await store.snapshot(conn, row)
    settings = await store.settings(conn, access)
    return detail_model(doc.slug, row, snap.doc, checks.problems(snap.doc, settings.template))


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
        access = await project_access(conn, user, project)
        found = await store.documents(conn, access, sink, with_archived=with_archived)
        return [summary_model(row) for row in found]

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}", response_model=SrsDocument)
async def show_document(
    request: Request, project: ProjectName, slug: Slug, user: CurrentUser, version: Version = None, sink: Sink = None
):
    """A document with every version, the newest first, and one version whole: the latest, or ``version``."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        shown = await _detail(conn, access, doc, version) if doc.latest_version else None
        versions = [version_model(row) for row in await store.versions(conn, doc.id)]
        return SrsDocument(**summary_model(doc).model_dump(), versions=versions, shown=shown)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/versions", response_model=list[SrsVersion])
async def list_versions(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """Every version of a document, the newest first, with its reviews."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        return [version_model(row) for row in await store.versions(conn, doc.id)]

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/versions/{number}", response_model=SrsVersionDetail)
async def show_version(
    request: Request, project: ProjectName, slug: Slug, number: Number, user: CurrentUser, sink: Sink = None
):
    """A version whole: its items and what is wrong with it."""

    async def work(conn):
        access = await project_access(conn, user, project)
        return await _detail(conn, access, await store.document(conn, access, slug, sink), number)

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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        row = await store.version(conn, doc, version)
        snap = await store.snapshot(conn, row)
        wanted = code_key(ref)
        found = next(
            (
                at
                for at, item in enumerate(snap.doc.items)
                if item.key == ref or wanted in {code_key(code) for code in item.codes()}
            ),
            None,
        )
        if found is None:
            raise SrsError(404, f"version {row.number} of {slug} has no item {ref}")
        return item_detail_model(slug, row.number, snap.doc, found)

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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        old = await store.snapshot(conn, await store.version(conn, doc, low))
        new = await store.snapshot(conn, await store.version(conn, doc, high))
        preamble = lines_between(old.doc.preamble, new.doc.preamble) if old.doc.preamble != new.doc.preamble else []
        return diff_model(slug, low, high, preamble, diff(old.doc, new.doc))

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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        row = await store.version(conn, doc, version)
        snap = await store.snapshot(conn, row)
        return SrsExport(document=slug, version=row.number, markdown=markdown.render(snap.doc, anchors=anchors))

    return await _answer(request, work)


# Writing


@router.post("/{project}/import", response_model=SrsImportResult)
async def import_document(
    request: Request, project: ProjectName, body: SrsImport, user: CurrentUser, sink: Sink = None
):
    """Import Markdown as a new document, or as the next version of ``document``, after a preview; ``dry_run`` stops
    at the preview."""

    async def work(conn):
        access = await project_access(conn, user, project)
        given = body.model_dump(exclude={"label"})
        label = body.label.model_dump(exclude_none=True) if body.label else None
        done = await imports.import_markdown(
            conn, access, user.user_id, imports.ImportRequest(**given, label=label), sink
        )
        return SrsImportResult(
            document=done.document,
            created=done.created,
            version=done.version,
            dry_run=body.dry_run,
            preview=preview_model(done.preview),
        )

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/versions", response_model=SrsVersionDetail, status_code=201)
async def save_version(
    request: Request, project: ProjectName, slug: Slug, body: SrsSave, user: CurrentUser, sink: Sink = None
):
    """Save changes made on version ``base_version`` as the document's next version, built on its latest; 409 with
    both texts of an item that changed since ``base_version``."""

    async def work(conn):
        access = await project_access(conn, user, project)
        reviews.writer(access, "saving a version")
        doc = await store.document(conn, access, slug, sink, lock=True)
        latest = await store.snapshot(conn, await store.version(conn, doc))
        base = latest if body.base_version == latest.version.number else None
        base = base or await store.snapshot(conn, await store.version(conn, doc, body.base_version))
        content = edits.edited(latest, base, edit_changes(body.changes), body.preamble)
        await give_screen_codes(conn, access.project_id, content)
        writer = Writer(access.project_id, doc.id, user.user_id, has_role(access.role, "admin"))
        template = (await store.settings(conn, access)).template
        number = await write_version(conn, writer, content, latest, template, body.summary)
        return await _detail(conn, access, await store.document(conn, access, slug, sink), number)

    return await _answer(request, work)


def _retired_summary(changes: list[edits.Change], latest: store.Snapshot) -> str:
    keys = {change.key for change in changes}
    named = [item.code or item.title for item in latest.doc.items if item.key in keys]
    return f"Retired {', '.join(named)}"[:MAX_SUMMARY_CHARS]


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
        access = await project_access(conn, user, project)
        reviews.admin(access, user, "retires items by their codes")
        doc = store.writable(access, await store.document(conn, access, slug, sink, lock=not body.dry_run))
        latest = await store.snapshot(conn, await store.version(conn, doc))
        writer = Writer(access.project_id, doc.id, user.user_id, admin=True)
        changes, unknown = edits.retiring(latest.doc, body.codes)
        if unknown:
            where = f"version {latest.version.number} of {slug}"
            found = await unheld_problems(conn, writer, where, unknown)
            raise SrsError(422, f"nothing is retired: {found[0].message}", found)
        content = edits.edited(latest, latest, changes)
        gone = dropped(content, latest)
        if body.dry_run:
            return retire_model(slug, None, True, gone)
        await give_screen_codes(conn, access.project_id, content)
        template = (await store.settings(conn, access)).template
        summary = body.summary or _retired_summary(changes, latest)
        return retire_model(slug, await write_version(conn, writer, content, latest, template, summary), False, gone)

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/versions/{number}/submit", response_model=SrsVersion)
async def submit_version(
    request: Request, project: ProjectName, slug: Slug, number: Number, user: CurrentUser, sink: Sink = None
):
    """Send a draft to review, once it has no error. Each time a version goes to review, in every round, @ba reviews it
    when the project enabled the agent type ba: its comments block nothing, and an open finding of level decide it
    records holds approval."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        sent = await reviews.submit(conn, access, doc, number, await store.settings(conn, access))
        if await review_entered(conn, access, doc, sent) is not None:
            sent = await store.version(conn, doc, number)  # with @ba's review run of the round
        return version_model(sent)

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
    decide, naming each, or while @ba's review of the version's round is queued or running. Once a version is
    approved in a project that enables the plugin tasks, each use case new in it gets a frame task, the open tasks of a
    use case that changed a comment and those of one it dropped are cancelled, and @ba splits the frame tasks into
    child tasks, in the same transaction."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        settings = await store.settings(conn, access)  # with self_approval on, the author reviews it too
        asked = reviews.ReviewRequest(body.decision, body.role, body.body)
        decided = await reviews.review(conn, access, user, doc, number, asked, settings)
        if decided.state == "approved":  # the knowledge graph reads the version approved last (``kg``)
            await auto_tasks.approved(conn, doc.id, number, doc.approved_version, user.user_id)
            await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)
        return version_model(decided)

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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        reason = body.comment if body is not None else None
        return version_model(await reviews.withdraw(conn, access, user, doc, number, reason))

    return await _answer(request, work)


async def _archived(request: Request, project: str, slug: str, user: Principal, sink: str | None, archived: bool):
    """Archive document ``slug`` of ``project`` or unarchive it, queue a build of the knowledge graph, which leaves
    archived documents out, and answer the document."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True, even_archived=True)
        await reviews.archive(conn, access, user, doc, archived)
        await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)
        return summary_model(await store.document(conn, access, slug, sink))

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
async def list_comments(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """The comments of a document, oldest first."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        return [comment_model(row) for row in await reviews.comments(conn, doc)]

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/comments", response_model=SrsComment, status_code=201)
async def add_comment(
    request: Request, project: ProjectName, slug: Slug, body: SrsCommentIn, user: CurrentUser, sink: Sink = None
):
    """Comment on a document, a version of it or an item, or answer a comment."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        asked = reviews.CommentRequest(body.body, body.version, body.item, body.parent_id)
        return comment_model(await reviews.comment(conn, access, user, doc, asked))

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
    """Close the thread of a comment."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        return comment_model(await reviews.resolve(conn, access, user, doc, comment_id))

    return await _answer(request, work)


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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        rows = await findings.findings(conn, doc, status)
        held = await findings.code_documents(conn, access, sink, rows)
        return [finding_model(row, held) for row in rows]

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
        access = await project_access(conn, user, project)
        reviews.admin(access, user, "waives a finding")
        doc = await store.document(conn, access, slug, sink, lock=True)
        waived = findings.Settling(findings.WAIVED, body.reason, user.user_id)
        row = await findings.settle(conn, access, doc, finding_id, waived)
        return finding_model(row, await findings.code_documents(conn, access, sink, [row]))

    return await _answer(request, work)


__all__ = ["router"]
