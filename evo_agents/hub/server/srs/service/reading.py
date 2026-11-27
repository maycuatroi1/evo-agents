"""What the routes of srs read (``routes``), each in the caller's transaction, as the member ``user`` through the sink
``sink``: the documents, the review queue, a document, its versions, a version whole, an item, the difference
between two versions, a version as Markdown, the comments and the findings. Each answers the API's model
(``models.api``)."""

from __future__ import annotations

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.srs.models import checks, markdown
from evo_agents.hub.server.srs.models.api import (
    SrsComment,
    SrsDiff,
    SrsDocument,
    SrsDocumentSummary,
    SrsExport,
    SrsFinding,
    SrsItemDetail,
    SrsQueuedUseCase,
    SrsQueuedVersion,
    SrsRevision,
    SrsVersion,
    SrsVersionDetail,
)
from evo_agents.hub.server.srs.models.diff import diff, lines_between
from evo_agents.hub.server.srs.repository import revisions, store
from evo_agents.hub.server.srs.service import findings, reviews, threads, use_case_reviews
from evo_agents.hub.server.srs.service.convert import (
    cases_model,
    comment_model,
    detail_model,
    diff_model,
    finding_model,
    item_detail_model,
    summary_model,
    use_case_model,
    version_model,
)


def revs(known: revisions.History, number: int) -> dict[str, int]:
    """The revision each use case has in version ``number``, by key."""
    found = {}
    for key in known.revisions:
        revision = known.at(key, number)
        if revision is not None and not revision.dropped:
            found[key] = revision.rev
    return found


async def detail(
    conn: AsyncConnection, access: ProjectAccess, doc: store.DocumentRow, number: int | None
) -> SrsVersionDetail:
    """Version ``number`` of ``doc`` (the latest when None) whole: its items, its problems and its use cases."""
    row = await store.version(conn, doc, number)
    snap = await store.snapshot(conn, row)
    settings = await store.settings(conn, access)
    wide = use_case_reviews.round_approvals(row)
    known = await revisions.history(conn, doc.id)
    cases = await use_case_reviews.states(conn, access.project_id, doc, row, snap.doc, settings, wide, known)
    problems = checks.problems(snap.doc, settings.template)
    released = {key: rev for key in known.releases if (rev := known.baseline(key)) is not None}
    return detail_model(doc.slug, row, snap.doc, problems, cases, access.name, revs(known, row.number), released)


async def list_documents(
    conn: AsyncConnection, user: Principal, project: str, sink: str | None, with_archived: bool
) -> list[SrsDocumentSummary]:
    access = await project_access(conn, user, project)
    found = await store.documents(conn, access, sink, with_archived=with_archived)
    return [summary_model(row) for row in found]


def _held_by(settings: store.Settings, running, deciding) -> Literal["ba_review", "findings"] | None:
    """What holds an approval now: @ba's review of the round, else open findings to decide; nothing of @ba holds one
    with the config's ``wait_for_ba`` off."""
    if not settings.wait_for_ba:
        return None
    return "ba_review" if running is not None else "findings" if deciding else None


async def _queued_cases(
    conn: AsyncConnection,
    access: ProjectAccess,
    doc: store.DocumentRow,
    row: store.VersionRow,
    content,
    cases,
    reviewer: Principal | None,
    settings: store.Settings,
) -> list[SrsQueuedUseCase]:
    """The use cases to review of version ``row``, whose content is ``content``, each with its intent as the version
    reads, the open findings of the document about it or a business rule under it, by level, what holds its approval
    (nothing of @ba with ``wait_for_ba`` off) and whether ``reviewer`` (None: nobody, the caller is no writer) may
    review it."""
    admin = holds(access.role, "admin")
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
                held_by=_held_by(settings, running, deciding),
                may_review=reviewer is not None
                and case.state not in use_case_reviews.DONE
                and reviewer.user_id not in case.authors
                and use_case_reviews.may_decide(case.owners, reviewer.login, admin)
                and (row.state == "in_review" or case.submitted),
            )
        )
    return queued


async def _queued_version(
    conn: AsyncConnection,
    access: ProjectAccess,
    user: Principal,
    doc: store.DocumentRow,
    row: store.VersionRow,
    settings: store.Settings,
) -> SrsQueuedVersion | None:
    """Version ``row`` of ``doc`` as the review queue lists it, or None when it lists it not."""
    writer = holds(access.role, "writer") and user.acts_for_run is None
    latest_draft = row.state == "draft" and row.number == doc.latest_version
    if row.state != "in_review" and not latest_draft:
        return None
    snap = await store.snapshot(conn, row)
    wide = use_case_reviews.round_approvals(row)
    cases = await use_case_reviews.states(conn, access.project_id, doc, row, snap.doc, settings, wide)
    if row.state != "in_review":  # a draft lists the use cases sent to review on their own alone
        cases = [case for case in cases if case.submitted]
        if not cases:
            return None
    deciding, running = await findings.holds(conn, doc, row)
    reviewer = user if writer else None
    use_cases = await _queued_cases(conn, access, doc, row, snap.doc, cases, reviewer, settings)
    if cases:
        may = any(case.may_review for case in use_cases)
    else:
        authors = await use_case_reviews.authors_since(conn, doc, row.number)
        may = writer and (settings.self_approval or user.user_id not in authors)
    version = cases_model(row, cases).model_dump(exclude={"use_cases"})
    return SrsQueuedVersion(
        **version,
        use_cases=use_cases,
        document=doc.slug,
        document_title=doc.title,
        held_by=_held_by(settings, running, deciding),
        deciding=len(deciding),
        may_review=may,
    )


async def review_queue(
    conn: AsyncConnection, user: Principal, project: str, sink: str | None
) -> list[SrsQueuedVersion]:
    access = await project_access(conn, user, project)
    settings = await store.settings(conn, access)
    queued = []
    for doc in await store.documents(conn, access, sink):
        for row in await store.versions(conn, doc.id):
            found = await _queued_version(conn, access, user, doc, row, settings)
            if found is not None:
                queued.append(found)
    return sorted(queued, key=lambda version: (version.submitted_at is None, version.submitted_at, version.document))


async def show_document(
    conn: AsyncConnection, user: Principal, project: str, slug: str, version: int | None, sink: str | None
) -> SrsDocument:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink)
    shown = await detail(conn, access, doc, version) if doc.latest_version else None
    versions = [version_model(row) for row in await store.versions(conn, doc.id)]
    return SrsDocument(**summary_model(doc).model_dump(), versions=versions, shown=shown)


async def list_versions(
    conn: AsyncConnection, user: Principal, project: str, slug: str, sink: str | None
) -> list[SrsVersion]:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink)
    return [version_model(row) for row in await store.versions(conn, doc.id)]


async def show_version(
    conn: AsyncConnection, user: Principal, project: str, slug: str, number: int, sink: str | None
) -> SrsVersionDetail:
    access = await project_access(conn, user, project)
    return await detail(conn, access, await store.document(conn, access, slug, sink), number)


async def show_item(
    conn: AsyncConnection, user: Principal, project: str, slug: str, ref: str, version: int | None, sink: str | None
) -> SrsItemDetail:
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
    return item_detail_model(slug, row.number, snap.doc, found, revs(known, row.number), released, history)


async def compare_versions(
    conn: AsyncConnection, user: Principal, project: str, slug: str, low: int, high: int, sink: str | None
) -> SrsDiff:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink)
    old = await store.snapshot(conn, await store.version(conn, doc, low))
    new = await store.snapshot(conn, await store.version(conn, doc, high))
    preamble = lines_between(old.doc.preamble, new.doc.preamble) if old.doc.preamble != new.doc.preamble else []
    return diff_model(slug, low, high, preamble, diff(old.doc, new.doc))


async def export_version(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    slug: str,
    version: int | None,
    anchors: bool,
    sink: str | None,
) -> SrsExport:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink)
    row = await store.version(conn, doc, version)
    snap = await store.snapshot(conn, row)
    return SrsExport(document=slug, version=row.number, markdown=markdown.render(snap.doc, anchors=anchors))


async def list_comments(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    slug: str,
    version: int | None,
    item: str | None,
    status: Literal["open", "resolved"] | None,
    sink: str | None,
) -> list[SrsComment]:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink)
    content = (await store.snapshot(conn, await store.version(conn, doc, version))).doc if doc.latest_version else None
    keys = None
    if item is not None:
        at = content.item_index(item, f"{slug}") if content is not None else None
        keys = threads.under(content, content.items[at].key or "") if at is not None and content else {item}
    chosen = threads.chosen(threads.threads(await reviews.comments(conn, doc)), keys, status)
    rows = sorted((row for one in chosen for row in (one.root, *one.replies)), key=lambda row: row.id)
    return [comment_model(row, threads.anchor(content, row.item, row.quote)) for row in rows]


async def list_findings(
    conn: AsyncConnection, user: Principal, project: str, slug: str, status: str | None, sink: str | None
) -> list[SrsFinding]:
    access = await project_access(conn, user, project)
    doc = await store.document(conn, access, slug, sink)
    rows = await findings.findings(conn, doc, status)
    held = await findings.code_documents(conn, access, sink, rows)
    return [finding_model(row, held) for row in rows]


__all__ = [
    "compare_versions",
    "detail",
    "export_version",
    "list_comments",
    "list_documents",
    "list_findings",
    "list_versions",
    "review_queue",
    "revs",
    "show_document",
    "show_item",
    "show_version",
]
