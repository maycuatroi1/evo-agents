"""The review of single use cases of a version in review (plan srs-use-case-review).

A version that goes to review has *use cases to review*: those it adds, those it changes (the use case itself, or a
business rule under it added, changed or dropped) and those it drops, compared with the version approved last; when no
version of the document was approved yet, every use case. A use case that only moved needs no review, and nothing else
of the document (section, screen, NFR, CBR, glossary, approval page, revision history) is reviewed on its own.

A writer approves or asks for changes on one use case (``srs_use_case_reviews``), as a reviewer of the version would
(``reviews``). The reviews that *count* for a version V of a use case are:

- those recorded on a version M after the one approved last (M up to V) while the use case reads at V as it did at M:
  its title, code, fields and body, and the business rules under it with theirs, the order of places not counted;
- of a reviewer, the latest one, which replaces their earlier ones (all stay in the table);
- of a reviewer who may still review V: a writer of the project who wrote no version since the one approved last, unless
  the config's ``self_approval`` is on.

A use case is ``changes_requested`` while one counted review asks for changes; else ``approved`` when its counted
approvals and the version-wide approvals of the round (each counts for every use case, for its role) satisfy the
config's ``approval_policy`` of the document's kind (``approved_by``); else ``pending``. The version is approved by the
review that makes every use case to review ``approved``; a version with no use case to review is approved by
version-wide reviews alone. Nothing here writes a version's state: ``reviews`` does.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.srs import store
from evo_agents.core_plugins.srs.doc import Document
from evo_agents.core_plugins.srs.store import DocumentRow, ReviewRow, Settings, VersionRow
from evo_agents.hub import tables
from evo_agents.hub.access import has_role

APPROVE, REQUEST_CHANGES = "approve", "request_changes"
USE_CASE, BUSINESS_RULE = "use_case", "business_rule"
ADDED, CHANGED, REMOVED = "added", "changed", "removed"
PENDING, APPROVED, CHANGES_REQUESTED = "pending", "approved", "changes_requested"
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
    """A use case to review, its state and the reviews that count."""

    key: str
    code: str | None
    title: str
    change: str
    state: str
    reviews: list[CaseReview]


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


def _own(item) -> list:
    return [item.title, item.code, item.body, item.fields]


def fingerprints(doc: Document) -> dict[str, str]:
    """A text for each use case of ``doc`` by key that two versions share when the use case reads the same in both."""
    rules: dict[str, list] = {}
    for at, item in enumerate(doc.items):
        if item.kind == BUSINESS_RULE:
            above = next((doc.items[up].key for up in doc.ancestors(at) if doc.items[up].kind == USE_CASE), None)
            if above is not None:
                rules.setdefault(above, []).append([item.key, _own(item)])
    return {
        item.key: json.dumps([_own(item), sorted(rules.get(item.key, []))], sort_keys=True, default=str)
        for item in doc.items
        if item.kind == USE_CASE and item.key
    }


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


async def _fingerprints_at(conn: AsyncConnection, doc: DocumentRow, held: VersionRow, new: Document, numbers: set[int]):
    found = {held.number: fingerprints(new)}
    for number in numbers - {held.number}:
        found[number] = fingerprints((await store.snapshot(conn, await store.version(conn, doc, number))).doc)
    return found


def _state(policy: dict | None, counted: Sequence[CaseReview], version_wide: Sequence[ReviewRow]) -> str:
    if any(review.decision == REQUEST_CHANGES for review in counted):
        return CHANGES_REQUESTED
    approvals = [review for review in counted if review.decision == APPROVE]
    return APPROVED if approved_by(policy, [*approvals, *version_wide]) else PENDING


async def states(
    conn: AsyncConnection,
    project_id: int,
    doc: DocumentRow,
    held: VersionRow,
    new: Document,
    settings: Settings,
    version_wide: Sequence[ReviewRow],
) -> list[CaseState]:
    """The use cases version ``held`` of ``doc`` (whose content is ``new``) needs reviewed, each with its state and the
    reviews that count; ``version_wide`` are the version-wide approvals that count for each. Empty for a version that
    is neither a draft nor in review, or has no use case to review."""
    if held.state not in REVIEWED_STATES or held.number <= (doc.approved_version or 0):
        return []
    old = (
        (await store.snapshot(conn, await store.version(conn, doc, doc.approved_version))).doc
        if doc.approved_version
        else None
    )
    cases = needed(old, new)
    if not cases:
        return []
    rows = await _rows(conn, doc, held.number)
    blocked = set() if settings.self_approval else await authors_since(conn, doc, held.number)
    eligible = await _writers(conn, project_id, {review.reviewer_id for _, review in rows}) - blocked
    at = await _fingerprints_at(conn, doc, held, new, {review.version for _, review in rows})
    latest = {(key, review.reviewer_id): review for key, review in rows}  # a later review replaces the earlier one
    policy = settings.policy.get(doc.kind)
    found = []
    for case in cases:
        counted = sorted(
            (
                review
                for (key, _), review in latest.items()
                if key == case.key
                and review.reviewer_id in eligible
                and at[review.version].get(key) == at[held.number].get(key)
            ),
            key=lambda review: (review.created_at, review.version),
        )
        found.append(CaseState(**case.__dict__, state=_state(policy, counted, version_wide), reviews=counted))
    return found


def ready(cases: Sequence[CaseState]) -> bool:
    """Whether every use case to review is approved (none is, when there is none)."""
    return bool(cases) and all(case.state == APPROVED for case in cases)


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
    "PENDING",
    "REMOVED",
    "REQUEST_CHANGES",
    "Case",
    "CaseReview",
    "CaseState",
    "approved_by",
    "authors_since",
    "fingerprints",
    "key_of",
    "needed",
    "ready",
    "record",
    "round_approvals",
    "states",
]
