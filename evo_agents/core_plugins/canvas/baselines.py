"""Baselines of a canvas (criterion a6 of plan living-lean-canvas): what members approve so that a claim's status takes
effect for the Loop, and the snapshot each approval leaves.

The status in effect. A claim's status changes at once when a member edits it, but takes effect only through an
approved baseline: its status in the latest approved snapshot of its canvas (``effective``), assumption for a claim no
snapshot holds yet.

What a baseline asks (``items_for``). One item per claim not archived, the first of these that holds:

- ``value_check``: an open proposal of the value check (the hub's, naming its evidence) to move the claim to a status;
- ``status``: members moved the claim's status away from the status in effect;
- ``overdue``: an assumption or a claim in testing whose test card's deadline passed (in UTC) with no real evidence,
  which a reviewer extends with a new deadline or retires;
- ``new``: a claim no snapshot holds, at assumption, which joins the snapshot as it is, decided by no one.

Each item keeps the status in effect (``from``), the status the claim was at (``at``), the one asked (``to``), the
claim's rev and evidence, and who may not decide it (``proposers``): every member who wrote a rev that changed the
claim's status since the status in effect (a rev an earlier baseline's decision wrote aside), and the writer who opened
it at once. What the value check or an overdue card asks, the hub proposed: any other writer decides it. With the
config's ``self_approval`` on (read by ``self_approval``), the proposers are kept as they are but bar nobody
(``ItemRow.barred``): any writer decides the item, a proposer too.

A baseline is proposed by the plugin's job each week (``schedule``), or opened at once by a writer for one claim
(``open_now``); the job's proposal expires every proposal before it that is still open, whose undecided claims it asks
again. Once every item has a decision, the baseline is approved (``approve``): its snapshot holds each claim not
archived with its code, rev, the status in effect and the SHA-256 of its content, and is never changed; the event
``canvas.baseline`` names every change of status that took effect. ``decisions`` decides an item. The caller holds the
transaction and the lock of the canvas.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.canvas import MANIFEST, events, rules, store
from evo_agents.core_plugins.canvas.content import CanvasError, code
from evo_agents.core_plugins.canvas.store import CanvasRow, ClaimRow, EvidenceRow
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.plugins import config as plugin_config

DEFAULT_STATUS = "assumption"  # in effect for a claim no snapshot holds yet
OVERDUE_STATUSES = ("assumption", "testing")  # what a past deadline without real evidence asks a reviewer about
ASKED = ("status", "value_check")  # items that ask for a status
DECIDES = {  # kind of item -> what a member decides of it
    "status": ("accepted", "rejected", "deferred"),
    "value_check": ("accepted", "rejected", "deferred"),
    "overdue": ("extended", "retired", "deferred"),
    "new": (),
}
MAX_LISTED = 100  # baselines one read lists
PLUGIN = MANIFEST["id"]  # acting.PLUGIN, not imported here: it loads the server's agent types
SELF_APPROVAL = "self_approval"  # the config's field that lets a proposer decide the item too


@dataclass(frozen=True)
class BaselineRow:
    id: int
    canvas_id: int
    number: int
    kind: str
    week: str | None
    due_at: datetime | None
    time_zone: str | None
    status: str
    label: dict
    created_by: str | None
    created_at: datetime
    approved_at: datetime | None
    expired_at: datetime | None
    snapshot: list | None


@dataclass(frozen=True)
class ItemRow:
    id: int
    claim_id: int
    number: int
    kind: str
    from_status: str
    at_status: str
    to_status: str | None
    claim_rev: int
    proposal_id: int | None
    deadline: date | None
    evidence_ids: list[int]
    proposers: list[int]
    label: dict
    decision: str | None
    decided_by: str | None
    decided_at: datetime | None
    new_deadline: date | None
    reason: str | None
    rev: int | None

    @property
    def code(self) -> str:
        return code(self.number)

    @property
    def waiting(self) -> bool:
        """Whether a member has still to decide it."""
        return self.decision is None and bool(DECIDES[self.kind])

    def barred(self, self_approval: bool) -> list[int]:
        """The members who may not decide it: its proposers, or nobody while the config's self_approval is on."""
        return [] if self_approval else list(self.proposers)


@dataclass(frozen=True)
class Item:
    """An item to propose: what it asks of ``claim``."""

    claim: ClaimRow
    kind: str
    from_status: str
    to_status: str | None = None
    proposal_id: int | None = None
    deadline: date | None = None
    evidence_ids: list[int] = field(default_factory=list)
    proposers: list[int] = field(default_factory=list)


# Reading


def _baseline_query():
    b, users = tables.canvas.baselines, tables.users
    return select(
        b.c.id,
        b.c.canvas_id,
        b.c.number,
        b.c.kind,
        b.c.week,
        b.c.due_at,
        b.c.time_zone,
        b.c.status,
        b.c.label,
        users.c.login.label("created_by"),
        b.c.created_at,
        b.c.approved_at,
        b.c.expired_at,
        b.c.snapshot,
    ).select_from(b.outerjoin(users, users.c.id == b.c.created_by))


async def baselines(conn: AsyncConnection, canvas: CanvasRow, limit: int = MAX_LISTED) -> list[BaselineRow]:
    """The baselines of ``canvas``, newest first."""
    b = tables.canvas.baselines
    query = _baseline_query().where(b.c.canvas_id == canvas.id).order_by(b.c.number.desc()).limit(limit)
    return [BaselineRow(**row._mapping) for row in await conn.execute(query)]


async def baseline(conn: AsyncConnection, canvas: CanvasRow, number: int, *, lock: bool = False) -> BaselineRow:
    """Baseline ``number`` of ``canvas``; 404 when it has none. ``lock`` takes its row lock."""
    b = tables.canvas.baselines
    where = (b.c.canvas_id == canvas.id) & (b.c.number == number)
    if lock:
        await conn.execute(select(b.c.id).where(where).with_for_update())
    row = (await conn.execute(_baseline_query().where(where))).first()
    if row is None:
        raise CanvasError(404, f"canvas {canvas.slug} has no baseline {number}")
    return BaselineRow(**row._mapping)


async def items(conn: AsyncConnection, baseline_id: int) -> list[ItemRow]:
    """The items of a baseline, by claim number."""
    i, k, users = tables.canvas.baseline_items, tables.canvas.claims, tables.users
    query = (
        select(
            i.c.id,
            i.c.claim_id,
            k.c.number,
            i.c.kind,
            i.c.from_status,
            i.c.at_status,
            i.c.to_status,
            i.c.claim_rev,
            i.c.proposal_id,
            i.c.deadline,
            i.c.evidence_ids,
            i.c.proposers,
            i.c.label,
            i.c.decision,
            users.c.login.label("decided_by"),
            i.c.decided_at,
            i.c.new_deadline,
            i.c.reason,
            i.c.rev,
        )
        .select_from(i.join(k, k.c.id == i.c.claim_id).outerjoin(users, users.c.id == i.c.decided_by))
        .where(i.c.baseline_id == baseline_id)
        .order_by(k.c.number)
    )
    return [ItemRow(**row._mapping) for row in await conn.execute(query)]


async def logins(conn: AsyncConnection, user_ids) -> dict[int, str]:
    users = tables.users
    found = await conn.execute(select(users.c.id, users.c.login).where(users.c.id.in_(sorted(set(user_ids)))))
    return dict(found.all())


async def effective(conn: AsyncConnection, canvas_id: int) -> dict[int, dict]:
    """claim id -> its entry in the latest approved snapshot of canvas ``canvas_id`` (status, rev); none before the
    first approval."""
    b = tables.canvas.baselines
    query = (
        select(b.c.snapshot)
        .where(b.c.canvas_id == canvas_id, b.c.status == "approved")
        .order_by(b.c.approved_at.desc(), b.c.id.desc())
        .limit(1)
    )
    snapshot = await conn.scalar(query)
    return {entry["claim_id"]: entry for entry in snapshot or []}


async def today(conn: AsyncConnection) -> date:
    """The day it is in UTC by Postgres's clock: a test card's deadline passes after it."""
    return (await conn.scalar(select(func.timezone("UTC", func.now())))).date()


async def config_value(conn: AsyncConnection, project_id: int, name: str):
    """The effective value of field ``name`` of the plugin's config in project ``project_id``, as its layers set it;
    None when canvas is not enabled there."""
    row = await enabled_in(conn, PLUGIN, project_id)
    if row is None:
        return None
    stacks = await stored_stacks(conn, [row.id], project_id, None)
    found = plugin_config.merge(row.manifest.get("config"), stacks.get(row.id, [])).fields.get(name)
    return None if found is None else found.value


async def self_approval(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the config's ``self_approval`` lets a proposer decide an item in project ``project_id``: off unless an
    admin of the project turned it on."""
    return await config_value(conn, project_id, SELF_APPROVAL) is True


# What a baseline asks


async def _value_checks(conn: AsyncConnection, canvas_id: int) -> dict[int, tuple[int, str]]:
    """claim id -> (the open proposal of the value check, the status it asks) of the claims of a canvas."""
    p = tables.canvas.proposals
    query = (
        select(p.c.claim_id, p.c.id, p.c.fields["status"].astext)
        .where(
            p.c.canvas_id == canvas_id,
            p.c.status == "open",
            p.c.actor == "hub",
            p.c.kind == "change",
            p.c.evidence_id.is_not(None),
            p.c.fields["status"].astext.is_not(None),
        )
        .order_by(p.c.id)
    )
    return {claim_id: (proposal_id, status) for claim_id, proposal_id, status in await conn.execute(query)}


async def proposers(conn: AsyncConnection, claim_id: int, since_rev: int) -> list[int]:
    """The members who wrote a rev of claim ``claim_id`` from ``since_rev`` on that changed its status, a rev a
    baseline's decision wrote aside."""
    r = tables.canvas.revs
    query = (
        select(r.c.rev, r.c.content["status"].astext, r.c.change, r.c.user_id)
        .where(r.c.claim_id == claim_id, r.c.rev >= max(1, since_rev - 1))
        .order_by(r.c.rev)
    )
    found, before = set(), DEFAULT_STATUS if since_rev <= 1 else None
    for rev, status, change, user_id in await conn.execute(query):
        moved = before is not None and status != before
        if rev >= since_rev and moved and change != "baseline" and user_id is not None:
            found.add(user_id)
        before = status
    return sorted(found)


def overdue(claim: ClaimRow, evidence: list[EvidenceRow], today: date) -> date | None:
    """The deadline of ``claim``'s test card when it passed with no real evidence of the claim; None otherwise."""
    deadline = (claim.test_card or {}).get("deadline")
    if claim.status not in OVERDUE_STATUSES or not deadline or date.fromisoformat(str(deadline)) >= today:
        return None
    if any(rules.not_real(piece.facts()) is None for piece in evidence):
        return None
    return date.fromisoformat(str(deadline))


async def _item(conn, claim: ClaimRow, held: dict | None, checked, evidence: list[EvidenceRow], today: date):
    """The item ``claim`` asks of a baseline, or None. ``held`` is its entry in the latest snapshot, ``checked`` the
    open proposal of the value check."""
    from_status = held["status"] if held else DEFAULT_STATUS
    pieces = [piece.id for piece in evidence if piece.withdrawn_at is None]
    found = {"claim": claim, "from_status": from_status, "evidence_ids": pieces}
    if checked is not None:
        found.update(kind="value_check", proposal_id=checked[0], to_status=checked[1])
    elif claim.status != from_status:
        found.update(kind="status", to_status=claim.status)
    elif (deadline := overdue(claim, evidence, today)) is not None:
        found.update(kind="overdue", deadline=deadline)
    elif held is None:
        found.update(kind="new", to_status=DEFAULT_STATUS)
    else:
        return None
    if found["kind"] in ASKED and claim.status != from_status:
        found["proposers"] = await proposers(conn, claim.id, held["rev"] + 1 if held else 1)
    return Item(**found)


async def items_for(conn: AsyncConnection, canvas: CanvasRow, today: date, only: ClaimRow | None = None) -> list[Item]:
    """What a baseline of ``canvas`` proposed now asks, of every claim not archived, or of ``only``."""
    held = await effective(conn, canvas.id)
    checked = await _value_checks(conn, canvas.id)
    claims = [only] if only is not None else await store.claims(conn, canvas.id)
    evidence = await store.evidence(conn, [claim.id for claim in claims])
    found = []
    for claim in claims:
        item = await _item(conn, claim, held.get(claim.id), checked.get(claim.id), evidence[claim.id], today)
        if item is not None:
            found.append(item)
    return found


# Proposing


async def _next_number(conn: AsyncConnection, canvas_id: int) -> int:
    b = tables.canvas.baselines
    return (await conn.scalar(select(func.max(b.c.number)).where(b.c.canvas_id == canvas_id)) or 0) + 1


async def expire_open(conn: AsyncConnection, canvas: CanvasRow) -> int:
    """Expire every baseline of ``canvas`` still proposed; how many."""
    b = tables.canvas.baselines
    moved = update(b).where(b.c.canvas_id == canvas.id, b.c.status == "proposed")
    return (await conn.execute(moved.values(status="expired", expired_at=func.now()))).rowcount


async def _insert_items(conn: AsyncConnection, baseline_id: int, canvas: CanvasRow, found: list[Item], opener):
    i = tables.canvas.baseline_items
    now = await conn.scalar(select(func.now()))  # one value for every row: a batch insert binds no SQL function
    rows = []
    for item in found:
        new = item.kind == "new"
        named = sorted({*item.proposers, *([opener] if opener is not None else [])})
        rows.append(
            {
                "baseline_id": baseline_id,
                "claim_id": item.claim.id,
                "kind": item.kind,
                "from_status": item.from_status,
                "at_status": item.claim.status,
                "to_status": item.to_status,
                "claim_rev": item.claim.rev,
                "proposal_id": item.proposal_id,
                "deadline": item.deadline,
                "evidence_ids": item.evidence_ids,
                "proposers": named,
                "label": canvas.label,
                "decision": "included" if new else None,
                "decided_at": now if new else None,
            }
        )
    if rows:
        await conn.execute(insert(i), rows)


async def propose(
    conn: AsyncConnection,
    project_id: int,
    canvas: CanvasRow,
    found: list[Item],
    *,
    week: str | None = None,
    due_at: datetime | None = None,
    time_zone: str | None = None,
    opener: int | None = None,
) -> BaselineRow:
    """A new baseline of ``canvas`` asking ``found``: weekly (``week``, ``due_at``, ``time_zone``) or opened at once
    by member ``opener``. Approved at once when no item waits for a member."""
    b = tables.canvas.baselines
    values = {
        "canvas_id": canvas.id,
        "number": await _next_number(conn, canvas.id),
        "kind": "push" if opener is not None else "weekly",
        "week": week,
        "due_at": due_at,
        "time_zone": time_zone,
        "label": canvas.label,
        "created_by": opener,
    }
    baseline_id = await conn.scalar(insert(b).values(**values).returning(b.c.id))
    await _insert_items(conn, baseline_id, canvas, found, opener)
    made = await baseline(conn, canvas, values["number"])
    if not any(item.waiting for item in await items(conn, baseline_id)):
        await approve(conn, project_id, canvas, made)
        made = await baseline(conn, canvas, values["number"])
    return made


async def waiting_in_open(conn: AsyncConnection, claim: ClaimRow) -> int | None:
    """The number of a baseline still proposed that holds an item of ``claim``, or None."""
    b, i = tables.canvas.baselines, tables.canvas.baseline_items
    query = (
        select(b.c.number)
        .join_from(i, b, b.c.id == i.c.baseline_id)
        .where(i.c.claim_id == claim.id, b.c.status == "proposed")
        .limit(1)
    )
    return await conn.scalar(query)


async def open_now(
    conn: AsyncConnection, project_id: int, canvas: CanvasRow, claim: ClaimRow, opener: int, today: date
) -> BaselineRow:
    """A baseline of ``claim`` alone, opened at once by member ``opener``, whom another writer approves; 409 when the
    claim asks nothing of a baseline or one still proposed holds it."""
    if claim.archived_at is not None:
        raise CanvasError(409, f"{claim.code} is archived: a baseline holds the claims that are not")
    held = await waiting_in_open(conn, claim)
    if held is not None:
        raise CanvasError(409, f"baseline {held} of canvas {canvas.slug} holds {claim.code} already: decide it there")
    found = [item for item in await items_for(conn, canvas, today, only=claim) if DECIDES[item.kind]]
    if not found:
        raise CanvasError(
            409, f"{claim.code} asks nothing of a baseline: its status is the one in effect and nothing is proposed"
        )
    return await propose(conn, project_id, canvas, found, opener=opener)


# Approving


def content_sha256(claim: ClaimRow) -> str:
    """The SHA-256 of a claim's content, as canonical JSON."""
    text = json.dumps(claim.content().as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def result(item: ItemRow) -> str:
    """The status ``item`` leaves in effect once decided."""
    if item.decision in ("accepted", "included"):
        return item.to_status
    if item.decision == "retired":
        return "retired"
    return item.from_status


def _transitions(found: list[ItemRow]) -> list[dict]:
    """The changes of status the decided ``found`` make take effect; a new claim's from none."""
    moved = []
    for item in found:
        to = result(item)
        if item.kind == "new" or to != item.from_status:
            before = None if item.kind == "new" else item.from_status
            moved.append(
                {"claim_id": item.claim_id, "claim": item.code, "from": before, "to": to, "evidence": item.evidence_ids}
            )
    return moved


async def approve(conn: AsyncConnection, project_id: int, canvas: CanvasRow, row: BaselineRow) -> None:
    """Approve baseline ``row``, every item of which has a decision: its snapshot, and the event ``canvas.baseline``."""
    held = await effective(conn, canvas.id)
    found = await items(conn, row.id)
    decided = {item.claim_id: result(item) for item in found}
    snapshot = [
        {
            "claim_id": claim.id,
            "claim": claim.code,
            "rev": claim.rev,
            "status": decided.get(claim.id) or held.get(claim.id, {}).get("status") or DEFAULT_STATUS,
            "sha256": content_sha256(claim),
        }
        for claim in await store.claims(conn, canvas.id)
    ]
    b = tables.canvas.baselines
    approved = {"status": "approved", "approved_at": func.now(), "snapshot": snapshot}
    await conn.execute(update(b).where(b.c.id == row.id, b.c.status == "proposed").values(**approved))
    deciders = sorted({item.decided_by for item in found if item.decided_by is not None})
    payload = {
        "canvas": canvas.slug,
        "canvas_id": canvas.id,
        "baseline_id": row.id,
        "baseline": row.number,
        "kind": row.kind,
        "week": row.week,
        "approved_by": deciders,
        "transitions": _transitions(found),
        "claims": len(snapshot),
    }
    await events.baseline_approved(conn, project_id, payload)


__all__ = [
    "ASKED",
    "DECIDES",
    "BaselineRow",
    "Item",
    "ItemRow",
    "approve",
    "baseline",
    "baselines",
    "config_value",
    "content_sha256",
    "effective",
    "expire_open",
    "items",
    "items_for",
    "logins",
    "open_now",
    "overdue",
    "propose",
    "proposers",
    "result",
    "self_approval",
    "today",
]
