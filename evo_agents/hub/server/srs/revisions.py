"""The revisions of each use case of a document, and its approved baseline (srs_use_case_revisions and
srs_use_case_releases, migration 0226).

- A use case has revisions r1 and up. ``record`` writes one, in the transaction of the save, each time a version changes
  how the use case reads (``fingerprints``: its title, code, fields and body, and the business rules under it with
  theirs; not its place), and one with no fingerprint when a version drops it. Its author is the version's.
- ``history`` reads a document's revisions and releases at once: the revision of a use case at a version (``at``), its
  newest release (``release``: the document's approved baseline holds the use case at that revision, or no longer
  holds it when the release is a removal), and who wrote a revision of it since that release (``authors``), who may not
  review it.
- ``release`` keeps an approved revision as the use case's newest release, never one older than the release it has.
- A writer sends a revision to review on its own (``submit``), and a request for changes takes it back (``unsubmit``);
  @ba's review of that round is kept on the revision (``review_run``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.srs.doc import Document

USE_CASE, BUSINESS_RULE = "use_case", "business_rule"
ADDED, CHANGED, REMOVED = "added", "changed", "removed"  # tables_srs.RELEASE_CHANGES


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


def hashed(text: str) -> str:
    """The SHA-256 of a use case's reading, as srs_use_case_revisions.fingerprint holds it."""
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Revision:
    """A revision of a use case: ``version`` is the number of the version it first appears in, ``fingerprint`` None
    for the revision of the version that dropped it."""

    id: int
    item_id: int
    key: str
    code: str | None
    rev: int
    version: int
    fingerprint: str | None
    author_id: int
    author: str
    created_at: datetime
    submitted_at: datetime | None
    submitted_by: str | None
    round: int
    review_run_id: int | None

    @property
    def dropped(self) -> bool:
        return self.fingerprint is None


@dataclass(frozen=True)
class Release:
    """An approved revision of a use case: ``version`` is the number of the version its approval was given on."""

    id: int
    item_id: int
    key: str
    rev: int
    version: int
    change: str
    released_by: str | None
    released_at: datetime


@dataclass
class History:
    """The revisions of a document's use cases by key, oldest first, and the newest release of each."""

    revisions: dict[str, list[Revision]] = field(default_factory=dict)
    releases: dict[str, Release] = field(default_factory=dict)

    def at(self, key: str, number: int) -> Revision | None:
        """The revision of use case ``key`` in version ``number``: its newest one that version or an older one has."""
        found = None
        for revision in self.revisions.get(key, ()):
            if revision.version > number:
                break
            found = revision
        return found

    def latest(self, key: str) -> Revision | None:
        listed = self.revisions.get(key)
        return listed[-1] if listed else None

    def release(self, key: str) -> Release | None:
        return self.releases.get(key)

    def baseline(self, key: str) -> int | None:
        """The revision of use case ``key`` the approved baseline holds; None when it holds none."""
        found = self.releases.get(key)
        return found.rev if found is not None and found.change != REMOVED else None

    def since_release(self, key: str) -> list[Revision]:
        """The revisions of use case ``key`` after its newest release (every one when it has none)."""
        found = self.releases.get(key)
        since = found.rev if found is not None else 0
        return [revision for revision in self.revisions.get(key, ()) if revision.rev > since]

    def authors(self, key: str) -> set[int]:
        """Who wrote a revision of use case ``key`` since its newest release (every revision when it has none)."""
        return {revision.author_id for revision in self.since_release(key)}

    def change(self, key: str, revision: Revision) -> str:
        """What releasing ``revision`` of use case ``key`` does to the baseline: adds it, changes it or removes it."""
        if revision.dropped:
            return REMOVED
        return CHANGED if self.baseline(key) is not None else ADDED


async def history(conn: AsyncConnection, document_id: int) -> History:
    """The revisions and releases of the use cases of document ``document_id``."""
    s, u = tables.srs, tables.users
    r, v, i = s.use_case_revisions, s.versions, s.items
    submitter = u.alias("submitter")
    query = (
        select(
            r.c.id,
            r.c.item_id,
            i.c.key,
            i.c.code,
            r.c.rev,
            v.c.number.label("version"),
            r.c.fingerprint,
            r.c.author_id,
            u.c.login.label("author"),
            r.c.created_at,
            r.c.submitted_at,
            submitter.c.login.label("submitted_by"),
            r.c.round,
            r.c.review_run_id,
        )
        .select_from(
            r.join(v, v.c.id == r.c.version_id)
            .join(i, i.c.id == r.c.item_id)
            .join(u, u.c.id == r.c.author_id)
            .outerjoin(submitter, submitter.c.id == r.c.submitted_by)
        )
        .where(r.c.document_id == document_id)
        .order_by(r.c.item_id, r.c.rev)
    )
    found = History()
    for row in await conn.execute(query):
        found.revisions.setdefault(row.key, []).append(Revision(**row._mapping))
    e = s.use_case_releases
    newest = (
        select(
            e.c.id,
            e.c.item_id,
            i.c.key,
            e.c.rev,
            v.c.number.label("version"),
            e.c.change,
            u.c.login.label("released_by"),
            e.c.released_at,
        )
        .select_from(
            e.join(v, v.c.id == e.c.version_id).join(i, i.c.id == e.c.item_id).outerjoin(u, u.c.id == e.c.released_by)
        )
        .where(e.c.document_id == document_id)
        .order_by(e.c.item_id, e.c.id.desc())
        .distinct(e.c.item_id)
    )
    for row in await conn.execute(newest):
        found.releases[row.key] = Release(**row._mapping)
    return found


async def record(
    conn: AsyncConnection,
    document_id: int,
    version_id: int,
    author_id: int,
    doc: Document,
    ids: dict[str, int],
) -> list[tuple[int, int]]:
    """Write the revisions version ``version_id`` of document ``document_id`` gives its use cases (``doc`` is its
    content, ``ids`` the id of each item by key): a use case it adds, or whose reading changed since its newest
    revision, takes the next one, and one it drops takes a last one with no fingerprint. (item id, rev) of each."""
    r = tables.srs.use_case_revisions
    query = (
        select(r.c.item_id, r.c.rev, r.c.fingerprint)
        .where(r.c.document_id == document_id)
        .order_by(r.c.item_id, r.c.rev.desc())
        .distinct(r.c.item_id)
    )
    latest = {row.item_id: (row.rev, row.fingerprint) for row in await conn.execute(query)}
    now = {ids[key]: hashed(text) for key, text in fingerprints(doc).items() if key in ids}
    rows = []
    for item_id, print_ in now.items():
        rev, before = latest.get(item_id, (0, None))
        if rev == 0 or before != print_:
            rows.append((item_id, rev + 1, print_))
    for item_id, (rev, before) in latest.items():
        if before is not None and item_id not in now:
            rows.append((item_id, rev + 1, None))
    if rows:
        values = [
            {
                "document_id": document_id,
                "item_id": item_id,
                "rev": rev,
                "version_id": version_id,
                "fingerprint": print_,
                "author_id": author_id,
            }
            for item_id, rev, print_ in rows
        ]
        await conn.execute(insert(r).values(values))
    return [(item_id, rev) for item_id, rev, _ in rows]


async def release(
    conn: AsyncConnection,
    document_id: int,
    revision: Revision,
    version_id: int,
    change: str,
    user_id: int | None,
) -> int | None:
    """Keep ``revision``, approved on version ``version_id``, as the newest release of its use case, unless the use
    case has a release as new or newer; the release's id when one was written."""
    e = tables.srs.use_case_releases
    held = await conn.scalar(select(func.max(e.c.rev)).where(e.c.item_id == revision.item_id))
    if held is not None and held >= revision.rev:
        return None
    values = {
        "document_id": document_id,
        "item_id": revision.item_id,
        "rev": revision.rev,
        "version_id": version_id,
        "change": change,
        "released_by": user_id,
    }
    query = pg_insert(e).values(values).on_conflict_do_nothing(index_elements=[e.c.item_id, e.c.rev])
    return await conn.scalar(query.returning(e.c.id))


async def submit(conn: AsyncConnection, revision: Revision, user_id: int) -> None:
    """Send ``revision`` to review on its own, in its next round, with no review of @ba yet."""
    r = tables.srs.use_case_revisions
    values = {"submitted_at": func.now(), "submitted_by": user_id, "round": r.c.round + 1, "review_run_id": None}
    await conn.execute(update(r).where(r.c.id == revision.id).values(values))


async def unsubmit(conn: AsyncConnection, revision: Revision) -> None:
    """Take ``revision`` out of review: a request for changes sends it back, as it sends a version back to draft."""
    r = tables.srs.use_case_revisions
    await conn.execute(update(r).where(r.c.id == revision.id).values(submitted_at=None))


async def review_run(conn: AsyncConnection, revision: Revision, run_id: int) -> None:
    """Keep ``run_id`` as @ba's review of the current round of ``revision``."""
    r = tables.srs.use_case_revisions
    await conn.execute(update(r).where(r.c.id == revision.id).values(review_run_id=run_id))


@dataclass(frozen=True)
class Reviewed:
    """The revision whose round's review by @ba is a run: its document, use case and number."""

    document_id: int
    key: str
    rev: int


async def reviewed_by(conn: AsyncConnection, run_id: int) -> Reviewed | None:
    """The revision whose current round @ba reviews in run ``run_id``, or None."""
    s = tables.srs
    r, i = s.use_case_revisions, s.items
    query = (
        select(r.c.document_id, i.c.key, r.c.rev)
        .join_from(r, i, i.c.id == r.c.item_id)
        .where(r.c.review_run_id == run_id, r.c.submitted_at.is_not(None))
    )
    row = (await conn.execute(query)).first()
    return None if row is None else Reviewed(**row._mapping)


async def title_in(conn: AsyncConnection, item_id: int, version: int) -> str | None:
    """The title use case ``item_id`` has in the newest version up to number ``version`` that holds it."""
    s = tables.srs
    iv, v = s.item_versions, s.versions
    query = (
        select(iv.c.title)
        .join_from(iv, v, v.c.id == iv.c.version_id)
        .where(iv.c.item_id == item_id, v.c.number <= version)
        .order_by(v.c.number.desc())
        .limit(1)
    )
    return await conn.scalar(query)


__all__ = [
    "ADDED",
    "CHANGED",
    "REMOVED",
    "History",
    "Release",
    "Revision",
    "Reviewed",
    "fingerprints",
    "hashed",
    "history",
    "record",
    "release",
    "review_run",
    "reviewed_by",
    "submit",
    "title_in",
    "unsubmit",
]
