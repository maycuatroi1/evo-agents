"""Signals of a project: taking a batch from an ingest key or a member, reading them back, and the retention.

A signal is a number or a piece of feedback in text about a project's product, measured by a service outside the hub
(a search's zero-result rate, a support ticket) or entered by a member. Its key must be in the project's registry
(``signal_keys``); the routes are ``evo_agents.hub.server.signals`` and ``evo_agents.hub.server.signal_keys``, and
``docs/signals.md`` says how a service sends them.

An ingest key is ``evi_`` and 32 random bytes in base64url; Postgres keeps only its hex SHA-256. It writes the signals
of its one project through POST /v1/signals/{project} and opens nothing else: the hub's middleware refuses it with 403
everywhere else before any database is asked (``evo_agents.hub.server.security``), and ``open_ingest`` refuses it on
another project's path. Revoking it takes effect on the next request.

``push`` takes one batch: at most MAX_ITEMS items, the route reading at most MAX_BATCH_BYTES of body. Each item is
checked on its own and a refused one is answered with its reason while the others go in: a key the registry lacks or
archived, a number for a feedback key or text for a number key, feedback over MAX_TEXT_BYTES, a time more than
FUTURE_SKEW ahead or older than the retention, a level the ladder lacks, above the ingest key's ``max_level``, or above
what the project's hub sink clears. A signal's label is the project's default with the level the item names, else the
key's level, else the default's own; its projects are this project alone. An ingest key and an ``external_id`` (or a
member and one, without a key) are the key of idempotence: a batch sent again adds nothing and counts the items it
repeats as ``duplicates``.

Two limits keep a service gone wrong from filling the database. An ingest key sends at most BATCHES_PER_MINUTE batches
in a fixed window of a minute, counted on its row in a transaction of its own so a refused batch counts too; past it
the answer is 429 with Retry-After until the window ends. A project takes at most SIGNALS_PER_DAY signals in any 24
hours, whoever sends them; a batch that would go past it is refused whole with 429 and Retry-After until the oldest
signal of the day leaves the window. Batches of one project go one at a time under a transaction-level advisory lock, so
two of them cannot both pass the count.

``prune_signals`` (the job hub.prune_signals, daily) deletes the signals measured more than EVO_HUB_SIGNAL_DAYS days
ago, 400 by default (``evo_agents.hub.config``), key by key along the index of each key's signals.

SQLAlchemy Core on ``evo_agents.hub.tables``; nothing here imports ``evo_agents.hub.server``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import math
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import BigInteger, case, delete, func, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.access import ProjectRules, Refused
from evo_agents.hub.tables_signals import MAX_TEXT_BYTES

log = logging.getLogger(__name__)

INGEST_PREFIX = "evi_"
TOKEN_BYTES = 32
INGEST_TOKEN = re.compile(r"evi_[A-Za-z0-9_-]{43}")  # the prefix and 32 bytes of base64url without padding
MAX_ITEMS = 500  # of a batch
MAX_BATCH_BYTES = 1024 * 1024  # of the body of a batch
BATCHES_PER_MINUTE = 60  # of one ingest key
WINDOW = timedelta(minutes=1)
SIGNALS_PER_DAY = 50_000  # of one project, in any 24 hours
DAY = timedelta(days=1)
FUTURE_SKEW = timedelta(minutes=10)  # how far ahead of the hub's clock a measurement may be
LOCK_BASE = (
    0x7369676E616C0000  # "signal" in ASCII, shifted: the advisory lock of a project's batches is this plus its id
)


class SignalError(Exception):
    """A refusal of a whole request: ``status`` and ``message`` for the hub's error body, ``headers`` such as
    Retry-After."""

    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers


@dataclass(frozen=True)
class Pusher:
    """Who sends a batch to which project: an ingest key (``ingest_key_id``, writing at most ``max_level``), or a
    member (``user_id``), the agent of a run among them (``run_id``)."""

    project_id: int
    rules: ProjectRules
    via: str  # ingest, member or run (tables_signals.VIAS)
    ingest_key_id: int | None = None
    ingest_key: str | None = None  # its name
    max_level: str | None = None  # an ingest key's; None for a member, whom the hub sink alone bounds
    user_id: int | None = None
    run_id: int | None = None

    @property
    def project(self) -> str:
        return self.rules.name

    def __repr__(self) -> str:
        return f"Pusher(project={self.project!r}, via={self.via!r}, ingest_key_id={self.ingest_key_id})"


@dataclass
class Pushed:
    """What ``push`` did with a batch: the signals stored, the items an earlier batch had stored already, and each item
    refused with its index in the batch and why."""

    batch_id: uuid.UUID
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict] = field(default_factory=list)


class _Rejected(Exception):
    pass


# Ingest keys


def new_ingest_token() -> str:
    return INGEST_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode()


def hash_token(token: str) -> str:
    """The hex SHA-256 the hub keeps of a token, as ``evo_agents.hub.server.security.hash_token`` computes it."""
    return hashlib.sha256(token.encode()).hexdigest()


async def project_rules(conn: AsyncConnection, project_id: int) -> ProjectRules:
    """The ladder, sinks and default label of project ``project_id``, as ``ProjectRules``."""
    p, ps = tables.projects, tables.project_sinks
    row = (
        await conn.execute(select(p.c.name, p.c.levels, p.c.locations, p.c.default_label).where(p.c.id == project_id))
    ).one()
    sinks = await conn.execute(
        select(ps.c.sink_id, ps.c.kind, ps.c.clearance).where(ps.c.project_id == project_id).order_by(ps.c.sink_id)
    )
    listed = [{"id": sink.sink_id, "kind": sink.kind, "clearance": sink.clearance} for sink in sinks]
    return ProjectRules(row.name, row.levels, row.locations, listed, row.default_label)


def _count_batch(key_id: int):
    """The UPDATE that counts one batch in the key's window of a minute, starting a new window when the last one
    ended, and marks the key used: (batches in the window, when it started, now)."""
    k = tables.signal_ingest_keys
    ended = or_(k.c.window_started_at.is_(None), k.c.window_started_at <= func.now() - WINDOW)
    return (
        update(k)
        .where(k.c.id == key_id)
        .values(
            window_started_at=case((ended, func.now()), else_=k.c.window_started_at),
            window_batches=case((ended, 1), else_=k.c.window_batches + 1),
            last_used_at=func.now(),
        )
        .returning(k.c.window_batches, k.c.window_started_at, func.now().label("now"))
    )


def _retry_after(until: datetime, now: datetime) -> str:
    return str(max(1, math.ceil((until - now).total_seconds())))


async def open_ingest(engine: AsyncEngine, token: str, project: str) -> Pusher:
    """The pusher of a batch sent with ingest key ``token`` to ``project``, its batch counted against the key's rate
    in a transaction of its own; 401 for a key that is malformed, revoked or unknown, 403 for another project's, 429
    past BATCHES_PER_MINUTE."""
    if not INGEST_TOKEN.fullmatch(token):
        raise SignalError(401, "the ingest key is malformed: it is evi_ and 43 characters of base64url")
    k, p = tables.signal_ingest_keys, tables.projects
    async with engine.begin() as conn:
        found = (
            await conn.execute(
                select(k.c.id, k.c.name, k.c.project_id, k.c.max_level, p.c.name.label("project"))
                .join_from(k, p, p.c.id == k.c.project_id)
                .where(k.c.token_hash == hash_token(token), k.c.revoked_at.is_(None))
            )
        ).one_or_none()
        if found is None:
            raise SignalError(401, "the ingest key is revoked or unknown: ask an admin of the project for a new one")
        if found.project != project:
            raise SignalError(403, f"this ingest key writes the signals of another project, not of {project}")
        batches, started, now = (await conn.execute(_count_batch(found.id))).one()
        rules = await project_rules(conn, found.project_id)
    if batches > BATCHES_PER_MINUTE:
        message = f"ingest key {found.name} sent more than {BATCHES_PER_MINUTE} batches this minute: send fewer, larger"
        raise SignalError(429, message, {"Retry-After": _retry_after(started + WINDOW, now)})
    return Pusher(found.project_id, rules, "ingest", found.id, found.name, found.max_level)


# Pushing


@dataclass(frozen=True)
class _Key:
    id: int
    name: str
    kind: str
    source: str
    level: str | None
    archived: bool


async def _keys(conn: AsyncConnection, project_id: int, names: set[str]) -> dict[str, _Key]:
    sk = tables.signal_keys
    query = select(sk.c.id, sk.c.name, sk.c.kind, sk.c.source, sk.c.level, sk.c.archived_at.is_not(None)).where(
        sk.c.project_id == project_id, sk.c.name.in_(sorted(names))
    )
    return {row[1]: _Key(*row) for row in await conn.execute(query)}


def _label(pusher: Pusher, key: _Key, level: str | None) -> dict:
    """The label of an item: the project's default with ``level``, else the key's, else the default's own level."""
    rules = pusher.rules
    level = level or key.level or rules.default_label.get("level")
    if level not in rules.levels:  # an ingest key learns no more of the ladder than that
        ladder = "" if pusher.via == "ingest" else f" ({', '.join(rules.levels)})"
        raise _Rejected(f"level {level!r} is not a level of project {pusher.project}{ladder}")
    if pusher.max_level is not None and (
        pusher.max_level not in rules.levels or rules.levels.index(level) > rules.levels.index(pusher.max_level)
    ):
        raise _Rejected(f"level {level} is above {pusher.max_level}, the highest ingest key {pusher.ingest_key} writes")
    try:
        found = rules.given({**rules.default_label, "level": level, "projects": [pusher.project]})
    except Refused as exc:
        raise _Rejected(str(exc)) from None
    if rules.hub_sink is None or not found.below(rules.clearance_label(rules.hub_sink)):
        raise _Rejected(f"level {level} is above what the hub sink of project {pusher.project} clears")
    return rules.describe(found)


def _value(key: _Key, item) -> tuple[float | None, str | None]:
    if key.kind == "number":
        if item.value is None or item.text is not None:
            raise _Rejected(f"key {key.name} takes a number in value, and no text")
        return item.value, None
    if item.text is None or item.value is not None:
        raise _Rejected(f"key {key.name} takes feedback in text, and no value")
    if not item.text.strip():
        raise _Rejected("the text of feedback is empty")
    if len(item.text.encode()) > MAX_TEXT_BYTES:
        raise _Rejected(f"the text of feedback is over {MAX_TEXT_BYTES // 1024} KiB in UTF-8")
    return None, item.text


def _measured(item, now: datetime, days: int) -> datetime:
    measured = item.measured_at or now
    if measured > now + FUTURE_SKEW:
        raise _Rejected(f"measured_at {measured.isoformat()} is ahead of the hub's clock")
    if measured < now - timedelta(days=days):
        raise _Rejected(f"measured_at {measured.isoformat()} is older than the {days} days the hub keeps signals")
    return measured


def _row(pusher: Pusher, keys: dict[str, _Key], item, now: datetime, days: int) -> dict:
    """The signals row of ``item``; raises _Rejected with why it may not be one."""
    key = keys.get(item.key)
    if key is None:
        raise _Rejected(f"key {item.key} is not in the registry of project {pusher.project}: an admin adds it first")
    if key.archived:
        raise _Rejected(f"key {item.key} is archived and takes no new signal")
    value, text = _value(key, item)
    return {
        "project_id": pusher.project_id,
        "key_id": key.id,
        "value": value,
        "text": text,
        "source": item.source if item.source is not None else key.source,
        "measured_at": _measured(item, now, days),
        "tenant": item.tenant,
        "synthetic": item.synthetic,
        "external_id": item.external_id,
        "label": _label(pusher, key, item.level),
        "via": pusher.via,
        "ingest_key_id": pusher.ingest_key_id,
        "user_id": pusher.user_id,
        "run_id": pusher.run_id,
    }


async def _within_the_day(conn: AsyncConnection, pusher: Pusher, adding: int) -> None:
    """429 when ``adding`` signals would take the project past SIGNALS_PER_DAY in the last 24 hours."""
    s = tables.signals
    since = func.now() - DAY
    count, oldest, now = (
        await conn.execute(
            select(func.count(), func.min(s.c.received_at), func.now()).where(
                s.c.project_id == pusher.project_id, s.c.received_at > since
            )
        )
    ).one()
    if count + adding <= SIGNALS_PER_DAY:
        return
    message = (
        f"project {pusher.project} took {count} signals in the last 24 hours and takes at most {SIGNALS_PER_DAY}: "
        f"this batch of {adding} would go past it"
    )
    raise SignalError(429, message, {"Retry-After": _retry_after((oldest or now) + DAY, now)})


async def push(conn: AsyncConnection, pusher: Pusher, items: list, days: int) -> Pushed:
    """Store the items of one batch that pass, in the transaction of ``conn``; the others answered with why. ``days``
    is the retention, which an item measured earlier falls out of."""
    await conn.execute(select(func.pg_advisory_xact_lock(literal(LOCK_BASE, BigInteger) + pusher.project_id)))
    now = (await conn.execute(select(func.now()))).scalar_one()
    keys = await _keys(conn, pusher.project_id, {item.key for item in items})
    done = Pushed(batch_id=uuid.uuid4())
    rows = []
    for index, item in enumerate(items):
        try:
            rows.append({**_row(pusher, keys, item, now, days), "batch_id": done.batch_id})
        except _Rejected as exc:
            done.rejected.append({"index": index, "key": item.key, "external_id": item.external_id, "reason": str(exc)})
    if rows:
        await _within_the_day(conn, pusher, len(rows))
        s = tables.signals
        stored = await conn.execute(insert(s).values(rows).on_conflict_do_nothing().returning(s.c.id))
        done.accepted = len(stored.all())
        done.duplicates = len(rows) - done.accepted
    figures = {"project": pusher.project, "via": pusher.via, "accepted": done.accepted, "rejected": len(done.rejected)}
    log.info("signals pushed", extra={**figures, "duplicates": done.duplicates, "batch_id": str(done.batch_id)})
    return done


# Reading


def _signals_query():
    s, sk, ik, users = tables.signals, tables.signal_keys, tables.signal_ingest_keys, tables.users
    return (
        select(
            s.c.id,
            sk.c.name.label("key"),
            sk.c.kind,
            s.c.value,
            s.c.text,
            s.c.source,
            s.c.measured_at,
            s.c.tenant,
            s.c.synthetic,
            s.c.external_id,
            s.c.label,
            s.c.via,
            ik.c.name.label("ingest_key"),
            users.c.login,
            s.c.run_id,
            s.c.batch_id,
            s.c.received_at,
        )
        .select_from(
            s.join(sk, sk.c.id == s.c.key_id)
            .outerjoin(ik, ik.c.id == s.c.ingest_key_id)
            .outerjoin(users, users.c.id == s.c.user_id)
        )
        .order_by(s.c.measured_at.desc(), s.c.id.desc())
    )


async def signal_rows(
    conn: AsyncConnection,
    project_id: int,
    readable: list[tuple[str, str]],
    *,
    key: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    tenant: str | None = None,
    limit: int = 100,
) -> list:
    """The newest ``limit`` signals of the project whose label's (level, location) is in ``readable``, of ``key``
    when given, measured from ``since`` (inclusive) to ``until`` (exclusive), of ``tenant`` when given."""
    if not readable:
        return []
    s, sk = tables.signals, tables.signal_keys
    query = _signals_query().where(
        s.c.project_id == project_id,
        tuple_(s.c.label["level"].astext, s.c.label["location"].astext).in_(readable),
    )
    for wanted, condition in (
        (key, lambda: sk.c.name == key),
        (since, lambda: s.c.measured_at >= since),
        (until, lambda: s.c.measured_at < until),
        (tenant, lambda: s.c.tenant == tenant),
    ):
        if wanted is not None:
            query = query.where(condition())
    return list(await conn.execute(query.limit(limit)))


# The retention


async def prune_signals(engine: AsyncEngine, days: int) -> dict:
    """Delete the signals measured more than ``days`` days ago, key by key; how many went."""
    s, sk = tables.signals, tables.signal_keys
    deleted = 0
    async with engine.begin() as conn:
        key_ids = list((await conn.execute(select(sk.c.id).order_by(sk.c.id))).scalars())
    for key_id in key_ids:
        async with engine.begin() as conn:
            cutoff = func.now() - timedelta(days=days)
            gone = await conn.execute(delete(s).where(s.c.key_id == key_id, s.c.measured_at < cutoff))
            deleted += gone.rowcount or 0
    if deleted:
        log.info("signals pruned", extra={"deleted": deleted, "days": days})
    return {"deleted": deleted, "days": days}
