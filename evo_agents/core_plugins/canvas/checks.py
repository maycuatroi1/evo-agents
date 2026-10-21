"""The check of a claim's test card against the project's signals (criterion a4 of plan living-lean-canvas): evidence
and proposals the hub writes, never a change of status.

Numbers. A claim not archived and not retired whose test card names a key of kind number (``metric``), with an
aggregate, a comparator and a threshold, is checked: the key's signals measured in the last ``window_days`` (90 when
the card names none), of the card's tenant when it names one, are summed up by ``aggregate`` (``last``, ``mean``,
``sum``, ``count``, and ``p50`` and ``p90`` as the nearest rank, Postgres's percentile_disc) and compared with
``threshold`` by ``comparator``: met, not met, or insufficient while fewer than ``min_samples`` (1 when the card names
none) are in the window. Nothing calls a model: the same signals give the same result.

Feedback. A test card whose metric is a key of kind feedback, or ``feedback``, counts what @strategist judged of the
claim in the window (``judging``; of that key, or of every item for ``feedback``): the value is how many items support
the claim, the samples how many support or contradict it, and the comparison is the same.

What it writes. Met and not met become a piece of evidence the hub writes, of kind signal for a number and feedback for
feedback, that supports or contradicts the claim; its ``basis`` holds the value, the samples, the window, the ids of
the first and last signal or item, and the result, and its link opens the key's signals on the web. It is synthetic
when any signal or item it sums up is, rests on a run when the agent of a run pushed any of them, and carries the
claim's label joined with theirs. A claim gets at most one such piece a day, and only when the result changed since the
last one, withdrawn or not; insufficient writes nothing. A new piece makes the hub's open proposals of the claim stale.

What it proposes. On the claim's latest result: met while the claim is an assumption or testing proposes validated;
not met once the test card's deadline passed proposes invalidated, unless the claim is invalidated or retired already.
Each is proposed once per piece of evidence (``proposals.propose`` with ``evidence_id``) for a member to accept, later
on the baseline. The rules of status hold as on every path, so validated is never proposed without real evidence that
supports the claim; a refusal is logged and nothing is proposed.

The caller holds the transaction and the canvas's row lock (``store.lock_canvas``).
"""

from __future__ import annotations

import logging
import operator
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urlencode

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.canvas import proposals, store, writes
from evo_agents.core_plugins.canvas.content import FEEDBACK, MAX_WINDOW_DAYS, CanvasError
from evo_agents.core_plugins.canvas.store import CanvasRow, ClaimRow
from evo_agents.hub import signals, tables
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_canvas import MAX_REASON_CHARS
from evo_agents.kg.policy import join_all

log = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = MAX_WINDOW_DAYS  # of a test card that names none
CHECK = "check"  # the key of the basis of a check's evidence: number or feedback
STOPPED = ("retired",)  # the statuses a check leaves alone
NUMBER_NEEDS = ("aggregate", "comparator", "threshold")  # of a test card on a key of kind number, beside its metric
FEEDBACK_NEEDS = ("comparator", "threshold")  # of a test card on feedback
COMPARE = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt}
OPEN_TO_VALIDATE = ("assumption", "testing")  # what met proposes to move to validated
CLOSED = ("invalidated", "retired")  # what not met past the deadline leaves alone


@dataclass(frozen=True)
class Hub:
    """The hub checking a project at ``now``: its access as the hub (every level of the project, writing as the hub)
    and the hub's public URL, which the links of its evidence start with."""

    access: ProjectAccess
    now: datetime
    public_url: str | None = None

    @property
    def writer(self) -> writes.Writer:
        return writes.Writer(self.access, writes.Author("hub"), None)

    @property
    def today(self) -> date:
        return self.now.astimezone(UTC).date()

    def since(self, card: dict) -> datetime:
        """Where the window of ``card`` starts: ``window_days`` before now."""
        return self.now - timedelta(days=card.get("window_days") or DEFAULT_WINDOW_DAYS)


async def hub_context(conn: AsyncConnection, project_id: int, config=None) -> Hub:
    """The hub checking project ``project_id`` now, by the clock of Postgres."""
    rules = await signals.project_rules(conn, project_id)
    access = ProjectAccess(project_id, rules, "admin", rules.levels[-1] if rules.levels else None)
    return Hub(access, await conn.scalar(select(func.now())), getattr(config, "public_url", None))


@dataclass(frozen=True)
class Measure:
    """What a check summed up for one claim: the value and the samples, the ids of the first and last signal or item,
    whether any was synthetic or pushed by the agent of a run, and their labels."""

    check: str  # number or feedback
    metric: str
    value: float | None
    samples: int
    first: int | None
    last: int | None
    synthetic: bool
    rests_on_run: bool
    labels: tuple

    def result(self, card: dict) -> str:
        """met, not_met, or insufficient while the window holds fewer than ``min_samples``."""
        if self.value is None or self.samples < (card.get("min_samples") or 1):
            return "insufficient"
        return "met" if COMPARE[card["comparator"]](self.value, card["threshold"]) else "not_met"


async def signal_key(conn: AsyncConnection, project_id: int, name: str) -> tuple[int, str] | None:
    """(id, kind) of key ``name`` of the project's registry, archived or not; None when it has none."""
    k = tables.signal_keys
    query = select(k.c.id, k.c.kind).where(k.c.project_id == project_id, k.c.name == name)
    row = (await conn.execute(query)).first()
    return None if row is None else (row.id, row.kind)


def _number(value) -> float | None:
    return None if value is None else float(value)


async def _measure_number(conn: AsyncConnection, hub: Hub, key_id: int, card: dict) -> Measure:
    s = tables.signals
    where = [s.c.key_id == key_id, s.c.value.is_not(None), s.c.measured_at > hub.since(card)]
    if card.get("tenant"):
        where.append(s.c.tenant == card["tenant"])
    figures = select(
        func.count(),
        func.avg(s.c.value),
        func.sum(s.c.value),
        func.percentile_disc(0.5).within_group(s.c.value),
        func.percentile_disc(0.9).within_group(s.c.value),
        func.min(s.c.id),
        func.max(s.c.id),
        func.bool_or(s.c.synthetic),
        func.bool_or(s.c.via == "run"),
    ).where(*where)
    count, mean, total, p50, p90, first, last, synthetic, by_run = (await conn.execute(figures)).one()
    latest = await conn.scalar(select(s.c.value).where(*where).order_by(s.c.measured_at.desc(), s.c.id.desc()).limit(1))
    values = {"last": latest, "mean": mean, "sum": total, "count": count, "p50": p50, "p90": p90}
    labels = tuple(await conn.scalars(select(s.c.label).where(*where).distinct()))
    value = _number(values[card["aggregate"]]) if count else None
    return Measure("number", card["metric"], value, count, first, last, bool(synthetic), bool(by_run), labels)


async def _measure_feedback(conn: AsyncConnection, hub: Hub, claim_id: int, key_id: int | None, card: dict):
    j = tables.canvas.judgings
    where = [j.c.claim_id == claim_id, j.c.verdict.in_(("supports", "contradicts")), j.c.measured_at > hub.since(card)]
    if key_id is not None:
        where.append(j.c.key_id == key_id)
    figures = select(
        func.count(),
        func.count().filter(j.c.verdict == "supports"),
        func.min(j.c.id),
        func.max(j.c.id),
        func.bool_or(j.c.synthetic),
        func.bool_or(j.c.rests_on_run),
    ).where(*where)
    count, supporting, first, last, synthetic, by_run = (await conn.execute(figures)).one()
    labels = tuple(await conn.scalars(select(j.c.label).where(*where).distinct()))
    value = float(supporting) if count else None
    return Measure("feedback", card["metric"], value, count, first, last, bool(synthetic), bool(by_run), labels)


async def measure(conn: AsyncConnection, hub: Hub, claim: ClaimRow) -> Measure | None:
    """What the check of ``claim``'s test card sums up; None when it has nothing to check (see the module)."""
    card = claim.test_card or {}
    metric = card.get("metric")
    if not metric or claim.status in STOPPED or claim.archived_at is not None:
        return None
    key = None if metric == FEEDBACK else await signal_key(conn, hub.access.project_id, metric)
    if metric != FEEDBACK and key is None:
        return None
    if metric == FEEDBACK or key[1] == "feedback":
        if any(card.get(name) is None for name in FEEDBACK_NEEDS):
            return None
        return await _measure_feedback(conn, hub, claim.id, None if key is None else key[0], card)
    if any(card.get(name) is None for name in NUMBER_NEEDS):
        return None
    return await _measure_number(conn, hub, key[0], card)


# Evidence


def _figure(value: float) -> str:
    return f"{value:g}"


def _summary(card: dict, found: Measure, result: str) -> str:
    window = card.get("window_days") or DEFAULT_WINDOW_DAYS
    tenant = f", tenant {card['tenant']}" if card.get("tenant") else ""
    against = f"against {card['comparator']} {_figure(card['threshold'])}: {result.replace('_', ' ')}"
    if found.check == "number":
        return (
            f"{found.metric}: {card['aggregate']} {_figure(found.value)} over the last {window} days "
            f"({found.samples} signals{tenant}) {against}"
        )
    of = "" if found.metric == FEEDBACK else f" of {found.metric}"
    return (
        f"{int(found.value)} of {found.samples} items{of} @strategist judged in the last {window} days support it"
        f"{tenant}, {against}"
    )


def _basis(card: dict, found: Measure, result: str) -> dict:
    basis = {
        CHECK: found.check,
        "metric": found.metric,
        "value": found.value,
        "samples": found.samples,
        "window_days": card.get("window_days") or DEFAULT_WINDOW_DAYS,
        "comparator": card["comparator"],
        "threshold": card["threshold"],
        "result": result,
        "signals" if found.check == "number" else "judgings": [found.first, found.last],
    }
    for name in ("aggregate", "tenant", "min_samples"):
        if card.get(name) is not None and (name != "aggregate" or found.check == "number"):
            basis[name] = card[name]
    return basis


def link(hub: Hub, key: str, tenant: str | None = None) -> str:
    """The page of key ``key``'s signals on the hub's web, from its public URL when the hub has one."""
    query = urlencode({"key": key, **({"tenant": tenant} if tenant else {})})
    return f"{hub.public_url or ''}/p/{quote(hub.access.name, safe='')}/signals?{query}"


def joined(hub: Hub, *labels) -> dict:
    """The label of what sums up things labelled ``labels``: their join, a label that cannot be read as the
    project's highest."""
    rules = hub.access.rules
    return rules.describe(join_all(rules.stored(label) or rules.top() for label in labels))


async def last_result(conn: AsyncConnection, claim_id: int):
    """The newest piece of evidence a check wrote of the claim, withdrawn or not: id, direction, synthetic,
    rests_on_run, created_at, withdrawn_at; None when there is none."""
    e = tables.canvas.evidence
    query = (
        select(e.c.id, e.c.direction, e.c.synthetic, e.c.rests_on_run, e.c.created_at, e.c.withdrawn_at)
        .where(e.c.claim_id == claim_id, e.c.actor == "hub", e.c.basis.has_key(CHECK))
        .order_by(e.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def _stale_proposals(conn: AsyncConnection, claim_id: int) -> None:
    """The hub's open proposals of the claim go stale: a newer result of its check replaces what they rest on."""
    p = tables.canvas.proposals
    rested = (p.c.claim_id == claim_id) & (p.c.status == "open") & (p.c.actor == "hub") & p.c.evidence_id.is_not(None)
    await conn.execute(update(p).where(rested).values(status="stale"))


async def record(conn: AsyncConnection, hub: Hub, claim: ClaimRow, found: Measure, result: str) -> int | None:
    """The piece of evidence the result ``result`` (met or not_met) of ``found`` writes of ``claim``; None when the
    claim got one today or its last one said the same."""
    direction = "supports" if result == "met" else "contradicts"
    last = await last_result(conn, claim.id)
    if last is not None:
        if last.created_at.astimezone(UTC).date() == hub.today:
            return None
        if (last.direction, last.synthetic, last.rests_on_run) == (direction, found.synthetic, found.rests_on_run):
            return None
    card = claim.test_card or {}
    is_key = found.metric != FEEDBACK
    given = writes.EvidenceInput(
        kind="signal" if found.check == "number" else "feedback",
        direction=direction,
        summary=_summary(card, found, result),
        url=link(hub, found.metric, card.get("tenant")) if is_key else None,
        observed_on=hub.today,
        synthetic=found.synthetic,
        basis=_basis(card, found, result),
        rests_on_run=found.rests_on_run,
        label=joined(hub, claim.label, *found.labels),
    )
    evidence_id = await writes.add_evidence(conn, hub.writer, claim, given)
    await _stale_proposals(conn, claim.id)
    return evidence_id


# Proposals


def _past(deadline, today: date) -> bool:
    return bool(deadline) and date.fromisoformat(str(deadline)) < today


def _target(hub: Hub, claim: ClaimRow, last) -> str | None:
    """The status the latest result ``last`` of ``claim`` proposes, or None."""
    if last is None or last.withdrawn_at is not None:
        return None
    if last.direction == "supports" and claim.status in OPEN_TO_VALIDATE:
        return "validated"
    deadline = (claim.test_card or {}).get("deadline")
    if last.direction == "contradicts" and _past(deadline, hub.today) and claim.status not in CLOSED:
        return "invalidated"
    return None


def _reason(claim: ClaimRow, target: str, evidence_id: int) -> str:
    if target == "validated":
        why = f"the test card of {claim.code} is met (evidence {evidence_id})"
    else:
        deadline = (claim.test_card or {}).get("deadline")
        why = f"the test card of {claim.code} is not met (evidence {evidence_id}) and its deadline {deadline} passed"
    return f"value check: {why}; a member decides"[:MAX_REASON_CHARS]


async def propose(conn: AsyncConnection, hub: Hub, canvas: CanvasRow, claim: ClaimRow) -> int | None:
    """The proposal the latest result of ``claim``'s check makes, once per piece of evidence; None when it makes
    none or the rules refuse it."""
    last = await last_result(conn, claim.id)
    target = _target(hub, claim, last)
    if target is None:
        return None
    p = tables.canvas.proposals
    made = select(p.c.id).where(p.c.evidence_id == last.id, p.c.fields["status"].astext == target).limit(1)
    if await conn.scalar(made) is not None:
        return None
    change = writes.Change(claim.code, claim.rev, {"status": target})
    try:
        (proposal_id,) = await proposals.propose(
            conn,
            hub.writer,
            canvas,
            [change],
            _reason(claim, target, last.id),
            synthetic=last.synthetic,
            evidence_id=last.id,
        )
    except CanvasError as exc:
        extra = {"project": hub.access.name, "claim": claim.code, "status": target, "why": exc.message}
        log.info("value check proposes nothing", extra=extra)
        return None
    return proposal_id


# A claim, a canvas


async def check_claim(conn: AsyncConnection, hub: Hub, canvas: CanvasRow, claim: ClaimRow) -> Counter:
    """Check ``claim``'s test card: the evidence and the proposal it makes; what it did, counted."""
    done = Counter()
    found = await measure(conn, hub, claim)
    if found is None:
        return done
    result = found.result(claim.test_card or {})
    done["checked"] += 1
    done[result] += 1
    if result != "insufficient":
        try:
            written = await record(conn, hub, claim, found, result)
        except CanvasError as exc:  # a claim that holds as much evidence as it may
            log.info("value check writes no evidence", extra={"claim": claim.code, "why": exc.message})
            written = None
        if written is not None:
            done["evidence"] += 1
    if await propose(conn, hub, canvas, claim) is not None:
        done["proposals"] += 1
    return done


async def check_canvas(conn: AsyncConnection, hub: Hub, canvas: CanvasRow) -> Counter:
    """Check every claim of ``canvas`` not archived; what it did, counted."""
    done = Counter()
    for claim in await store.claims(conn, canvas.id):
        done += await check_claim(conn, hub, canvas, claim)
    return done


__all__ = [
    "CHECK",
    "DEFAULT_WINDOW_DAYS",
    "Hub",
    "Measure",
    "check_canvas",
    "check_claim",
    "hub_context",
    "joined",
    "last_result",
    "link",
    "measure",
    "propose",
    "record",
    "signal_key",
]
