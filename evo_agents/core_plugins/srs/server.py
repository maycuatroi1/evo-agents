"""The routes of the plugin srs, under /v1/x/srs/<project>/ for a member with a grant on a project that enabled it
(the hub checks both first, ``evo_agents.hub.server.plugin_routes``; a project that did not enable it answers 404).

Readers read: the documents, a document with its versions, a version whole with its problems, an item, the difference
between two versions, a version as Markdown, the comments. Writers import Markdown, save versions, submit them (a
version's first submit hands @ba its review, ``auto_review``), review them (never one they wrote a version of since
the one approved last) and comment; dropping a coded item takes an admin. The author of a version in review, or an
admin, withdraws it (POST .../versions/{n}/withdraw), for good. Approving a version queues a build of the project's
knowledge graph, which reads the approved versions. Reads go through the sink X-Evo-Sink names, else the project's hub
sink. A refusal is the hub's error body, with the problems that refused it (or a conflict's two texts) in ``detail``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import checks, edits, imports, markdown, reviews, store
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
    SrsImport,
    SrsImportResult,
    SrsItemDetail,
    SrsReviewIn,
    SrsSave,
    SrsVersion,
    SrsVersionDetail,
    SrsWithdrawIn,
    comment_model,
    detail_model,
    diff_model,
    edit_changes,
    item_detail_model,
    preview_model,
    summary_model,
    version_model,
)
from evo_agents.core_plugins.srs.template import code_key
from evo_agents.core_plugins.srs.writes import Writer, give_screen_codes, write_version
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.tables_srs import SLUG

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
async def list_documents(request: Request, project: ProjectName, user: CurrentUser, sink: Sink = None):
    """The BA documents of the project the caller sees, by slug, each with its latest version."""

    async def work(conn):
        access = await project_access(conn, user, project)
        return [summary_model(row) for row in await store.documents(conn, access, sink)]

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


@router.post("/{project}/documents/{slug}/versions/{number}/submit", response_model=SrsVersion)
async def submit_version(
    request: Request, project: ProjectName, slug: Slug, number: Number, user: CurrentUser, sink: Sink = None
):
    """Send a draft to review, once it has no error. The first time a version goes to review, @ba reviews it when the
    project enabled the agent type ba; its comments block nothing."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        sent = await reviews.submit(conn, access, doc, number, await store.settings(conn, access))
        await review_entered(conn, access, doc, sent)
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
    it, and the agent of a run never does."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        settings = await store.settings(conn, access)
        asked = reviews.ReviewRequest(body.decision, body.role, body.body)
        decided = await reviews.review(conn, access, user, doc, number, asked, settings)
        if decided.state == "approved":  # the knowledge graph reads the version approved last (``kg``)
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
        doc = await store.document(conn, access, slug, sink)
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
        doc = await store.document(conn, access, slug, sink)
        return comment_model(await reviews.resolve(conn, access, user, doc, comment_id))

    return await _answer(request, work)


__all__ = ["router"]
