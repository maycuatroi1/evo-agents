"""Deciding versions and documents, and keeping comments.

- ``submit``: a writer sends a draft to review once it has no error (``checks``); its review round goes up by one,
  and the round has no review run of @ba until ``auto_review`` hands one.
- ``review``: a writer approves a version in review or asks for changes. Nobody who wrote a version since the one
  approved last (the version under review included) reviews it, admins included, unless the config's ``self_approval``
  is on: then the author reviews it as any writer does. The agent of a run never reviews one, the switch on or off.
  An approval goes through the gate of ``findings.approval_hold`` first: no open finding of level decide in the
  document, and no review of @ba of the version's current round still queued or running (409 otherwise). A request for
  changes is never held, and sends the version back to draft. An approval counts for a role the reviewer holds in the
  config's ``roles``; the version is approved once every role the ``approval_policy`` of the document's kind requires
  approved it in this round, or after one approval when it requires none. Approving it supersedes every older version
  but a withdrawn one, and emits ``srs.version.approved`` in the same transaction (``events.version_approved``), from
  which the plugin keeps the project's tasks in step (``auto_tasks``).
- ``review_use_case``: a writer approves or asks for changes on one use case of a version in review, or of a version
  whose revision of it was sent to review on its own (plan srs-use-case-review, ``use_case_reviews``). The author rule
  is the use case's: nobody who wrote a revision of it since its newest release reviews it, and writing one use case
  bars nobody from the others. A later review of theirs replaces the earlier one. An approval goes through the gate
  narrowed to the use case (``findings.Case``); a request for changes needs a note, is never held, takes a revision sent
  on its own back out of review, and leaves the version in review, where it blocks every approval (a version-wide one is
  still recorded). A review that makes the use case approved releases its revision into the approved baseline at once
  (``events.use_cases_released``), whatever becomes of the version. The version is approved by the review that makes
  every use case it adds, changes or drops since the version approved last approved or superseded, while nothing of the
  whole document holds it (``findings.held_now``); a version with no such use case is approved by version-wide reviews
  alone, as before.
- ``submit_use_case``: a writer sends the revision a use case has in the latest version to review on its own, while
  that version is not in review: a revision not approved yet, not in review already, with no error of the use case or
  of a business rule under it.
- ``withdraw``: the author of a version in review, or an admin of the project, takes it out of review for good: it is
  withdrawn, and never submitted, reviewed or approved again; a reason given is a comment on it. The agent of a run
  never withdraws one.
- ``archive``: an admin of the project archives a document, or unarchives it. An archived document is out of the list
  of documents (unless asked for), of @ba's search and of the knowledge graph, still read by its slug, and takes no
  write (``store.writable``); its codes stay held and its slug taken. The agent of a run never archives one.
- ``admin``: what only an admin of the project does, never the agent of a run: archiving, unarchiving, and retiring
  coded items by their codes (``server``, ``retire_items``).
- ``comments``, ``comment``, ``resolve``: threads on a document, a version or an item; writers write them and close
  them, readers read them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.srs import ROLES, checks, events, findings, notices, revisions, store, use_case_reviews
from evo_agents.hub.server.srs.doc import Document, SrsError
from evo_agents.hub.server.srs.store import DocumentRow, ReviewRow, Settings, VersionRow
from evo_agents.hub.server.srs.use_case_reviews import (
    APPROVE,
    APPROVED,
    REQUEST_CHANGES,
    SUPERSEDED,
    CaseReview,
    CaseState,
    approved_by,
    case_name,
)

WITHDRAWN = "withdrawn"
LAST_STATES = ("superseded", WITHDRAWN)  # a version in one never changes state again


def writer(access: ProjectAccess, what: str) -> None:
    if not has_role(access.role, "writer"):
        raise SrsError(403, f"{what} in project {access.name} needs the writer role on it")


async def submit(conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, number: int, settings: Settings):
    """Send draft ``number`` of ``doc`` to review; its row."""
    writer(access, "submitting a version")
    held = await store.version(conn, doc, number, lock=True)
    if held.state != "draft":
        raise SrsError(409, f"version {number} of {doc.slug} is {held.state}: only a draft is submitted")
    errors = checks.blocking(checks.problems((await store.snapshot(conn, held)).doc, settings.template))
    if errors:
        raise SrsError(422, f"version {number} is not submitted: {errors[0].message}", errors)
    v = tables.srs.versions
    changed = {"state": "in_review", "round": v.c.round + 1, "submitted_at": func.now(), "review_run_id": None}
    await conn.execute(update(v).where(v.c.id == held.id).values(changed))
    return await store.version(conn, doc, number)


def role_for(
    settings: Settings, login: str, asked: str | None, kind: str, approvals: Sequence[ReviewRow | CaseReview]
) -> str | None:
    """The role a review of ``login`` counts for: the one asked, which they must hold, else the first role the policy
    requires that they hold and no approval of the round filled, else the first they hold; None when they hold none."""
    held = [role for role in ROLES if login in (settings.roles.get(role) or [])]
    if asked is not None:
        if asked not in held:
            raise SrsError(403, f"{login} does not hold the role {asked} in the config of plugin srs")
        return asked
    if not held:
        return None
    required = (settings.policy.get(kind) or {}).get("required") or []
    filled = {review.role for review in approvals}
    return next((role for role in required if role in held and role not in filled), held[0])


async def _approve(conn: AsyncConnection, doc: DocumentRow, held: VersionRow) -> None:
    s = tables.srs
    v, d = s.versions, s.documents
    await conn.execute(update(v).where(v.c.id == held.id).values(state="approved", decided_at=func.now()))
    older = and_(v.c.document_id == doc.id, v.c.number < held.number, v.c.state.not_in(LAST_STATES))
    await conn.execute(update(v).where(older).values(state="superseded"))
    await conn.execute(update(d).where(d.c.id == doc.id).values(approved_version=held.number))


async def _may_review(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, doc, held: VersionRow, settings: Settings
) -> None:
    """Refuse ``user`` a review of ``held``: the agent of a run always, a member without writer, a version not in
    review, an author since the version approved last (unless ``settings.self_approval``)."""
    if user.acts_for_run is not None:
        raise SrsError(403, "the agent of a run never approves or returns a version: a member of the project does")
    writer(access, "reviewing a version")
    if held.state != "in_review":
        raise SrsError(
            409, f"version {held.number} of {doc.slug} is {held.state}: only a version in review is reviewed"
        )
    if not settings.self_approval and user.user_id in await use_case_reviews.authors_since(conn, doc, held.number):
        since = f"version {doc.approved_version}" if doc.approved_version else "the first version"
        raise SrsError(403, f"{user.login} wrote a version of {doc.slug} since {since}: another member reviews it")


async def _checked_reviewer(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, doc, held: VersionRow, settings: Settings
) -> None:
    """``_may_review``, and refuse a second review of the version this round."""
    await _may_review(conn, access, user, doc, held, settings)
    if any(review.reviewer_id == user.user_id and review.round == held.round for review in held.reviews):
        raise SrsError(409, f"{user.login} reviewed version {held.number} in this round already")


@dataclass(frozen=True)
class ReviewRequest:
    decision: str
    role: str | None = None
    body: str = ""


async def review(
    conn: AsyncConnection,
    access: ProjectAccess,
    user: Principal,
    doc: DocumentRow,
    number: int,
    request: ReviewRequest,
    settings: Settings,
) -> VersionRow:
    """Approve version ``number`` of ``doc`` or ask for changes, as ``user``; its row after. The caller holds the
    document's row lock."""
    held = await store.version(conn, doc, number, lock=True)
    await _checked_reviewer(conn, access, user, doc, held, settings)
    if request.decision == APPROVE:
        await findings.approval_hold(conn, access, doc, held)
    approvals = [r for r in held.reviews if r.round == held.round and r.decision == APPROVE]
    role = role_for(settings, user.login, request.role, doc.kind, approvals)
    s = tables.srs
    values = {
        "version_id": held.id,
        "round": held.round,
        "reviewer_id": user.user_id,
        "role": role,
        "decision": request.decision,
        "body": request.body,
    }
    await conn.execute(insert(s.reviews).values(values))
    reviewer = (user.user_id, user.login)
    if request.decision == REQUEST_CHANGES:
        await conn.execute(update(s.versions).where(s.versions.c.id == held.id).values(state="draft"))
        await notices.tell_author(conn, access.project_id, doc, held, reviewer, notices.CHANGES, note=request.body)
    else:
        mine = ReviewRow(user.login, user.user_id, role, APPROVE, request.body, held.round, held.created_at)
        approves, cases = await _approves(conn, access, doc, held, settings, [*approvals, mine])
        await _released(conn, access, doc, held, user, cases)
        if approves:
            await _approve(conn, doc, held)
            await events.version_approved(conn, access.project_id, doc, held, doc.approved_version, user)
            await notices.tell_author(conn, access.project_id, doc, held, reviewer, notices.APPROVED)
    return await store.version(conn, doc, number)


async def _approves(
    conn: AsyncConnection,
    access: ProjectAccess,
    doc: DocumentRow,
    held: VersionRow,
    settings: Settings,
    approvals: list[ReviewRow],
) -> tuple[bool, list[CaseState]]:
    """Whether the version-wide ``approvals`` of the round, with the reviews of use cases that count, approve ``held``:
    every use case to review approved, or the policy satisfied by ``approvals`` when it has none; and the use cases to
    review as they leave them."""
    doc_now = (await store.snapshot(conn, held)).doc
    cases = await use_case_reviews.states(conn, access.project_id, doc, held, doc_now, settings, approvals)
    if cases:
        return use_case_reviews.ready(cases), cases
    return approved_by(settings.policy.get(doc.kind), approvals), cases


async def _released(
    conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, held: VersionRow, user: Principal, cases
) -> list[events.Released]:
    """Release each of ``cases`` a review of ``user`` just made approved, and tell the author of each revision."""
    done = await events.use_cases_released(conn, access.project_id, doc, held, user, cases)
    for released in done:
        await notices.tell_case_author(conn, access.project_id, doc, held, (user.user_id, user.login), released.case)
    return done


def _subtree(content: Document, key: str) -> set[str]:
    """The keys of use case ``key`` of ``content`` and of every item under it."""
    at = next((index for index, item in enumerate(content.items) if item.key == key), None)
    if at is None:
        return {key}
    return {key} | {item.key for index, item in enumerate(content.items) if at in content.ancestors(index) and item.key}


async def _case_of(
    conn: AsyncConnection, access: ProjectAccess, doc: DocumentRow, number: int, ref: str, cases: list[CaseState]
) -> CaseState:
    item_id = await store.item_id(conn, access, doc, ref)
    key = await use_case_reviews.key_of(conn, item_id)
    case = next((case for case in cases if case.key == key), None)
    if case is None:
        named = ", ".join(c.code or c.key for c in cases) or "none"
        raise SrsError(
            422,
            f"{ref} is not a use case version {number} of {doc.slug} needs reviewed: a use case it adds, changes or "
            f"drops since the version approved last is, and these are: {named}",
        )
    return case


def _reviewable(access: ProjectAccess, user: Principal, doc: DocumentRow, held: VersionRow, case: CaseState, settings):
    """Refuse ``user`` a review of use case ``case`` of version ``held``: the agent of a run always, a member without
    writer, a version not in review whose revision of the use case was not sent on its own, a revision approved
    already or superseded, a member who wrote a revision of it since its newest release (unless ``self_approval``)."""
    if user.acts_for_run is not None:
        raise SrsError(403, "the agent of a run never approves or returns a use case: a member of the project does")
    writer(access, "reviewing a use case")
    name = case_name(case)
    if held.state != "in_review" and not case.submitted:
        submit = f"evo-agents hub srs submit {doc.slug} --item '{case.code or case.key}' --project {access.name}"
        raise SrsError(
            409,
            f"version {held.number} of {doc.slug} is {held.state} and {name} was not sent to review on its own: "
            f"`{submit}` sends it, or submit the version",
        )
    if case.state == SUPERSEDED:
        raise SrsError(409, f"{name} is superseded: r{case.released} of it is approved, which is newer")
    if case.state == APPROVED and case.released is not None and case.released == case.rev:
        raise SrsError(409, f"{name} is approved already")
    if not settings.self_approval and user.user_id in case.authors:
        since = f"r{case.released}, its last approved revision" if case.released else "it was first written"
        raise SrsError(
            403, f"{user.login} wrote a revision of {case.code or case.title} since {since}: another member reviews it"
        )


async def review_use_case(
    conn: AsyncConnection,
    access: ProjectAccess,
    user: Principal,
    doc: DocumentRow,
    number: int,
    ref: str,
    request: ReviewRequest,
    settings: Settings,
) -> tuple[VersionRow, list[CaseState]]:
    """Approve use case ``ref`` (code, alias or key) of version ``number`` of ``doc`` or ask for changes on it, as
    ``user``; the version's row and its use cases to review after. The caller holds the document's row lock."""
    held = await store.version(conn, doc, number, lock=True)
    if user.acts_for_run is not None:
        raise SrsError(403, "the agent of a run never approves or returns a use case: a member of the project does")
    writer(access, "reviewing a use case")
    content = (await store.snapshot(conn, held)).doc
    wide = use_case_reviews.round_approvals(held)
    before = await use_case_reviews.states(conn, access.project_id, doc, held, content, settings, wide)
    case = await _case_of(conn, access, doc, number, ref, before)
    _reviewable(access, user, doc, held, case, settings)
    if request.decision == REQUEST_CHANGES and not request.body.strip():
        raise SrsError(422, "a request for changes on a use case needs a note saying what to change")
    if request.decision == APPROVE:
        hold = findings.Case(case.key, case_name(case), content, case.review_run_id)
        await findings.approval_hold(conn, access, doc, held, hold)
    others = [r for r in case.reviews if r.reviewer_id != user.user_id and r.decision == APPROVE]
    role = role_for(settings, user.login, request.role, doc.kind, [*others, *wide])
    item_id = await store.item_id(conn, access, doc, case.key)
    await use_case_reviews.record(conn, held, item_id, user.user_id, role, request.decision, request.body)
    after = await use_case_reviews.states(conn, access.project_id, doc, held, content, settings, wide)
    reviewer = (user.user_id, user.login)
    if request.decision == APPROVE:
        await _released(conn, access, doc, held, user, after)
    else:
        if case.submitted:
            revision = (await revisions.history(conn, doc.id)).at(case.key, held.number)
            if revision is not None:
                await revisions.unsubmit(conn, revision)
        await notices.tell_case_changes(conn, access.project_id, doc, held, reviewer, case, request.body)
    after = await use_case_reviews.states(conn, access.project_id, doc, held, content, settings, wide)
    if (
        request.decision == APPROVE
        and held.state == "in_review"
        and use_case_reviews.ready(after)
        and not await findings.held_now(conn, doc, held)
    ):
        await _approve(conn, doc, held)
        await events.version_approved(conn, access.project_id, doc, held, doc.approved_version, user)
        await notices.tell_author(conn, access.project_id, doc, held, reviewer, notices.APPROVED)
    return await store.version(conn, doc, number), after


async def submit_use_case(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, doc: DocumentRow, ref: str, settings: Settings
) -> tuple[VersionRow, list[CaseState], CaseState, revisions.Revision]:
    """Send the revision use case ``ref`` (code, alias or key) has in the latest version of ``doc`` to review on its
    own, as ``user``; the latest version, its use cases to review, the use case and its revision. The caller holds the
    document's row lock."""
    writer(access, "submitting a use case")
    latest = await store.version(conn, doc)
    if latest.state == "in_review":
        raise SrsError(
            409,
            f"version {latest.number} of {doc.slug} is in review: its use cases are reviewed there, each on its own",
        )
    content = (await store.snapshot(conn, latest)).doc
    key = await use_case_reviews.key_of(conn, await store.item_id(conn, access, doc, ref))
    known = await revisions.history(conn, doc.id)
    revision = known.at(key, latest.number)
    if revision is None:
        raise SrsError(422, f"{ref} is no use case of version {latest.number} of {doc.slug}")
    name = f"{revision.code or ref} r{revision.rev}"
    if revision.dropped and known.baseline(key) is None:
        raise SrsError(422, f"{name}: version {latest.number} dropped a use case that was never approved")
    release = known.release(key)
    if release is not None and release.rev >= revision.rev:
        raise SrsError(409, f"{name} is approved already: version {latest.number} does not change it")
    if revision.submitted_at is not None:
        raise SrsError(409, f"{name} is in review already, sent by {revision.submitted_by}")
    if not revision.dropped:
        under = _subtree(content, key)
        errors = [p for p in checks.blocking(checks.problems(content, settings.template)) if p.item in under]
        if errors:
            raise SrsError(422, f"{name} is not submitted: {errors[0].message}", errors)
    await revisions.submit(conn, revision, user.user_id)
    cases = await use_case_reviews.states(conn, access.project_id, doc, latest, content, settings, [])
    case = next((case for case in cases if case.key == key), None)
    if case is None:
        raise SrsError(409, f"{name} is no use case version {latest.number} of {doc.slug} needs reviewed")
    return latest, cases, case, revision


async def withdraw(
    conn: AsyncConnection,
    access: ProjectAccess,
    user: Principal,
    doc: DocumentRow,
    number: int,
    reason: str | None = None,
) -> VersionRow:
    """Withdraw version ``number`` of ``doc``, in review, as ``user``, its author or an admin of the project: it is
    withdrawn for good, and ``reason``, when given, is a comment on it. Its row after. The caller holds the document's
    row lock."""
    if user.scope is not None:
        raise SrsError(403, "the agent of a run never withdraws a version: its author or an admin of the project does")
    writer(access, "withdrawing a version")
    held = await store.version(conn, doc, number, lock=True)
    if held.author_id != user.user_id and not has_role(access.role, "admin"):
        raise SrsError(
            403,
            f"version {number} of {doc.slug} is {held.author}'s: its author or an admin of {access.name} withdraws it",
        )
    if held.state != "in_review":
        raise SrsError(409, f"version {number} of {doc.slug} is {held.state}: only a version in review is withdrawn")
    v = tables.srs.versions
    await conn.execute(update(v).where(v.c.id == held.id).values(state=WITHDRAWN, decided_at=func.now()))
    if reason:
        await comment(conn, access, user, doc, CommentRequest(reason, version=number))
    return await store.version(conn, doc, number)


def admin(access: ProjectAccess, user: Principal, what: str) -> None:
    """Only an admin of the project does ``what`` (such as "archives a document"), and never the agent of a run, whose
    scope refuses it before anything else is read."""
    if user.scope is not None:
        raise SrsError(403, f"the agent of a run never {what}: an admin of the project does")
    if not has_role(access.role, "admin"):
        raise SrsError(403, f"only an admin of project {access.name} {what}")


async def archive(conn: AsyncConnection, access: ProjectAccess, user: Principal, doc: DocumentRow, archived: bool):
    """Archive ``doc`` (``archived``) or unarchive it, as ``user``, an admin of the project; 409 when it is so already.
    The caller holds the document's row lock and reads the document again."""
    admin(access, user, f"{'archives' if archived else 'unarchives'} a document")
    if (doc.archived_at is not None) == archived:
        state = "archived already" if archived else "not archived"
        raise SrsError(409, f"document {doc.slug} of project {access.name} is {state}")
    d = tables.srs.documents
    await conn.execute(update(d).where(d.c.id == doc.id).values(archived_at=func.now() if archived else None))


# Comments


@dataclass(frozen=True)
class CommentRow:
    id: int
    version: int | None
    item: str | None
    item_code: str | None
    parent_id: int | None
    author: str
    run_id: int | None
    body: str
    created_at: datetime
    resolved_by: str | None
    resolved_at: datetime | None


def _comments_query(document_id: int):
    s, u = tables.srs, tables.users
    c, v, i = s.comments, s.versions, s.items
    author, resolver = u.alias("author"), u.alias("resolver")
    return (
        select(
            c.c.id,
            v.c.number.label("version"),
            i.c.key.label("item"),
            i.c.code.label("item_code"),
            c.c.parent_id,
            author.c.login.label("author"),
            c.c.run_id,
            c.c.body,
            c.c.created_at,
            resolver.c.login.label("resolved_by"),
            c.c.resolved_at,
        )
        .select_from(
            c.join(author, author.c.id == c.c.author_id)
            .outerjoin(v, v.c.id == c.c.version_id)
            .outerjoin(i, i.c.id == c.c.item_id)
            .outerjoin(resolver, resolver.c.id == c.c.resolved_by)
        )
        .where(c.c.document_id == document_id)
    )


async def comments(conn: AsyncConnection, doc: DocumentRow) -> list[CommentRow]:
    """The comments of ``doc``, oldest first."""
    query = _comments_query(doc.id).order_by(tables.srs.comments.c.id)
    return [CommentRow(**row._mapping) for row in await conn.execute(query)]


@dataclass(frozen=True)
class CommentRequest:
    body: str
    version: int | None = None
    item: str | None = None
    parent_id: int | None = None


async def _parent_checked(conn: AsyncConnection, doc: DocumentRow, parent_id: int | None) -> None:
    if parent_id is None:
        return
    c = tables.srs.comments
    held = select(c.c.id).where(c.c.id == parent_id, c.c.document_id == doc.id)
    if (await conn.execute(held)).first() is None:
        raise SrsError(422, f"document {doc.slug} has no comment {parent_id}")


async def comment(
    conn: AsyncConnection,
    access: ProjectAccess,
    user: Principal,
    doc: DocumentRow,
    request: CommentRequest,
    run_id: int | None = None,
) -> CommentRow:
    """Add a comment to ``doc`` as ``user``; the comment."""
    writer(access, "commenting on a document")
    version_id = (await store.version(conn, doc, request.version)).id if request.version is not None else None
    item_id = await store.item_id(conn, access, doc, request.item) if request.item else None
    await _parent_checked(conn, doc, request.parent_id)
    c = tables.srs.comments
    values = {
        "document_id": doc.id,
        "version_id": version_id,
        "item_id": item_id,
        "parent_id": request.parent_id,
        "author_id": user.user_id,
        "run_id": run_id,
        "body": request.body,
    }
    comment_id = (await conn.execute(insert(c).values(values).returning(c.c.id))).scalar_one()
    return CommentRow(**(await conn.execute(_comments_query(doc.id).where(c.c.id == comment_id))).one()._mapping)


async def resolve(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, doc: DocumentRow, comment_id: int
) -> CommentRow:
    """Close the thread of comment ``comment_id`` as ``user``; the comment."""
    writer(access, "closing a thread of comments")
    c = tables.srs.comments
    held = and_(c.c.id == comment_id, c.c.document_id == doc.id)
    done = await conn.execute(
        update(c).where(held, c.c.resolved_at.is_(None)).values(resolved_by=user.user_id, resolved_at=func.now())
    )
    row = (await conn.execute(_comments_query(doc.id).where(c.c.id == comment_id))).first()
    if row is None:
        raise SrsError(404, f"document {doc.slug} has no comment {comment_id}")
    if not done.rowcount and row.resolved_at is None:
        raise SrsError(409, f"comment {comment_id} could not be resolved")
    return CommentRow(**row._mapping)


__all__ = [
    "APPROVE",
    "REQUEST_CHANGES",
    "CommentRequest",
    "CommentRow",
    "ReviewRequest",
    "admin",
    "approved_by",
    "archive",
    "comment",
    "comments",
    "resolve",
    "review",
    "review_use_case",
    "role_for",
    "submit",
    "submit_use_case",
    "withdraw",
]
