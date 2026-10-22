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
- ``review_use_case``: the same writer approves or asks for changes on one use case of a version in review (plan
  srs-use-case-review, ``use_case_reviews``), under the same conditions as the version's review, except that a later
  review of theirs replaces the earlier one. An approval goes through the same gate; a request for changes needs a
  note, is never held, and leaves the version in review, where it blocks every approval (a version-wide one is still
  recorded). The version is approved by the review that makes every use case it adds, changes or drops since the
  version approved last approved by the policy (a version-wide approval counts for each use case, for its role), and
  none of them changes_requested; a version with no such use case is approved by version-wide reviews alone, as before.
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

from evo_agents.core_plugins.srs import ROLES, checks, events, findings, store, use_case_reviews
from evo_agents.core_plugins.srs.doc import SrsError
from evo_agents.core_plugins.srs.store import DocumentRow, ReviewRow, Settings, VersionRow
from evo_agents.core_plugins.srs.use_case_reviews import APPROVE, REQUEST_CHANGES, CaseReview, CaseState, approved_by
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal

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
    if user.scope is not None:
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
    if request.decision == REQUEST_CHANGES:
        await conn.execute(update(s.versions).where(s.versions.c.id == held.id).values(state="draft"))
    else:
        mine = ReviewRow(user.login, user.user_id, role, APPROVE, request.body, held.round, held.created_at)
        if await _approves(conn, access, doc, held, settings, [*approvals, mine]):
            await _approve(conn, doc, held)
            await events.version_approved(conn, access.project_id, doc, held, doc.approved_version, user)
    return await store.version(conn, doc, number)


async def _approves(
    conn: AsyncConnection,
    access: ProjectAccess,
    doc: DocumentRow,
    held: VersionRow,
    settings: Settings,
    approvals: list[ReviewRow],
) -> bool:
    """Whether the version-wide ``approvals`` of the round, with the reviews of use cases that count, approve ``held``:
    every use case to review approved, or the policy satisfied by ``approvals`` when it has none."""
    doc_now = (await store.snapshot(conn, held)).doc
    cases = await use_case_reviews.states(conn, access.project_id, doc, held, doc_now, settings, approvals)
    if cases:
        return use_case_reviews.ready(cases)
    return approved_by(settings.policy.get(doc.kind), approvals)


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
    await _may_review(conn, access, user, doc, held, settings)
    wide = use_case_reviews.round_approvals(held)
    before = await use_case_reviews.states(
        conn, access.project_id, doc, held, (await store.snapshot(conn, held)).doc, settings, wide
    )
    item_id = await store.item_id(conn, access, doc, ref)
    key = await use_case_reviews.key_of(conn, item_id)
    case = next((case for case in before if case.key == key), None)
    if case is None:
        named = ", ".join(c.code or c.key for c in before) or "none"
        raise SrsError(
            422,
            f"{ref} is not a use case version {number} of {doc.slug} needs reviewed: a use case it adds, changes or "
            f"drops since the version approved last is, and these are: {named}",
        )
    if request.decision == REQUEST_CHANGES and not request.body.strip():
        raise SrsError(422, "a request for changes on a use case needs a note saying what to change")
    if request.decision == APPROVE:
        await findings.approval_hold(conn, access, doc, held)
    others = [r for r in case.reviews if r.reviewer_id != user.user_id and r.decision == APPROVE]
    role = role_for(settings, user.login, request.role, doc.kind, [*others, *wide])
    await use_case_reviews.record(conn, held, item_id, user.user_id, role, request.decision, request.body)
    doc_now = (await store.snapshot(conn, held)).doc
    after = await use_case_reviews.states(conn, access.project_id, doc, held, doc_now, settings, wide)
    if request.decision == APPROVE and use_case_reviews.ready(after):
        await _approve(conn, doc, held)
        await events.version_approved(conn, access.project_id, doc, held, doc.approved_version, user)
    return await store.version(conn, doc, number), after


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
    "withdraw",
]
