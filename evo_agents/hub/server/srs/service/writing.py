"""What the routes of srs write (``routes``), each in the caller's transaction, as the member ``user`` through the sink
``sink``: an import, a saved version, items retired, a version or a use case sent to review, a review of either, a
version withdrawn, a document archived or unarchived, a comment, a thread resolved or reopened, and a finding settled
or waived. Each answers the API's model (``models.api``); ``jobs`` is the hub's job queue, which a write the knowledge
graph reads queues a build on."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.srs.models.api import (
    SrsComment,
    SrsCommentIn,
    SrsDocumentSummary,
    SrsFinding,
    SrsImport,
    SrsImportResult,
    SrsRetireIn,
    SrsRetireResult,
    SrsReviewIn,
    SrsReviewRun,
    SrsSave,
    SrsSettleIn,
    SrsUseCaseSubmitted,
    SrsVersion,
    SrsVersionCases,
    SrsVersionDetail,
    SrsWaiveIn,
)
from evo_agents.hub.server.srs.models.doc import SrsError
from evo_agents.hub.server.srs.repository import people, store
from evo_agents.hub.server.srs.repository.versions import Writer
from evo_agents.hub.server.srs.service import edits, findings, imports, notices, reviews, threads, use_case_reviews
from evo_agents.hub.server.srs.service.auto_review import case_entered, review_entered
from evo_agents.hub.server.srs.service.convert import (
    cases_model,
    comment_model,
    edit_changes,
    finding_model,
    preview_model,
    retire_model,
    summary_model,
    version_model,
)
from evo_agents.hub.server.srs.service.reading import detail
from evo_agents.hub.server.srs.service.writes import dropped, give_screen_codes, unheld_problems, write_version
from evo_agents.hub.tables_srs import MAX_SUMMARY_CHARS


async def import_document(
    conn: AsyncConnection, user: Principal, project: str, body: SrsImport, sink: str | None
) -> SrsImportResult:
    access = await project_access(conn, user, project)
    given = body.model_dump(exclude={"label"})
    label = body.label.model_dump(exclude_none=True) if body.label else None
    done = await imports.import_markdown(conn, access, user.user_id, imports.ImportRequest(**given, label=label), sink)
    return SrsImportResult(
        document=done.document,
        created=done.created,
        version=done.version,
        dry_run=body.dry_run,
        preview=preview_model(done.preview),
    )


async def save_version(
    conn: AsyncConnection, user: Principal, project: str, slug: str, body: SrsSave, sink: str | None
) -> SrsVersionDetail:
    access = await project_access(conn, user, project)
    reviews.writer(access, "saving a version")
    doc = await store.document(conn, access, slug, sink, lock=True)
    latest = await store.snapshot(conn, await store.version(conn, doc))
    base = latest if body.base_version == latest.version.number else None
    base = base or await store.snapshot(conn, await store.version(conn, doc, body.base_version))
    content = edits.edited(latest, base, edit_changes(body.changes), body.preamble)
    await give_screen_codes(conn, access.project_id, content)
    writer = Writer(access.project_id, doc.id, user.user_id, holds(access.role, "admin"))
    template = (await store.settings(conn, access)).template
    number = await write_version(conn, writer, content, latest, template, body.summary, body.base_version)
    return await detail(conn, access, await store.document(conn, access, slug, sink), number)


def _retired_summary(changes: list[edits.Change], latest: store.Snapshot) -> str:
    keys = {change.key for change in changes}
    named = [item.code or item.title for item in latest.doc.items if item.key in keys]
    return f"Retired {', '.join(named)}"[:MAX_SUMMARY_CHARS]


async def retire_items(
    conn: AsyncConnection, user: Principal, project: str, slug: str, body: SrsRetireIn, sink: str | None
) -> SrsRetireResult:
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


async def submit_version(
    conn: AsyncConnection, user: Principal, project: str, slug: str, number: int, sink: str | None
) -> SrsVersionCases:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink, lock=True)
    settings = await store.settings(conn, access)
    sent = await reviews.submit(conn, access, doc, number, settings)
    if await review_entered(conn, access, doc, sent) is not None:
        sent = await store.version(conn, doc, number)  # with @ba's review run of the round
        if not settings.wait_for_ba:  # @ba's review is advice: the reviewers need not wait for it
            await notices.tell_reviewers(conn, access.project_id, doc, sent)
    else:
        await notices.tell_reviewers(conn, access.project_id, doc, sent)
    content = (await store.snapshot(conn, sent)).doc
    cases = await use_case_reviews.states(conn, access.project_id, doc, sent, content, settings, [])
    return cases_model(sent, cases, (access.name, slug))


async def submit_use_case(
    conn: AsyncConnection, user: Principal, project: str, slug: str, ref: str, sink: str | None
) -> SrsUseCaseSubmitted:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink, lock=True)
    settings = await store.settings(conn, access)
    latest, cases, case, revision = await reviews.submit_use_case(conn, access, user, doc, ref, settings)
    run_id = await case_entered(conn, access, doc, latest, case, revision, user.login)
    if run_id is None or not settings.wait_for_ba:
        await notices.tell_case_reviewers(conn, access.project_id, doc, latest, case.key)
    run = None
    if run_id is not None:
        run = SrsReviewRun(id=run_id, state=await people.run_state(conn, run_id))
    where = (access.name, slug)
    found = cases_model(latest, cases, where)
    mine = next(model for model in found.use_cases if model.key == case.key)
    return SrsUseCaseSubmitted(**found.model_dump(), use_case=mine, use_case_review_run=run)


async def review_version(
    conn: AsyncConnection,
    jobs,
    user: Principal,
    project: str,
    slug: str,
    number: int,
    body: SrsReviewIn,
    sink: str | None,
) -> SrsVersion:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink, lock=True)
    settings = await store.settings(conn, access)  # with self_approval on, the author reviews it too
    asked = reviews.ReviewRequest(body.decision, body.role, body.body)
    decided = await reviews.review(conn, access, user, doc, number, asked, settings)
    if decided.state == "approved":  # the knowledge graph reads the version approved last (``kg``)
        await queue_if_configured(jobs, conn, access.project_id, access.name)
    return version_model(decided)


async def review_use_case(
    conn: AsyncConnection,
    jobs,
    user: Principal,
    project: str,
    slug: str,
    number: int,
    ref: str,
    body: SrsReviewIn,
    sink: str | None,
) -> SrsVersionCases:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink, lock=True)
    settings = await store.settings(conn, access)
    asked = reviews.ReviewRequest(body.decision, body.role, body.body)
    decided, cases = await reviews.review_use_case(conn, access, user, doc, number, ref, asked, settings)
    if body.decision == "approve":  # the knowledge graph reads the approved baseline (``kg``)
        await queue_if_configured(jobs, conn, access.project_id, access.name)
    return cases_model(decided, cases, (access.name, slug))


async def withdraw_version(
    conn: AsyncConnection, user: Principal, project: str, slug: str, number: int, reason: str | None, sink: str | None
) -> SrsVersion:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink, lock=True)
    return version_model(await reviews.withdraw(conn, access, user, doc, number, reason))


async def archive(
    conn: AsyncConnection, jobs, user: Principal, project: str, slug: str, sink: str | None, archived: bool
) -> SrsDocumentSummary:
    """Archive document ``slug`` of ``project`` or unarchive it, queue a build of the knowledge graph, which leaves
    archived documents out, and answer the document."""
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink, lock=True, even_archived=True)
    await reviews.archive(conn, access, user, doc, archived)
    await queue_if_configured(jobs, conn, access.project_id, access.name)
    return summary_model(await store.document(conn, access, slug, sink))


async def add_comment(
    conn: AsyncConnection, user: Principal, project: str, slug: str, body: SrsCommentIn, sink: str | None
) -> SrsComment:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink, lock=True)
    asked = reviews.CommentRequest(**body.model_dump())
    row = await reviews.comment(conn, access, user, doc, asked)
    thread = next(
        one for one in threads.threads(await reviews.comments(conn, doc)) if one.root.id == (row.parent_id or row.id)
    )
    await threads.told(conn, access, doc, thread, row)
    if await threads.asked(conn, access, doc, thread, row) is not None:
        row = next(one for one in await reviews.comments(conn, doc) if one.id == row.id)  # with the run it asked
    content = (await store.snapshot(conn, await store.version(conn, doc))).doc
    return comment_model(row, threads.anchor(content, row.item, row.quote))


async def resolve_comment(
    conn: AsyncConnection, user: Principal, project: str, slug: str, comment_id: int, sink: str | None, *, reopen: bool
) -> SrsComment:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink, lock=True)
    return comment_model(await reviews.resolve(conn, access, user, doc, comment_id, reopen=reopen))


async def settle_finding(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    slug: str,
    finding_id: int,
    body: SrsSettleIn,
    sink: str | None,
) -> SrsFinding:
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


async def waive_finding(
    conn: AsyncConnection, user: Principal, project: str, slug: str, finding_id: int, body: SrsWaiveIn, sink: str | None
) -> SrsFinding:
    access = await project_access(conn, user, project)
    reviews.admin(access, user, "waives a finding")
    doc = await store.document(conn, access, slug, sink, lock=True)
    waived = findings.Settling(findings.WAIVED, body.reason, user.user_id)
    row = await findings.settle(conn, access, doc, finding_id, waived)
    if row.level == findings.DECIDE:  # the last one waived lets the reviewers know (``notices``)
        await notices.findings_settled(conn, access.project_id, doc)
    return finding_model(row, await findings.code_documents(conn, access, sink, [row]))


__all__ = [
    "add_comment",
    "archive",
    "import_document",
    "resolve_comment",
    "retire_items",
    "review_use_case",
    "review_version",
    "save_version",
    "settle_finding",
    "submit_use_case",
    "submit_version",
    "waive_finding",
    "withdraw_version",
]
