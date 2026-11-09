"""The hub's cache and its sliding-window rate limits, shared by its processes on Redis when EVO_HUB_REDIS_URL names one,
else kept in each process's memory (``docs/hub.md``, section Cache).

``Cache`` keeps JSON values under a namespace, a project's name or HUB for what belongs to none, and a key whose first
segment, up to the first ``:``, is its area (``guard:policy`` is of the area ``guard``). ``get``, ``set`` with a TTL and
``drop`` (a namespace's keys under a prefix) are the plain calls; ``fetch`` is what a read path uses: the value cached
for the key, else what ``load`` reads from Postgres, kept for ``ttl`` seconds. Postgres stays what is true: a value is
only ever a copy, and a cache that cannot answer (Redis away, a timeout) is a miss, so the hub answers the same, only
slower. After a failure the process leaves Redis alone for BREAK_SECONDS rather than wait on each request.

What a value depends on is said once per area, by the tables it is read from (``watch``). A write to one of them
through any engine of the process (an INSERT, UPDATE or DELETE, also one inside a CTE, and a DELETE of a table whose
rows the database removes or changes with them, by ON DELETE CASCADE or SET NULL) drops the area's values once the
transaction commits: in the writing process at the commit, and in every api process by a wake-up on CHANNEL
(``evo_agents.hub.wake``, through Postgres's NOTIFY or the event bus) that the process's listener
(``server.listen.Listener``) hands to ``Cache.heard``. Each value carries the generations it was read under, the
whole cache's (its epoch), its namespace's and its area's; a drop bumps them, so a value read before a write and kept
after the drop is never served. While the process's listener does not listen, the cache is bypassed, since a write
elsewhere would not reach it; after the listener (re)connects, and after Redis comes back from a failure, the epoch is
bumped, so nothing kept while a drop could be missed is served.

``Window`` counts hits of a key in a sliding window of ``window`` seconds: ``retry_after`` says how long a key that
reached ``limit`` waits, ``hit`` counts one more. On Redis the window is a sorted set per key, updated by one script
on Redis's clock, so every process sees the same count; without Redis, or while it fails, the process's own
``LocalWindow`` counts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import weakref
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

REDIS_VARIABLE = "EVO_HUB_REDIS_URL"
SCHEMES = ("redis", "rediss", "unix")
CHANNEL = "hub_cache"  # the wake-up channel of an area written: the payload is the area
HUB = "~hub"  # the namespace of what belongs to no project
PREFIX = "evo:cache:"  # every key the hub writes to Redis
TIMEOUT = 0.25  # seconds Redis has to answer a call before the call counts as a miss
CONNECT_TIMEOUT = 0.5  # ...and to take a connection
BREAK_SECONDS = 5.0  # seconds the process leaves Redis alone after a call failed
DIRTY_SECONDS = 2.0  # seconds an area this process wrote skips Redis, until its listener heard the write
CHECK_EVERY = 15.0  # seconds a health check's answer is kept
MAX_LOCAL_KEYS = 10_000  # values the process's memory keeps; past that the least recently set goes
MAX_WINDOW_KEYS = 10_000  # keys a LocalWindow tracks; past that the one hit least recently is forgotten

MISSING = object()


class RedisUrlError(ValueError):
    """EVO_HUB_REDIS_URL is set and is not a redis URL."""


def redis_url(env: Mapping[str, str] | None = None) -> str | None:
    """Redis's URL, or None while the cache is in memory. The URL may hold a password: never log it."""
    env = os.environ if env is None else env
    raw = (env.get(REDIS_VARIABLE) or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme not in SCHEMES or not (parts.hostname or (parts.scheme == "unix" and parts.path)):
        raise RedisUrlError(
            f"{REDIS_VARIABLE} must be a redis://, rediss:// or unix:// URL such as redis://redis:6379/0"
        )
    return raw


def area_of(key: str) -> str:
    return key.split(":", 1)[0]


# What writes drop

_WATCHED: dict[str, set[str]] = {}  # table name -> the areas read from it
_CASCADED: dict[str, set[str]] | None = None  # table name -> the areas a DELETE of its rows reaches
_TOUCHED = "hub_cache_touched"  # the connection's info: the areas its open transaction wrote
_caches: weakref.WeakSet[Cache] = weakref.WeakSet()


def watch(area: str, *tables) -> None:
    """Drop the values of ``area`` whenever a transaction that wrote one of ``tables`` commits."""
    global _CASCADED
    for table in tables:
        _WATCHED.setdefault(table.name, set()).add(area)
    _CASCADED = None
    _listen()


def _cascaded() -> dict[str, set[str]]:
    """For each table, the areas a DELETE of its rows reaches through ON DELETE CASCADE or SET NULL, transitively."""
    global _CASCADED
    if _CASCADED is not None:
        return _CASCADED
    from evo_agents.hub import tables

    reaches: dict[str, set[str]] = {}  # parent -> children its deletes change
    for table in tables.metadata.tables.values():
        for fk in table.foreign_keys:
            if (fk.ondelete or "").upper().split() in (["CASCADE"], ["SET", "NULL"]):
                reaches.setdefault(fk.column.table.name, set()).add(table.name)
    out: dict[str, set[str]] = {}
    for start in reaches:
        seen, todo = set(), [start]
        while todo:
            for child in reaches.get(todo.pop(), ()):
                if child not in seen:
                    seen.add(child)
                    todo.append(child)
        areas = set().union(*(_WATCHED.get(name, set()) for name in seen))
        if areas:
            out[start] = areas
    _CASCADED = out
    return out


def _listen() -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    if event.contains(Engine, "after_execute", _executed):
        return
    event.listen(Engine, "after_execute", _executed)
    event.listen(Engine, "commit", _committed)
    event.listen(Engine, "rollback", _rolled_back)


def _table_name(target) -> str | None:
    while target is not None and not hasattr(target, "foreign_keys"):  # an alias of a table
        target = getattr(target, "element", None)
    return getattr(target, "name", None)


def _written(statement, compiled) -> set[str]:
    """The areas ``statement`` (with its CTEs, as compiled) may change."""
    from sqlalchemy.sql.dml import Delete, UpdateBase

    writes = [statement] if isinstance(statement, UpdateBase) else []
    for cte in getattr(compiled, "ctes", None) or ():
        if isinstance(getattr(cte, "element", None), UpdateBase):
            writes.append(cte.element)
    areas: set[str] = set()
    for write in writes:
        name = _table_name(write.table)
        areas |= _WATCHED.get(name, set())
        if isinstance(write, Delete):
            areas |= _cascaded().get(name, set())
    return areas


def _executed(conn, clauseelement, multiparams, params, execution_options, result) -> None:
    if not _WATCHED:
        return
    areas = _written(clauseelement, getattr(getattr(result, "context", None), "compiled", None))
    if not areas:
        return
    touched = conn.info.setdefault(_TOUCHED, set())
    from evo_agents.hub import wake

    for area in sorted(areas - touched):
        touched.add(area)
        wake.notify_sync(conn, CHANNEL, area)  # sent when the transaction commits, dropped with it on a rollback


def _committed(conn) -> None:
    for area in conn.info.pop(_TOUCHED, ()):
        for cache in list(_caches):
            cache.wrote(area)


def _rolled_back(conn) -> None:
    conn.info.pop(_TOUCHED, None)


# The cache


class Cache:
    """The process's cache: on Redis at ``url``, else in memory. ``listener`` is the api process's
    ``server.listen.Listener``, None where no other process writes what the cache keeps (a test of the cache alone)."""

    def __init__(self, url: str | None = None, *, listener=None, clock: Callable[[], float] = time.monotonic):
        self.url, self.listener, self.clock = url, listener, clock
        self._values: OrderedDict[str, tuple[float, tuple, Any]] = OrderedDict()  # key -> (expiry, tag, value)
        self._gens: dict[str, int] = {}  # the memory's generations: "~epoch", "ns:<namespace>", "area:<area>"
        self._dirty: dict[str, float] = {}  # area -> until when this process skips Redis for it
        self._clients: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # event loop -> redis client
        self._down_until = 0.0
        self._missed = False  # a call to Redis failed since the epoch was last bumped
        self._checked: tuple[float, str] | None = None
        if listener is not None:
            listener.on(CHANNEL, self.heard)
        _caches.add(self)

    @property
    def shared(self) -> bool:
        return self.url is not None

    # The calls

    async def get(self, namespace: str, key: str, default=None):
        """The value kept for ``key`` of ``namespace``, or ``default``."""
        found, _ = await self._read(namespace, key)
        return default if found is MISSING else found

    async def set(self, namespace: str, key: str, value, ttl: float) -> None:
        """Keep ``value`` (JSON) for ``key`` of ``namespace`` for ``ttl`` seconds."""
        _, tag = await self._read(namespace, key)
        await self._write(namespace, key, value, ttl, tag)

    async def fetch(self, namespace: str, key: str, ttl: float, load: Callable[[], Awaitable[Any]]):
        """The value kept for ``key``, else ``await load()``, kept for ``ttl`` seconds unless a write dropped its area
        while it loaded."""
        if not self._usable(area_of(key)):
            return await load()
        found, tag = await self._read(namespace, key)
        if found is not MISSING:
            return found
        value = await load()
        await self._write(namespace, key, value, ttl, tag)
        return value

    async def drop(self, namespace: str, prefix: str = "") -> None:
        """Drop the values of ``namespace`` whose key starts with ``prefix``: all of the namespace's for none. The area
        of a prefix that names one is dropped in every namespace, so a value being loaded is not kept either."""
        bumped = f"area:{area_of(prefix)}" if prefix else f"ns:{namespace}"
        self._drop_local(namespace, prefix, bumped)
        if not self.shared:
            return
        client = self._client()
        if client is None:
            return
        try:
            await client.incr(PREFIX + "~gen:" + bumped)
            async for found in client.scan_iter(match=_glob(PREFIX + f"{namespace}:{prefix}") + "*", count=500):
                await client.unlink(found)
        except Exception as exc:
            self._failed(exc)

    # What writes say

    def wrote(self, area: str) -> None:
        """A transaction of this process that wrote ``area`` committed."""
        if self.shared:
            self._dirty[area] = self.clock() + DIRTY_SECONDS
        else:
            self._bump(f"area:{area}")

    def heard(self, payload: str | None) -> None:
        """The listener's handler: an area some process wrote, or None when the listener (re)connected."""
        bumped = "~epoch" if payload is None else f"area:{payload}"
        self._bump(bumped)
        if not self.shared:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._bump_shared(bumped, payload), name="evo-hub-cache-drop")

    async def _bump_shared(self, bumped: str, area: str | None) -> None:
        client = self._client()
        if client is None:
            return
        try:
            await client.incr(PREFIX + "~gen:" + bumped)
        except Exception as exc:
            self._failed(exc)
            return
        if area is not None:
            self._dirty.pop(area, None)

    # Health

    async def check(self) -> str:
        """``off`` in memory, else ``ok`` when Redis answers a PING and ``error`` when not; kept CHECK_EVERY s."""
        if not self.shared:
            return "off"
        now = self.clock()
        if self._checked is not None and now - self._checked[0] < CHECK_EVERY:
            return self._checked[1]
        client = self._client(ignore_break=True)
        try:
            if client is None:
                raise ConnectionError("no client")
            await asyncio.wait_for(client.ping(), TIMEOUT * 4)
            status = "ok"
        except Exception as exc:  # Redis's error, never the hub's
            log.warning("health check: cache unavailable", extra={"error": type(exc).__name__})
            status = "error"
        self._checked = (now, status)
        return status

    async def close(self) -> None:
        """Close the Redis client of the running event loop, when there is one."""
        try:
            client = self._clients.pop(asyncio.get_running_loop(), None)
        except RuntimeError:
            return
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # closing what may have failed
                pass

    # Inside

    def _usable(self, area: str) -> bool:
        if self.listener is not None:
            if not self.listener.up:
                self.listener.start()  # its first (re)connection bumps the epoch; until then nothing is kept
                return False
        until = self._dirty.get(area)
        if until is not None:
            if self.clock() < until:
                return False
            del self._dirty[area]
        return not self.shared or self.clock() >= self._down_until

    async def _read(self, namespace: str, key: str) -> tuple[Any, tuple | None]:
        """The value kept and the tag a value read now is to carry; (MISSING, None) when Redis failed."""
        names = ("~epoch", f"ns:{namespace}", f"area:{area_of(key)}")
        if not self.shared:
            tag = tuple(self._gens.get(name, 0) for name in names)
            kept = self._values.get(f"{namespace}:{key}")
            if kept is None or kept[0] <= self.clock() or kept[1] != tag:
                return MISSING, tag
            return kept[2], tag
        client = self._client()
        if client is None:
            return MISSING, None
        try:
            raw, *gens = await client.mget([PREFIX + f"{namespace}:{key}", *(PREFIX + "~gen:" + n for n in names)])
        except Exception as exc:
            self._failed(exc)
            return MISSING, None
        await self._recovered(client)
        tag = tuple(int(gen or 0) for gen in gens)
        if raw is None:
            return MISSING, tag
        kept = json.loads(raw)
        return (kept["v"], tag) if tuple(kept["t"]) == tag else (MISSING, tag)

    async def _write(self, namespace: str, key: str, value, ttl: float, tag: tuple | None) -> None:
        if tag is None:
            return
        if not self.shared:
            self._values[f"{namespace}:{key}"] = (self.clock() + ttl, tag, value)
            self._values.move_to_end(f"{namespace}:{key}")
            while len(self._values) > MAX_LOCAL_KEYS:
                self._values.popitem(last=False)
            return
        client = self._client()
        if client is None:
            return
        body = json.dumps({"t": tag, "v": value}, separators=(",", ":"))
        try:
            await client.set(PREFIX + f"{namespace}:{key}", body, px=max(1, int(ttl * 1000)))
        except Exception as exc:
            self._failed(exc)

    def _bump(self, name: str) -> None:
        self._gens[name] = self._gens.get(name, 0) + 1

    def _drop_local(self, namespace: str, prefix: str, bumped: str) -> None:
        self._bump(bumped)
        for kept in [k for k in self._values if k.startswith(f"{namespace}:{prefix}")]:
            del self._values[kept]

    def _client(self, *, ignore_break: bool = False):
        if not ignore_break and self.clock() < self._down_until:
            return None
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            import redis.asyncio

            client = redis.asyncio.Redis.from_url(
                self.url, socket_timeout=TIMEOUT, socket_connect_timeout=CONNECT_TIMEOUT, retry_on_timeout=False
            )
            self._clients[loop] = client
        return client

    def _failed(self, exc: BaseException) -> None:
        if self.clock() >= self._down_until:
            log.warning(
                "cache unavailable: the hub reads Postgres instead",
                extra={"error": type(exc).__name__, "retry_s": BREAK_SECONDS},
            )
        self._down_until = self.clock() + BREAK_SECONDS
        self._missed = True

    async def _recovered(self, client) -> None:
        """After a failure, bump the epoch once Redis answers: a drop may have been missed meanwhile."""
        if not self._missed:
            return
        try:
            await client.incr(PREFIX + "~gen:~epoch")
            self._missed = False
        except Exception as exc:
            self._failed(exc)


def _glob(text: str) -> str:
    """``text`` with the special characters of a Redis MATCH pattern escaped."""
    return "".join("\\" + c if c in "*?[]\\" else c for c in text)


# Rate limits


class LocalWindow:
    """Hits per key in a sliding window, in this process's memory: a key hit ``limit`` times within ``window`` seconds
    waits until the oldest of those hits leaves the window. At most ``max_keys`` keys are tracked; past that the one
    hit least recently is forgotten."""

    def __init__(
        self, limit: int, window: float, max_keys: int = MAX_WINDOW_KEYS, clock: Callable[[], float] = time.monotonic
    ):
        self.limit, self.window, self.max_keys, self.clock = limit, window, max_keys, clock
        self._hits: dict[str, deque[float]] = {}  # oldest hit first

    def _recent(self, key: str, now: float) -> deque[float] | None:
        times = self._hits.get(key)
        while times and times[0] <= now - self.window:
            times.popleft()
        if times is not None and not times:
            del self._hits[key]
            return None
        return times

    def retry_after(self, key: str) -> int | None:
        """Whole seconds until ``key`` may go on; None while it is under the limit."""
        now = self.clock()
        times = self._recent(key, now)
        if times is None or len(times) < self.limit:
            return None
        return max(1, math.ceil(times[0] + self.window - now))

    def hit(self, key: str) -> None:
        """Count one hit of ``key``."""
        now = self.clock()
        times = self._recent(key, now) or deque(maxlen=self.limit)
        self._hits.pop(key, None)
        times.append(now)
        self._hits[key] = times  # moved to the end: hit most recently
        while len(self._hits) > self.max_keys:
            del self._hits[next(iter(self._hits))]


# KEYS[1] the window's sorted set; ARGV: limit, window in ms, 1 to count a hit, a member unique to the hit.
# Answers the milliseconds until the key may go on, 0 while it is under the limit.
_WINDOW_SCRIPT = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local limit, window = tonumber(ARGV[1]), tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if ARGV[3] == '1' then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  redis.call('ZREMRANGEBYRANK', KEYS[1], 0, -(limit + 1))
  redis.call('PEXPIRE', KEYS[1], window)
end
if redis.call('ZCARD', KEYS[1]) < limit then return 0 end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return math.max(1, tonumber(oldest[2]) + window - now)
"""


class Window:
    """A sliding window named ``name`` shared by the hub's processes on ``cache``'s Redis; ``local`` counts while the
    cache is in memory or Redis fails."""

    def __init__(self, cache: Cache, name: str, local: LocalWindow):
        self.cache, self.name, self.local = cache, name, local
        self._serial = 0

    @property
    def limit(self) -> int:
        return self.local.limit

    @property
    def window(self) -> float:
        return self.local.window

    async def retry_after(self, key: str) -> int | None:
        """Whole seconds until ``key`` may go on; None while it is under the limit."""
        return await self._run(key, hit=False)

    async def hit(self, key: str) -> None:
        """Count one hit of ``key``."""
        await self._run(key, hit=True)

    async def _run(self, key: str, *, hit: bool) -> int | None:
        client = self.cache._client() if self.cache.shared else None
        if client is not None:
            self._serial += 1
            member = f"{os.getpid()}:{id(self)}:{self._serial}:{time.time_ns()}"
            args = (self.limit, int(self.window * 1000), "1" if hit else "0", member)
            try:
                wait_ms = int(await client.eval(_WINDOW_SCRIPT, 1, f"{PREFIX}~rate:{self.name}:{key}", *args))
            except Exception as exc:
                self.cache._failed(exc)
            else:
                return math.ceil(wait_ms / 1000) if wait_ms > 0 else None
        if hit:
            self.local.hit(key)
            return None
        return self.local.retry_after(key)
