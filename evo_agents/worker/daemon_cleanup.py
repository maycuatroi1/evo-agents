"""The cleanup of a worker's daemon (``daemon``): at its start and every hour, the worktrees of the runs that ended go.

It asks the hub which ended runs whose worktrees it keeps had their work merged (POST /v1/worker/merged) and removes
those at once, after it committed what each worktree has not to a ref ``refs/evo-recovery/run-<id>/<folder>`` of its
checkout and noted it in recovery.jsonl; a run it holds, a parked run, one another run went on from and a directory a
run not ended uses stay. The worktrees of runs that ended more than 7 days ago go whatever the hub says; a parked run's
stay 30 days, for the run that resumes it. Only the evo-run branch of a run is deleted with its worktree.

The drive of the worktrees keeps ``WorkerConfig.disk_reserve_gb`` free (plan hub-steward, criterion a4). Below it, the
cleanup removes at once the worktrees of ended runs the merged ones may go of (``_removable``: not parked, not taken
over, not used by a run that has not ended), oldest end first and each one's uncommitted changes kept on its recovery
ref first, until the drive is back over the reserve and its headroom (``WorkerConfig.disk_headroom_gb``,
``home.disk_headroom`` of the reserve by default), so the free disk does not hover at the reserve (``make_room``); it
does so whenever the drive is under that target, so what the hub's band remembers of a dip (a heartbeat under the
reserve, a daemon restarted since) needs no memory of the daemon. The claims take no run while it stays below the
reserve (``daemon_claims``), and the hub gives the worker none either (``Claimer.stops``) until the drive is back over
the reserve and the headroom the heartbeat says, so a run waits or goes to another worker instead of failing at its
checkout; over the reserve with nothing more it may remove, the heartbeat says a headroom of 0 so the band turns ok.

Daemons of one machine whose homes lie side by side (``~/.evo/worker``, ``~/.evo/worker-<name>``) share a drive. When
the worktrees of its own ended runs are gone and the drive is still below the reserve, ``make_room`` goes on in the
sibling homes on the same drive (``_sibling_homes``): with each one's ``cleanup.lock`` taken without waiting, it removes
the worktrees of their ended runs, oldest end first, by the same rules and keeping each one's uncommitted changes on its
recovery ref and in that home's recovery.jsonl. A run that has not ended, a parked run, one a run went on from, and one
that ended within ``SIBLING_GRACE`` (which its own daemon may still hold) stay. A daemon takes its own home's lock while
it sweeps or makes room, so two never remove in one home at once.

The temporary directory of each run, its agent's TMPDIR (``WorkerHome.run_tmp``), goes once the run ended more than
``SIBLING_GRACE`` ago and is not parked (``sweep_tmp``), each hour and before ``make_room``.

The container engine of the machine (``stacks``) is the other thing a run leaves on the drive. Each worktree removed
takes the Compose stacks started from it along (``remove_worktree``). Each hour (``tend_stacks``), the running stacks
of this daemon's runs that ended more than ``SIBLING_GRACE`` ago are stopped, and the stacks started from a worktree
of a worker home on the machine that is gone are removed. Under the reserve and its headroom, ``make_room`` first
removes those and prunes the engine's caches (at most once every ``ENGINE_PRUNE_EVERY``, and only when the engine keeps
them on the drive of the worktrees), then goes on with the worktrees.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.worker import gitops, health, run_kinds, stacks
from evo_agents.worker.home import WorkerHome, disk_headroom
from evo_agents.worker.hubapi import HubProblem, Refused

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")

CLEANUP_EVERY = 3600.0
# gigabytes the worktrees' drive keeps free, over WorkerConfig.disk_reserve_gb: for an operator, and for tests whose
# machine has less free than the default
RESERVE_VARIABLE = "EVO_WORKER_DISK_RESERVE_GB"
KEEP_WORKTREES = timedelta(days=7)  # the sweep's: the worktrees of a run whose work merged go at the next cleanup
KEEP_PARKED = timedelta(days=30)  # the sweep's for a parked run, whose worktrees the run that resumes it takes over
SIBLING_GRACE = timedelta(minutes=15)  # a sibling's run that ended since may still be held by its own daemon
LOCK_NAME = "cleanup.lock"  # in a worker's home: held by whatever removes worktrees there
LOCK_WAIT = 60.0  # seconds a daemon waits for its own home's lock, which a sibling's making of room may hold
MAX_MERGED_ASKED = 200  # run ids in one POST /v1/worker/merged, as the hub takes them
ENGINE_PRUNE_EVERY = 600.0  # seconds between two prunes of the engine's caches while the drive stays short


def _paths(record: dict) -> list[Path]:
    """The directory and worktrees a run's record names."""
    found = [record.get("dir"), record.get("worktree")]
    found += [item.get("worktree") for item in record.get("repos") or [] if isinstance(item, dict)]
    return [Path(item) for item in found if isinstance(item, str) and item]


def _parked(record: dict) -> bool:
    return str(record.get("state") or "").startswith("parked")


def _old(record: dict, now: datetime) -> bool:
    """Whether the sweep removes a run's worktrees whatever the hub says: it ended 7 days ago, or 30 when parked."""
    return now - _finished(record, now) >= (KEEP_PARKED if _parked(record) else KEEP_WORKTREES)


def _finished(record: dict, now: datetime) -> datetime:
    """When a run's record says it ended, in UTC; ``now`` for one that has not, or says it unreadably."""
    try:
        finished = datetime.fromisoformat(str(record["finished_at"])) if record.get("finished_at") else now
    except ValueError:
        return now
    return finished.replace(tzinfo=UTC) if finished.tzinfo is None else finished


def _goes_on_from(spec: dict) -> set:
    """The runs whose worktrees a run of claim ``spec`` may adopt: the one it resumes and its lost attempt."""
    lost = spec.get("lost_attempt")
    lost = lost if isinstance(lost, dict) else {}
    ids = (spec.get("resume_of_run_id"), lost.get("run_id"))
    return {int(found) for found in ids if isinstance(found, int | str) and str(found).isdigit()}


def _try_lock(root: Path) -> int | None:
    """An open descriptor of ``root``'s ``cleanup.lock`` locked exclusively without waiting; None when another
    holds it or it cannot be opened."""
    import fcntl

    try:
        fd = os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlock(fd: int) -> None:
    os.close(fd)  # closing drops the lock


@contextlib.asynccontextmanager
async def _home_lock(root: Path, wait: float | None = None) -> AsyncIterator[bool]:
    """``root``'s ``cleanup.lock`` held for the block, waited for up to ``wait`` seconds; yields True when acquired and
    False when another process still holds it past ``wait``."""
    budget = LOCK_WAIT if wait is None else wait
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    fd = _try_lock(root)
    while fd is None and loop.time() < deadline and (root / LOCK_NAME).exists():
        await asyncio.sleep(min(0.2, max(0.01, budget / 2)))
        fd = _try_lock(root)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            _unlock(fd)


def _within(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except OSError:
        return False


def _sibling_removable(record: dict, records: list[dict], worktrees: Path, now: datetime) -> bool:
    """Whether the worktrees of ``record``'s run, in a sibling home whose runs are ``records`` and worktrees lie in
    ``worktrees``, may go to make room: it ended more than ``SIBLING_GRACE`` ago, is not parked, no run went on from
    it, each path its record names lies in ``worktrees`` and one of them is still there, and no run of that home that
    has not ended uses one of them."""
    if not record.get("finished_at") or record.get("resumed_by") or record.get("moved_to") or _parked(record):
        return False
    if now - _finished(record, now) < SIBLING_GRACE:
        return False
    mine = _paths(record)
    if not mine or not all(_within(path, worktrees) for path in mine) or not any(p.exists() for p in mine):
        return False
    for other in records:
        if other["id"] == record["id"] or other.get("finished_at"):
            continue
        if any(a == b or a in b.parents or b in a.parents for a in mine for b in _paths(other)):
            return False
    return True


class Cleanup:
    """The worktrees and records of the runs of ``daemon`` that ended, and what removes them."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.asks_merged = True  # until a hub before POST /v1/worker/merged answers 404
        self.removing = asyncio.Lock()  # one sweep or making of room at a time
        # ``make_room`` found nothing more to remove over the reserve and short of its headroom: the heartbeat says a
        # headroom of 0 while the drive stays there (``reported_headroom_gb``), and the claims do not ask again
        self.stalled = False
        self._engine: stacks.Engine | None = None
        self._engine_looked = False  # whether ``engine`` looked for one
        self._pruned_at: float | None = None  # the loop time of the last prune of the engine's caches

    @property
    def engine(self) -> stacks.Engine | None:
        """The container engine the cleanup tends (``stacks.Engine.find``), looked for once; None without one."""
        if not self._engine_looked:
            self._engine, self._engine_looked = stacks.Engine.find(self.daemon.env), True
        return self._engine

    @engine.setter
    def engine(self, value: stacks.Engine | None) -> None:
        self._engine, self._engine_looked = value, True

    async def loop(self) -> None:
        while True:
            try:
                await self.sweep_tmp()
                await self.sweep()
                await self.tend_stacks()
                await self.make_room()
            except Exception:
                log.exception("cleaning up old worktrees failed")
            await asyncio.sleep(CLEANUP_EVERY)

    async def sweep_tmp(self) -> list[int]:
        """Delete the temporary directory (``WorkerHome.run_tmp``) of each run of this daemon that ended more than
        ``SIBLING_GRACE`` ago and is not parked, whatever becomes of its worktrees; the ids of those deleted."""
        home, now = self.daemon.home, datetime.now(UTC)
        records = await asyncio.to_thread(home.load_runs)
        done = [
            record["id"]
            for record in records
            if record.get("finished_at")
            and not _parked(record)
            and now - _finished(record, now) > SIBLING_GRACE
            and home.run_tmp(record["id"]).exists()
        ]
        for run_id in done:
            await asyncio.to_thread(shutil.rmtree, home.run_tmp(run_id), True)
        if done:
            log.info("temporary directories of ended runs removed", extra={"runs": done})
        return done

    def free_gb(self) -> float | None:
        """The gigabytes free on the drive of the worktrees (``health.disk``); None when they cannot be read."""
        found = health.disk(self.daemon.home.worktrees_dir)
        return None if found is None else found["free_gb"]

    @property
    def reserve_gb(self) -> float:
        """The gigabytes the drive of the worktrees keeps free: $EVO_WORKER_DISK_RESERVE_GB when it holds a size of 0
        or more, else ``WorkerConfig.disk_reserve_gb``."""
        try:
            found = float(self.daemon.env.get(RESERVE_VARIABLE) or "nan")
        except ValueError:
            found = float("nan")
        return found if found >= 0 else float(self.daemon.config.disk_reserve_gb)

    @property
    def headroom_gb(self) -> float:
        """The gigabytes over ``reserve_gb`` ``make_room`` frees once the drive fell below the reserve:
        ``WorkerConfig.disk_headroom_gb``, else ``home.disk_headroom`` of the reserve."""
        headroom = self.daemon.config.disk_headroom_gb
        return float(headroom) if headroom is not None else disk_headroom(self.reserve_gb)

    @property
    def reported_headroom_gb(self) -> float:
        """The headroom the heartbeat says (``health.disk.headroom_gb``), over which the hub counts the disk ok again:
        ``headroom_gb``, or 0 while ``make_room`` stalled over the reserve and short of it with nothing more to remove,
        so a worker over its reserve is not held low by the hub for a headroom it cannot free. Under the reserve, or
        over the target, the stall is over."""
        free = self.free_gb()
        if free is None or not self.reserve_gb <= free < self.reserve_gb + self.headroom_gb:
            self.stalled = False
        return 0.0 if self.stalled else self.headroom_gb

    def under_target(self) -> bool:
        """Whether the drive of the worktrees has less free than ``reserve_gb`` and ``headroom_gb``, the free disk
        ``make_room`` goes on up to; not when it cannot be read."""
        free = self.free_gb()
        return free is not None and free < self.reserve_gb + self.headroom_gb

    def short_of_room(self) -> bool:
        """Whether the drive of the worktrees has less free than ``reserve_gb``; not when it cannot be read."""
        free = self.free_gb()
        return free is not None and free < self.reserve_gb

    async def make_room(self) -> list[int]:
        """While the drive has less free than the reserve and its headroom (``under_target``), remove the worktrees and
        records of the ended runs whose worktrees may go (``_removable``), the run that ended first first, each one's
        uncommitted changes kept on its recovery ref first (``_recover``); then, while still under, those of the
        sibling homes on the same drive (``_make_room_in_siblings``); the ids removed. Before the worktrees, what the
        container engine holds that nothing needs goes (``_engine_room``). Stalled over the reserve and short of the
        target, the heartbeat says a headroom of 0 (``_settle``). A parked run, one a run went on from, and a directory
        a run not ended uses stay, however short the drive."""
        if not self.under_target():
            self.stalled = False
            return []
        home = self.daemon.home
        removed: list[int] = []
        await self.sweep_tmp()
        async with self.removing:
            await self._engine_room()
            async with _home_lock(home.root) as locked:
                if locked:
                    now = datetime.now(UTC)
                    records = await asyncio.to_thread(home.load_runs)
                    ended = [r for r in records if self._removable(r, records) and any(p.exists() for p in _paths(r))]
                    for record in sorted(ended, key=lambda found: _finished(found, now)):
                        if not self.under_target():
                            break
                        run_id = record["id"]
                        found = self._still_ended(run_id)
                        if found is None or not await self._recover(found) or self._still_ended(run_id) is None:
                            continue
                        await self.remove_worktree(found)
                        await asyncio.to_thread(home.remove_run, run_id)
                        removed.append(run_id)
                        where = found.get("dir") or found.get("worktree")
                        log.info(
                            "worktree removed to keep the disk reserve",
                            extra={"run_id": run_id, "worktree": where},
                        )
                else:
                    log.info(
                        "own worker home cleanup lock is held: skipping own-home eviction",
                        extra={"home": str(home.root)},
                    )
            if self.under_target():
                removed += await self._make_room_in_siblings()
            self._settle()
        if removed or self.short_of_room():
            log.info(
                "made room on the worktrees' drive",
                extra={
                    "removed": removed,
                    "free_gb": self.free_gb(),
                    "reserve_gb": self.reserve_gb,
                    "headroom_gb": self.headroom_gb,
                },
            )
        return removed

    async def _engine_room(self) -> None:
        """While the drive is under the reserve and its headroom: remove the stacks started from worktrees that are
        gone (``_gone_stacks``), then prune the engine's caches (``stacks.Engine.prune``) when the engine keeps them on
        the drive of the worktrees, at most once every ENGINE_PRUNE_EVERY."""
        engine = self.engine
        if engine is None or not self.under_target():
            return
        found = await asyncio.to_thread(engine.containers)
        gone = await asyncio.to_thread(self._gone_stacks, found or [])
        if gone:
            done = await asyncio.to_thread(engine.remove, gone)
            log.info("stacks of worktrees that are gone removed to keep the disk reserve", extra=done)
        now = asyncio.get_running_loop().time()
        if not self.under_target() or (self._pruned_at is not None and now - self._pruned_at < ENGINE_PRUNE_EVERY):
            return
        try:
            device = self.daemon.home.worktrees_dir.stat().st_dev
        except OSError:
            return
        if not await asyncio.to_thread(stacks.on_drive, engine, device):
            return
        self._pruned_at = now
        freed = await asyncio.to_thread(engine.prune)
        log.info("the container engine's caches pruned to keep the disk reserve", extra={"freed_gb": freed})

    def _gone_stacks(self, found: list[stacks.Container]) -> list[stacks.Container]:
        """The containers of ``found`` whose stack was started from a directory under the worktrees of this daemon's
        home or of a sibling home on its drive, that directory is gone, and no directory or worktree a run of those
        homes names that is still there holds it."""
        homes = (self.daemon.home, *self._sibling_homes())
        roots = [home.worktrees_dir for home in homes]
        candidates = [
            item for item in found if stacks.within(item.working_dir, roots) and not item.working_dir.exists()
        ]
        if not candidates:
            return []
        kept = [path for home in homes for record in home.load_runs() for path in _paths(record) if path.exists()]
        return [item for item in candidates if not stacks.within(item.working_dir, kept)]

    async def tend_stacks(self) -> dict:
        """Stop the running stacks started from the worktrees of this daemon's runs that ended more than
        ``SIBLING_GRACE`` ago and whose worktrees may go once merged (``_removable``, ``_still_ended``), and remove the
        stacks of worktrees that are gone (``_gone_stacks``); {stopped, containers, images}."""
        engine = self.engine
        if engine is None:
            return {}
        found = await asyncio.to_thread(engine.containers)
        if not found:
            return {}
        done: dict = {}
        gone = await asyncio.to_thread(self._gone_stacks, found)
        if gone:
            done = await asyncio.to_thread(engine.remove, gone)
        now = datetime.now(UTC)
        records = await asyncio.to_thread(self.daemon.home.load_runs)
        ended = [
            path
            for record in records
            if self._removable(record, records)
            and now - _finished(record, now) >= SIBLING_GRACE
            and self._still_ended(record["id"]) is not None
            for path in _paths(record)
        ]
        idle = [item for item in found if item.running and item not in gone and stacks.within(item.working_dir, ended)]
        if idle:
            done["stopped"] = await asyncio.to_thread(engine.stop, idle)
        if done:
            log.info("stacks of ended runs tended", extra=done)
        return done

    async def _remove_stacks(self, paths: list[Path]) -> None:
        """Remove the stacks started from ``paths`` or a directory in them (``stacks.Engine.remove``)."""
        engine = self.engine
        if engine is None or not paths:
            return
        found = await asyncio.to_thread(engine.containers)
        mine = [item for item in found or [] if stacks.within(item.working_dir, paths)]
        if mine:
            done = await asyncio.to_thread(engine.remove, mine)
            log.info("stacks of a removed worktree removed", extra={**done, "worktrees": [str(p) for p in paths]})

    def engine_usage(self) -> dict | None:
        """What the container engine holds and how much of it nothing uses (``stacks.Engine.usage``) when it keeps
        them on the drive of the worktrees; None without an engine, or one elsewhere. Blocking: for a thread."""
        engine = self.engine
        if engine is None:
            return None
        try:
            device = self.daemon.home.worktrees_dir.stat().st_dev
        except OSError:
            return None
        return engine.usage() if stacks.on_drive(engine, device) else None

    def _settle(self) -> None:
        """After ``make_room`` removed what it may: stalled when the drive is over the reserve and short of its
        headroom, where the heartbeat then says a headroom of 0 (``reported_headroom_gb``); not when it is done or
        still under the reserve, where the band is low whatever the headroom."""
        free = self.free_gb()
        self.stalled = free is not None and self.reserve_gb <= free < self.reserve_gb + self.headroom_gb

    def _sibling_homes(self) -> list[WorkerHome]:
        """The other worker homes beside this daemon's (a directory named worker* in its parent with runs/ and
        worktrees/) whose worktrees lie on the drive of this daemon's worktrees."""
        own = self.daemon.home
        try:
            device = own.worktrees_dir.stat().st_dev
            parent = own.root.resolve().parent
            mine = own.root.resolve()
            entries = sorted(parent.iterdir())
        except OSError:
            return []
        found = []
        for entry in entries:
            with contextlib.suppress(OSError):
                if not entry.name.startswith("worker") or not entry.is_dir() or entry.resolve() == mine:
                    continue
                sibling = WorkerHome(entry)
                if not sibling.runs_dir.is_dir() or not sibling.worktrees_dir.is_dir():
                    continue
                if sibling.worktrees_dir.stat().st_dev == device:
                    found.append(sibling)
        return found

    async def _make_room_in_siblings(self) -> list[int]:
        """While the drive is still under the reserve and its headroom (``under_target``), remove the worktrees and
        records of the ended runs of the sibling homes (``_sibling_homes``) whose ``cleanup.lock`` it could take
        without waiting and whose worktrees may go (``_sibling_removable``), the run that ended first first across
        them, each one's uncommitted changes kept on its recovery ref and in that home's recovery.jsonl first; the ids
        removed."""
        held: list[tuple[WorkerHome, int]] = []
        removed: list[int] = []
        try:
            for sibling in self._sibling_homes():
                fd = _try_lock(sibling.root)
                if fd is None:
                    log.info("a sibling worker home is being cleaned up: left alone", extra={"home": str(sibling.root)})
                    continue
                held.append((sibling, fd))
            now = datetime.now(UTC)
            candidates: list[tuple[datetime, WorkerHome, dict]] = []
            for sibling, _fd in held:
                records = await asyncio.to_thread(sibling.load_runs)
                for record in records:
                    if _sibling_removable(record, records, sibling.worktrees_dir, now):
                        candidates.append((_finished(record, now), sibling, record))
            for _end, sibling, record in sorted(candidates, key=lambda found: found[0]):
                if not self.under_target():
                    break
                run_id = record["id"]
                found = sibling.load_run(run_id)  # read again: its own daemon may have resumed or adopted it since
                records = await asyncio.to_thread(sibling.load_runs)
                if found is None or not _sibling_removable(found, records, sibling.worktrees_dir, now):
                    continue
                if not await self._recover(found, sibling):
                    continue
                await self.remove_worktree(found, sibling)
                await asyncio.to_thread(sibling.remove_run, run_id)
                removed.append(run_id)
                where = found.get("dir") or found.get("worktree")
                log.info(
                    "worktree of a sibling worker removed to keep the disk reserve",
                    extra={"run_id": run_id, "worktree": where, "home": str(sibling.root)},
                )
        finally:
            for _sibling, fd in held:
                _unlock(fd)
        return removed

    async def sweep(self, now: datetime | None = None) -> list[int]:
        """Remove the worktrees and records of runs that ended more than 7 days ago (30 for a parked run, ``_old``), and
        those of ended runs whose work the hub says merged (``_merged_work``), each worktree's uncommitted changes
        kept first (``_recover``); their ids."""
        async with self.removing, _home_lock(self.daemon.home.root) as locked:
            if not locked:
                log.info(
                    "own worker home cleanup lock is held: skipping sweep",
                    extra={"home": str(self.daemon.home.root)},
                )
                return []
            return await self._sweep(now or datetime.now(UTC))

    async def _sweep(self, now: datetime) -> list[int]:
        home = self.daemon.home
        records = home.load_runs()
        old = {record["id"] for record in records if _old(record, now)}
        merged = await self._merged_work([record for record in records if record["id"] not in old], records)
        removed = []
        for record in records:
            run_id = record["id"]
            if run_id not in old and run_id not in merged:
                continue
            record = self._still_ended(run_id)  # read again: a run may have resumed or adopted it since
            if record is None:
                continue
            if run_id in old:
                await self.remove_worktree(record)
                log.info("old worktree removed", extra={"run_id": run_id, "worktree": record.get("worktree")})
            elif await self._recover(record) and self._still_ended(run_id) is not None:
                await self.remove_worktree(record)
                where = record.get("dir") or record.get("worktree")
                log.info("worktree of merged work removed", extra={"run_id": run_id, "worktree": where})
            else:
                continue
            await asyncio.to_thread(home.remove_run, run_id)
            removed.append(run_id)
        return removed

    def _still_ended(self, run_id: int) -> dict | None:
        """The record of run ``run_id`` as it is on disk now, while the cleanup may still remove what it names: the run
        ended, this daemon holds it no more, and no run this daemon holds resumes it or goes on from its lost attempt
        (a run that went on from it already took its worktrees out of the record, ``Conversation.adopt``). Else None."""
        held = self.daemon.runs
        record = self.daemon.home.load_run(run_id)
        if record is None or run_id in held or not record.get("finished_at"):
            return None
        if any(run_id in _goes_on_from(run.spec) for run in held.values()):
            return None
        return record

    async def _merged_work(self, young: list[dict], records: list[dict]) -> set[int]:
        """Of the runs of ``young`` (within their 7 days) whose worktrees this worker may remove (``_removable``, over
        all its ``records``), those the hub says the work of merged (POST /v1/worker/merged); none without a hub, from
        a hub before that route (asked once a daemon), or when it cannot answer now."""
        hub = self.daemon.hub
        if hub is None or not self.asks_merged:
            return set()
        asked = [record["id"] for record in young if self._removable(record, records)]
        found: set[int] = set()
        for start in range(0, len(asked), MAX_MERGED_ASKED):
            try:
                found |= set(await hub.merged(asked[start : start + MAX_MERGED_ASKED]))
            except Refused as exc:
                if exc.status in (404, 405):
                    self.asks_merged = False
                    log.info("the hub does not say which runs merged: their worktrees wait out the 7 days")
                else:
                    log.warning("the hub refused to say which runs merged", extra={"status": exc.status})
                break
            except HubProblem as exc:
                log.info("the hub did not say which runs merged; the next cleanup asks", extra={"error": str(exc)})
                break
        return found

    def _removable(self, record: dict, records: list[dict]) -> bool:
        """Whether ``record``'s run is one whose worktrees may go once its work merged: it ended and this daemon holds
        it no more, it is not parked, no run went on from it (``Conversation.adopt``, ``adopt_lost``), its record names
        a worktree, and no run that has not ended uses its directory or one of its worktrees."""
        held = self.daemon.runs
        if record["id"] in held or not record.get("finished_at") or record.get("resumed_by"):
            return False
        if _parked(record) or record.get("moved_to"):
            return False
        mine = _paths(record)
        if not mine:
            return False
        for other in records:
            if other["id"] == record["id"] or (other.get("finished_at") and other["id"] not in held):
                continue
            if any(a == b or a in b.parents or b in a.parents for a in mine for b in _paths(other)):
                return False
        return True

    async def _recover(self, record: dict, home: WorkerHome | None = None) -> bool:
        """Commit what each worktree of ``record``'s run has not committed to a ref of its checkout
        (``gitops.save_wip``, ``refs/evo-recovery/run-<id>/<folder>``) and add a line for it to the worker's recovery
        file (``WorkerHome.note_recovery``): the run, the repo, the branch, HEAD and the WIP commit. Whether every
        worktree was kept or had nothing to keep; the run's worktrees stay when one could not be. ``home`` is the worker
        home the run is of, this daemon's when None."""
        home = home or self.daemon.home
        run_id = record["id"]
        items = record.get("repos") if run_kinds.of(record.get("kind")).directory else [record]
        for item in items or []:
            if not isinstance(item, dict) or not item.get("worktree") or not item.get("checkout"):
                continue
            path, checkout = Path(item["worktree"]), Path(item["checkout"])
            if not path.is_dir() or not checkout.is_dir():
                continue
            ref = f"{gitops.RECOVERY_REFS}run-{int(run_id)}/{path.name}"
            try:
                async with self.daemon.repo_lock(checkout):
                    wip = await gitops.save_wip(path, ref, f"WIP of run {run_id}, kept before its worktree went")
            except gitops.GitError as exc:
                log.warning(
                    "the uncommitted changes of a merged run were not kept: its worktree stays",
                    extra={"run_id": run_id, "worktree": str(path), "error": str(exc)},
                )
                return False
            if wip is not None:
                home.note_recovery(
                    {
                        "run": run_id,
                        "repo": item.get("repo"),
                        "branch": item.get("local_branch") or item.get("branch"),
                        "head": wip.head,
                        "wip": wip.wip,
                        "ref": wip.ref,
                        "checkout": str(checkout),
                    }
                )
                log.info("uncommitted changes kept", extra={"run_id": run_id, "ref": wip.ref, "wip": wip.wip})
        return True

    async def remove_worktree(self, record: dict, home: WorkerHome | None = None) -> None:
        """Remove a run's worktree and its evo-run branch; for a plan run, each repo's, then the run's directory. A
        plan run whose worktrees a resumed run took over names none of them any more. The Compose stacks started from
        them go first (``_remove_stacks``). ``home`` is the worker home the run is of, this daemon's when None."""
        home = home or self.daemon.home
        try:
            await self._remove_stacks(_paths(record))
        except Exception:  # the engine never keeps a worktree
            log.exception("removing the stacks of a worktree failed", extra={"run_id": record.get("id")})
        if run_kinds.of(record.get("kind")).directory:
            for item in record.get("repos") or []:
                if isinstance(item, dict):
                    await self._remove_one(item, home)
            await self._remove_one({"worktree": record.get("dir")}, home)
        else:
            await self._remove_one(record, home)

    async def _remove_one(self, item: dict, home: WorkerHome) -> None:
        path = Path(item["worktree"]) if item.get("worktree") else None
        checkout = Path(item["checkout"]) if item.get("checkout") else None
        if checkout is not None and checkout.is_dir():
            async with self.daemon.repo_lock(checkout):
                if path is not None and path.exists():
                    with contextlib.suppress(gitops.GitError):
                        await gitops.remove_worktree(checkout, path)
                await gitops.prune_worktrees(checkout)
                branch = item.get("local_branch")
                if isinstance(branch, str) and branch.startswith("evo-run/"):
                    await gitops.delete_branch(checkout, branch)
        worktrees = home.worktrees_dir.resolve()
        if path is not None and path.exists() and path.resolve().is_relative_to(worktrees):
            await asyncio.to_thread(shutil.rmtree, path, True)
