"""The review of single use cases of a version in review (plan srs-use-case-review), and of a use case sent to review on
its own.

A version that goes to review has *use cases to review*: those it adds, those it changes (the use case itself, or a
business rule under it added, changed or dropped) and those it drops, compared with the version approved last; when no
version of the document was approved yet, every use case. A use case the approved baseline holds and the version
dropped is one too (``revisions``). A use case that only moved needs no review, and nothing else of the document
(section, screen, NFR, CBR, glossary, approval page, revision history) is reviewed on its own.

Each use case has revisions, r1 and up (``revisions``): a review is of the revision the use case has in the version it
was recorded on. A writer approves or asks for changes on one use case (``srs_use_case_reviews``), as a reviewer of the
version would (``reviews``). The reviews that *count* for a version V of a use case are:

- those recorded on a version M after the one approved last (M up to V) where the use case has the revision it has at
  V;
- of a reviewer, the latest one, which replaces their earlier ones (all stay in the table);
- of a reviewer who may still review that use case: a writer of the project who wrote no revision of it since its newest
  release (``revisions.History.authors``), unless the config's ``self_approval`` is on. Writing a revision of one use
  case bars nobody from the others.

A use case is ``approved`` when the approved baseline holds it at the revision it has at V (or holds a removal of it,
for one V drops), ``superseded`` when the baseline holds a newer revision of it, else ``changes_requested`` while one
counted review asks for changes, else ``approved`` when its counted approvals and the version-wide approvals of the
round (each counts for every use case, for its role) satisfy the config's ``approval_policy`` of the document's kind
(``approved_by``), else ``pending``. A review that makes a use case approved releases its revision into the baseline
(``events.use_cases_released``). The version is approved by the review that makes every use case to review approved or
superseded, while nothing of the whole document holds it; a version with no use case to review is approved by
version-wide reviews alone. Nothing here writes a version's state: ``reviews`` does.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.srs import revisions, store
from evo_agents.hub.server.srs.doc import Document
from evo_agents.hub.server.srs.revisions import History, Revision, fingerprints
from evo_agents.hub.server.srs.store import DocumentRow, ReviewRow, Settings, VersionRow

APPROVE, REQUEST_CHANGES = "approve", "request_changes"
USE_CASE, BUSINESS_RULE = "use_case", "business_rule"
ADDED, CHANGED, REMOVED = "added", "changed", "removed"
PENDING, APPROVED, CHANGES_REQUESTED, SUPERSEDED = "pending", "approved", "changes_requested", "superseded"
DONE = (APPROVED, SUPERSEDED)  # the states of a use case that no longer holds its version
REVIEWED_STATES = ("draft", "in_review")  # a version in another state lists no use case to review


@dataclass(frozen=True)
class CaseReview:
    """One review of a use case that counts, with the number of the version it was recorded on."""

    reviewer: str
    reviewer_id: int
    role: str | None
    decision: str
    body: str
    version: int
    created_at: datetime


@dataclass(frozen=True)
class Case:
    """A use case a version needs reviewed, and why."""

    key: str
    code: str | None
    title: str
    change: str  # added, changed, removed


@dataclass(frozen=True)
class CaseState:
    """A use case to review, its state and the reviews that count; its revision in the version (``rev``), the one the
    approved baseline holds (``released``), whether the revision was sent to review on its own (``submitted``) with
    @ba's review of that round, the revision's author, and who may not review it (``authors``, never answered)."""

    key: str
    code: str | None
    title: str
    change: str
    state: str
    reviews: list[CaseReview]
    rev: int | None = None
    released: int | None = None
    submitted: bool = False
    review_run_id: int | None = None
    round: int = 0
    author: str | None = None
    author_id: int | None = None
    authors: frozenset[int] = frozenset()
    author_logins: tuple[str, ...] = ()


def case_name(case: Case | CaseState) -> str:
    """A use case and its revision as members read them: ``UC ORD-1 r3``."""
    name = case.code or case.title
    rev = getattr(case, "rev", None)
    return f"{name} r{rev}" if rev else name


def approved_by(policy: dict | None, approvals: Iterable[ReviewRow | CaseReview]) -> bool:
    """Whether ``approvals`` approve under ``policy`` (that of the document's kind): each role it requires approved,
    or one approval when it requires none."""
    approvals = list(approvals)
    required = (policy or {}).get("required") or []
    if not required:
        return bool(approvals)
    return set(required) <= {review.role for review in approvals}


def round_approvals(held: VersionRow) -> list[ReviewRow]:
    """The version-wide approvals of the current round of ``held``, which count for every use case of it."""
    if held.state != "in_review":
        return []
    return [r for r in held.reviews if r.round == held.round and r.decision == APPROVE]


# What a version changes


def needed(old: Document | None, new: Document) -> list[Case]:
    """The use cases of ``new`` to review against ``old``, the version approved last (None: none yet): those of ``new``
    in its order, then those ``new`` dropped in the order of ``old``."""
    now = fingerprints(new)
    before = fingerprints(old) if old is not None else {}
    found = []
    for item in new.items:
        if item.kind != USE_CASE or not item.key:
            continue
        if item.key not in before:
            found.append(Case(item.key, item.code, item.title, ADDED))
        elif before[item.key] != now[item.key]:
            found.append(Case(item.key, item.code, item.title, CHANGED))
    for item in old.items if old is not None else []:
        if item.kind == USE_CASE and item.key and item.key not in now:
            found.append(Case(item.key, item.code, item.title, REMOVED))
    return found


# What was reviewed


async def authors_since(conn: AsyncConnection, doc: DocumentRow, number: int) -> set[int]:
    """The authors of the versions of ``doc`` after the one approved last, up to ``number``."""
    v = tables.srs.versions
    since = doc.approved_version or 0
    query = select(v.c.author_id).where(v.c.document_id == doc.id, v.c.number > since, v.c.number <= number)
    return set((await conn.execute(query)).scalars())


async def _approved_before(conn: AsyncConnection, doc: DocumentRow, held: VersionRow) -> int | None:
    """The version approved before ``held`` when it was approved, as its event ``srs.version.approved`` says; None
    when none was, or the event is not there."""
    e = tables.hub_events
    query = (
        select(e.c.payload["previous"].astext)
        .where(
            e.c.kind == "srs.version.approved",
            e.c.payload["document_id"].astext == str(doc.id),
            e.c.payload["version"].astext == str(held.number),
        )
        .order_by(e.c.id.desc())
        .limit(1)
    )
    found = (await conn.execute(query)).scalar()
    return int(found) if found and found.isdigit() else None


async def _rows(conn: AsyncConnection, doc: DocumentRow, number: int) -> list[tuple[str, CaseReview]]:
    """(key of the use case, review) recorded on the versions after the one approved last, up to ``number``, oldest
    first."""
    s = tables.srs
    r, v, i, u = s.use_case_reviews, s.versions, s.items, tables.users
    query = (
        select(
            i.c.key,
            u.c.login.label("reviewer"),
            r.c.reviewer_id,
            r.c.role,
            r.c.decision,
            r.c.body,
            v.c.number.label("version"),
            r.c.created_at,
        )
        .select_from(
            r.join(v, v.c.id == r.c.version_id).join(i, i.c.id == r.c.item_id).join(u, u.c.id == r.c.reviewer_id)
        )
        .where(v.c.document_id == doc.id, v.c.number > (doc.approved_version or 0), v.c.number <= number)
        .order_by(r.c.id)
    )
    return [
        (row.key, CaseReview(**{k: val for k, val in row._mapping.items() if k != "key"}))
        for row in await conn.execute(query)
    ]


async def _writers(conn: AsyncConnection, project_id: int, ids: set[int]) -> set[int]:
    g = tables.grants
    query = select(g.c.user_id, g.c.role).where(g.c.project_id == project_id, g.c.user_id.in_(ids or [0]))
    return {row.user_id for row in await conn.execute(query) if has_role(row.role, "writer")}


def _same(one: Revision | None, other: Revision | None) -> bool:
    return one is not None and other is not None and one.rev == other.rev


def _state(policy: dict | None, counted: Sequence[CaseReview], version_wide: Sequence[ReviewRow]) -> str:
    if any(review.decision == REQUEST_CHANGES for review in counted):
        return CHANGES_REQUESTED
    approvals = [review for review in counted if review.decision == APPROVE]
    return APPROVED if approved_by(policy, [*approvals, *version_wide]) else PENDING


def _case_state(known: History, key: str, revision: Revision | None, reviewed: str) -> str:
    """``reviewed`` (what the counted reviews make of the use case), unless the baseline decides: approved when it
    holds this revision, superseded when it holds a newer one."""
    release = known.release(key)
    if release is not None and revision is not None:
        if release.rev == revision.rev:
            return APPROVED
        if release.rev > revision.rev:
            return SUPERSEDED
    return reviewed


async def _dropped_since_release(
    conn: AsyncConnection, known: History, old: Document | None, new: Document, number: int
) -> list[Case]:
    """The use cases the approved baseline holds that version ``number`` (``new``) dropped, and the version approved
    last (``old``) does not hold either: removals to review, which ``needed`` cannot see."""
    have = {item.key for item in new.items} | ({item.key for item in old.items} if old is not None else set())
    found = []
    for key in known.releases:
        revision = known.at(key, number)
        if key in have or known.baseline(key) is None or revision is None or not revision.dropped:
            continue
        title = await revisions.title_in(conn, revision.item_id, number) or revision.code or key
        found.append(Case(key, revision.code, title, REMOVED))
    return found


async def states(
    conn: AsyncConnection,
    project_id: int,
    doc: DocumentRow,
    held: VersionRow,
    new: Document,
    settings: Settings,
    version_wide: Sequence[ReviewRow],
    known: History | None = None,
) -> list[CaseState]:
    """The use cases version ``held`` of ``doc`` (whose content is ``new``) needs reviewed, each with its state, its
    revisions and the reviews that count; ``version_wide`` are the version-wide approvals that count for each, ``known``
    the document's revisions when the caller read them. Empty for a version that is neither a draft, in review nor the
    version approved last, or has no use case to review; the last one is read against the version approved before
    it."""
    if held.state == "approved" and held.number == doc.approved_version:
        doc = replace(doc, approved_version=await _approved_before(conn, doc, held))  # what it was approved against
    elif held.state not in REVIEWED_STATES or held.number <= (doc.approved_version or 0):
        return []
    old = (
        (await store.snapshot(conn, await store.version(conn, doc, doc.approved_version))).doc
        if doc.approved_version
        else None
    )
    known = known or await revisions.history(conn, doc.id)
    cases = needed(old, new) + await _dropped_since_release(conn, known, old, new, held.number)
    if not cases:
        return []
    rows = await _rows(conn, doc, held.number)
    writers = await _writers(conn, project_id, {review.reviewer_id for _, review in rows})
    latest = {(key, review.reviewer_id): review for key, review in rows}  # a later review replaces the earlier one
    policy = settings.policy.get(doc.kind)
    found = []
    for case in cases:
        revision = known.at(case.key, held.number)
        barred = frozenset() if settings.self_approval else frozenset(known.authors(case.key))
        counted = sorted(
            (
                review
                for (key, reviewer_id), review in latest.items()
                if key == case.key
                and reviewer_id in writers
                and reviewer_id not in barred
                and _same(known.at(key, review.version), revision)
            ),
            key=lambda review: (review.created_at, review.version),
        )
        reviewed = _state(policy, counted, version_wide)
        found.append(
            CaseState(
                **case.__dict__,
                state=_case_state(known, case.key, revision, reviewed),
                reviews=counted,
                rev=revision.rev if revision else None,
                released=known.baseline(case.key),
                submitted=revision is not None and revision.submitted_at is not None,
                review_run_id=revision.review_run_id if revision else None,
                round=revision.round if revision else 0,
                author=revision.author if revision else None,
                author_id=revision.author_id if revision else None,
                authors=barred,
                author_logins=()
                if settings.self_approval
                else tuple(dict.fromkeys(revision.author for revision in known.since_release(case.key))),
            )
        )
    return found


def ready(cases: Sequence[CaseState]) -> bool:
    """Whether every use case to review is approved, or superseded by a newer approved revision (none is, when there is
    none)."""
    return bool(cases) and all(case.state in DONE for case in cases)


async def key_of(conn: AsyncConnection, item_id: int) -> str:
    i = tables.srs.items
    return (await conn.execute(select(i.c.key).where(i.c.id == item_id))).scalar_one()


async def record(
    conn: AsyncConnection, held: VersionRow, item_id: int, reviewer_id: int, role: str | None, decision: str, body: str
) -> None:
    """Keep a review of use case ``item_id`` on version ``held``."""
    values = {
        "version_id": held.id,
        "item_id": item_id,
        "reviewer_id": reviewer_id,
        "role": role,
        "decision": decision,
        "body": body,
    }
    await conn.execute(insert(tables.srs.use_case_reviews).values(values))


__all__ = [
    "ADDED",
    "APPROVE",
    "APPROVED",
    "CHANGED",
    "CHANGES_REQUESTED",
    "DONE",
    "PENDING",
    "REMOVED",
    "REQUEST_CHANGES",
    "SUPERSEDED",
    "Case",
    "CaseReview",
    "CaseState",
    "approved_by",
    "authors_since",
    "case_name",
    "fingerprints",
    "key_of",
    "needed",
    "ready",
    "record",
    "round_approvals",
    "states",
]
