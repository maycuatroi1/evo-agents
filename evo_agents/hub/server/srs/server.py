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
is never held. Approving a version emits ``srs.version.approved`` in the same transaction (``events``); in a project
that enables the plugin tasks, the plugin's handler of that event keeps its tasks in step when the hub delivers it
(``auto_tasks.approved``, with the version approved before it and the approver): frame tasks of its new use cases,
comments and cancellations, and @ba's split run. A writer sends one use case to review on its own (POST
.../documents/{slug}/use-cases/{ref}/submit): the revision it has in the latest version, which @ba reviews alone and
reviewers approve into the approved baseline without waiting for the rest of the version. Every use case answered
carries its revision (``rev``), the revision the baseline holds (``released``) and the link of its review. Reads go
through the sink X-Evo-Sink names, else the project's hub sink. A refusal is the hub's error body, with the problems
that refused it (or a conflict's two texts) in ``detail``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.srs import (
    checks,
    edits,
    findings,
    imports,
    markdown,
    notices,
    reviews,
    revisions,
    store,
    threads,
    use_case_reviews,
)
from evo_agents.hub.server.srs.auto_review import case_entered, review_entered
from evo_agents.hub.server.srs.diff import diff, lines_between
from evo_agents.hub.server.srs.doc import SrsError
from evo_agents.hub.server.srs.models import (
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
    SrsQueuedUseCase,
    SrsQueuedVersion,
    SrsRetireIn,
    SrsRetireResult,
    SrsReviewIn,
    SrsReviewRun,
    SrsRevision,
    SrsSave,
    SrsSettleIn,
    SrsUseCaseSubmitted,
    SrsVersion,
    SrsVersionCases,
    SrsVersionDetail,
    SrsWaiveIn,
    SrsWithdrawIn,
    cases_model,
    comment_model,
    detail_model,
    diff_model,
    edit_changes,
    finding_model,
    item_detail_model,
    preview_model,
    retire_model,
    summary_model,
    use_case_model,
    version_model,
)
from evo_agents.hub.server.srs.writes import Writer, dropped, give_screen_codes, unheld_problems, write_version
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


def _revs(known: revisions.History, number: int) -> dict[str, int]:
    """The revision each use case has in version ``number``, by key."""
    found = {}
    for key in known.revisions:
        revision = known.at(key, number)
        if revision is not None and not revision.dropped:
            found[key] = revision.rev
    return found


async def _detail(conn: AsyncConnection, access: ProjectAccess, doc: store.DocumentRow, number: int | None):
    row = await store.version(conn, doc, number)
    snap = await store.snapshot(conn, row)
    settings = await store.settings(conn, access)
    wide = use_case_reviews.round_approvals(row)
    known = await revisions.history(conn, doc.id)
    cases = await use_case_reviews.states(conn, access.project_id, doc, row, snap.doc, settings, wide, known)
    problems = checks.problems(snap.doc, settings.template)
    released = {key: rev for key in known.releases if (rev := known.baseline(key)) is not None}
    return detail_model(doc.slug, row, snap.doc, problems, cases, access.name, _revs(known, row.number), released)


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


async def _queued_cases(
    conn: AsyncConnection,
    access: ProjectAccess,
    doc: store.DocumentRow,
    row: store.VersionRow,
    content,
    cases,
    reviewer: int | None,
) -> list[SrsQueuedUseCase]:
    """The use cases to review of version ``row``, whose content is ``content``, each with its intent as the version
    reads, the open findings of the document about it or a business rule under it, by level, what holds its approval
    and whether ``reviewer`` (None: nobody, the caller is no writer) may review it."""
    under = findings.use_case_of(content)
    intents = {  # Intent, or the Objective it replaced in older use cases
        item.key: item.fields.get("intent") or item.fields.get("objective")
        for item in content.items
        if item.kind == "use_case"
    }
    counts: dict[str, list[int]] = {}
    for found in await findings.findings(conn, doc, findings.OPEN):
        case = under.get(found.item or "")
        if case is not None:
            counts.setdefault(case, [0, 0])[0 if found.level == findings.DECIDE else 1] += 1
    queued = []
    for case in cases:
        hold = findings.Case(case.key, use_case_reviews.case_name(case), content, case.review_run_id)
        deciding, running = await findings.holds(conn, doc, row, hold)
        link = store.case_path(access.name, doc.slug, row.number, case.key)
        queued.append(
            SrsQueuedUseCase(
                **use_case_model(case, link).model_dump(),
                intent=intents.get(case.key) or None,
                deciding=counts.get(case.key, [0, 0])[0],
                noted=counts.get(case.key, [0, 0])[1],
                held_by="ba_review" if running is not None else "findings" if deciding else None,
                may_review=reviewer is not None
                and case.state not in use_case_reviews.DONE
                and reviewer not in case.authors
                and (row.state == "in_review" or case.submitted),
            )
        )
    return queued


@router.get("/{project}/reviews", response_model=list[SrsQueuedVersion])
async def review_queue(request: Request, project: ProjectName, user: CurrentUser, sink: Sink = None):
    """The versions in review of the documents the caller sees, the one submitted first first: each with the use cases
    it needs reviewed, their state and the reviews that count, what holds its approval now (@ba's review of the round,
    then open findings of level decide) and whether the caller may review it."""

    async def work(conn):
        access = await project_access(conn, user, project)
        settings = await store.settings(conn, access)
        writer = has_role(access.role, "writer") and user.acts_for_run is None
        queued = []
        for doc in await store.documents(conn, access, sink):
            for row in await store.versions(conn, doc.id):
                latest_draft = row.state == "draft" and row.number == doc.latest_version
                if row.state != "in_review" and not latest_draft:
                    continue
                snap = await store.snapshot(conn, row)
                wide = use_case_reviews.round_approvals(row)
                cases = await use_case_reviews.states(conn, access.project_id, doc, row, snap.doc, settings, wide)
                if row.state != "in_review":  # a draft lists the use cases sent to review on their own alone
                    cases = [case for case in cases if case.submitted]
                    if not cases:
                        continue
                deciding, running = await findings.holds(conn, doc, row)
                reviewer = user.user_id if writer else None
                use_cases = await _queued_cases(conn, access, doc, row, snap.doc, cases, reviewer)
                if cases:
                    may = any(case.may_review for case in use_cases)
                else:
                    authors = await use_case_reviews.authors_since(conn, doc, row.number)
                    may = writer and (settings.self_approval or user.user_id not in authors)
                held_by = "ba_review" if running is not None else "findings" if deciding else None
                version = cases_model(row, cases).model_dump(exclude={"use_cases"})
                queued.append(
                    SrsQueuedVersion(
                        **version,
                        use_cases=use_cases,
                        document=doc.slug,
                        document_title=doc.title,
                        held_by=held_by,
                        deciding=len(deciding),
                        may_review=may,
                    )
                )
        return sorted(
            queued, key=lambda version: (version.submitted_at is None, version.submitted_at, version.document)
        )

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
        found = snap.doc.item_index(ref, f"version {row.number} of {slug}")
        known = await revisions.history(conn, doc.id)
        key = snap.doc.items[found].key or ""
        history = [
            SrsRevision(
                rev=one.rev,
                version=one.version,
                author=one.author,
                created_at=one.created_at,
                dropped=one.dropped,
                submitted_at=one.submitted_at,
                submitted_by=one.submitted_by,
            )
            for one in known.revisions.get(key, ())
        ]
        released = known.baseline(key)
        return item_detail_model(slug, row.number, snap.doc, found, _revs(known, row.number), released, history)

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


@router.post("/{project}/documents/{slug}/versions/{number}/submit", response_model=SrsVersionCases)
async def submit_version(
    request: Request, project: ProjectName, slug: Slug, number: Number, user: CurrentUser, sink: Sink = None
):
    """Send a draft to review, once it has no error. Each time a version goes to review, in every round, @ba reviews it
    when the project enabled the agent type ba: its comments block nothing, and an open finding of level decide it
    records holds approval. The reviewers get the notice srs_review once nothing holds approval: at once when @ba
    reviews nothing, else when its review ends (``notices``)."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        settings = await store.settings(conn, access)
        sent = await reviews.submit(conn, access, doc, number, settings)
        if await review_entered(conn, access, doc, sent) is not None:
            sent = await store.version(conn, doc, number)  # with @ba's review run of the round
        else:
            await notices.tell_reviewers(conn, access.project_id, doc, sent)
        content = (await store.snapshot(conn, sent)).doc
        cases = await use_case_reviews.states(conn, access.project_id, doc, sent, content, settings, [])
        return cases_model(sent, cases, (access.name, slug))

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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        settings = await store.settings(conn, access)
        latest, cases, case, revision = await reviews.submit_use_case(conn, access, user, doc, ref, settings)
        run_id = await case_entered(conn, access, doc, latest, case, revision, user.login)
        if run_id is None:
            await notices.tell_case_reviewers(conn, access.project_id, doc, latest, case.key)
        run = None
        if run_id is not None:
            state = await conn.scalar(select(tables.runs.c.state).where(tables.runs.c.id == run_id))
            run = SrsReviewRun(id=run_id, state=state)
        where = (access.name, slug)
        found = cases_model(latest, cases, where)
        mine = next(model for model in found.use_cases if model.key == case.key)
        return SrsUseCaseSubmitted(**found.model_dump(), use_case=mine, use_case_review_run=run)

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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        settings = await store.settings(conn, access)  # with self_approval on, the author reviews it too
        asked = reviews.ReviewRequest(body.decision, body.role, body.body)
        decided = await reviews.review(conn, access, user, doc, number, asked, settings)
        if decided.state == "approved":  # the knowledge graph reads the version approved last (``kg``)
            await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)
        return version_model(decided)

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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        settings = await store.settings(conn, access)
        asked = reviews.ReviewRequest(body.decision, body.role, body.body)
        decided, cases = await reviews.review_use_case(conn, access, user, doc, number, ref, asked, settings)
        if body.decision == "approve":  # the knowledge graph reads the approved baseline (``kg``)
            await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)
        return cases_model(decided, cases, (access.name, slug))

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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        content = (
            (await store.snapshot(conn, await store.version(conn, doc, version))).doc if doc.latest_version else None
        )
        keys = None
        if item is not None:
            at = content.item_index(item, f"{slug}") if content is not None else None
            keys = threads.under(content, content.items[at].key or "") if at is not None and content else {item}
        chosen = threads.chosen(threads.threads(await reviews.comments(conn, doc)), keys, status)
        rows = sorted((row for one in chosen for row in (one.root, *one.replies)), key=lambda row: row.id)
        return [comment_model(row, threads.anchor(content, row.item, row.quote)) for row in rows]

    return await _answer(request, work)


@router.post("/{project}/documents/{slug}/comments", response_model=SrsComment, status_code=201)
async def add_comment(
    request: Request, project: ProjectName, slug: Slug, body: SrsCommentIn, user: CurrentUser, sink: Sink = None
):
    """Comment on a document, a version of it or an item, or answer a comment."""

    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        asked = reviews.CommentRequest(**body.model_dump())
        row = await reviews.comment(conn, access, user, doc, asked)
        thread = next(
            one
            for one in threads.threads(await reviews.comments(conn, doc))
            if one.root.id == (row.parent_id or row.id)
        )
        await threads.told(conn, access, doc, thread, row)
        if await threads.asked(conn, access, doc, thread, row) is not None:
            row = next(one for one in await reviews.comments(conn, doc) if one.id == row.id)  # with the run it asked
        content = (await store.snapshot(conn, await store.version(conn, doc))).doc
        return comment_model(row, threads.anchor(content, row.item, row.quote))

    return await _answer(request, work)


async def _resolved(request: Request, project: str, slug: str, comment_id: int, user, sink, *, reopen: bool):
    async def work(conn):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink, lock=True)
        return comment_model(await reviews.resolve(conn, access, user, doc, comment_id, reopen=reopen))

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
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        rows = await findings.findings(conn, doc, status)
        held = await findings.code_documents(conn, access, sink, rows)
        return [finding_model(row, held) for row in rows]

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
        access = await project_access(conn, user, project)
        if user.acts_for_run is not None:
            raise SrsError(403, "the agent of a run settles a finding with srs__finding, in its settle chat")
        reviews.writer(access, "settling a finding")
        doc = await store.document(conn, access, slug, sink, lock=True)
        held = await findings.finding(conn, doc, finding_id)
        resolution = findings.chosen(held, body.option, body.note)
        row = await findings.settle(
            conn, access, doc, finding_id, findings.Settling(findings.RESOLVED, resolution, user.user_id)
        )
        if row.level == findings.DECIDE:
            await notices.findings_settled(conn, access.project_id, doc)
        return finding_model(row, await findings.code_documents(conn, access, sink, [row]))

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
        if row.level == findings.DECIDE:  # the last one waived lets the reviewers know (``notices``)
            await notices.findings_settled(conn, access.project_id, doc)
        return finding_model(row, await findings.code_documents(conn, access, sink, [row]))

    return await _answer(request, work)


__all__ = ["router"]
