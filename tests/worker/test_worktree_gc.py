"""The disk reserve of a worker (plan hub-steward, criterion a4): under ``disk_reserve_gb`` free on the drive of its
worktrees, the daemon removes the worktrees of ended runs no parked or running run needs, oldest first and their
uncommitted changes kept on a recovery ref, claims no run while the drive stays short, and tells the hub its reserve;
a parked run's worktrees stay."""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

from evo_agents.hub import ops_events  # noqa: E402
from evo_agents.worker import daemon_cleanup  # noqa: E402
from evo_agents.worker.daemon import Daemon  # noqa: E402
from evo_agents.worker.home import DISK_RESERVE_GB, WorkerConfig, WorkerHome  # noqa: E402

GIT = {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b"}


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=_env())
    return done.stdout.strip()


def _env() -> dict:
    return {**os.environ, **GIT}


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch", "main", str(path)], check=True)
    (path / "README.md").write_text("x\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "--quiet", "-m", "x")
    return path


class _Drive:
    """A drive whose free gigabytes grow by ``per_worktree`` for each worktree of ``home`` removed."""

    def __init__(self, home: WorkerHome, free_gb: float, per_worktree: float):
        self.home, self.free, self.per = home, free_gb, per_worktree
        self.start = self._count()

    def _count(self) -> int:
        return sum(1 for _ in self.home.worktrees_dir.iterdir())

    def disk(self, _path) -> dict:
        return {"free_gb": self.free + self.per * (self.start - self._count()), "free_pct": 5.0}


def _stack(tmp_path: Path, reserve: float = DISK_RESERVE_GB) -> tuple[Path, WorkerHome, Daemon]:
    checkout = _checkout(tmp_path / "checkout")
    home = WorkerHome(tmp_path / "worker")
    home.ensure()
    config = WorkerConfig(url="https://hub.example.org", worker_id=1, name="mac", projects=["demo"])
    config.disk_reserve_gb = reserve
    env = {key: value for key, value in os.environ.items() if key != daemon_cleanup.RESERVE_VARIABLE}
    daemon = Daemon(home, config, "evw_x", adapters={}, env={**env, "EVO_WORKER_HEARTBEAT_SECONDS": "0.2"})
    return checkout, home, daemon


def _run(home: WorkerHome, checkout: Path, run_id: int, ended: timedelta | None, **extra) -> Path:
    path = home.worktree_path("demo", run_id)
    branch = f"evo-run/{run_id}"
    _git(checkout, "worktree", "add", "--quiet", "-b", branch, str(path))
    record = {"id": run_id, "checkout": str(checkout), "worktree": str(path), "local_branch": branch, **extra}
    if ended is not None:
        record["finished_at"] = (datetime.now(UTC) - ended).isoformat()
    home.save_run(record)
    return path


def test_under_its_reserve_the_daemon_removes_ended_worktrees_oldest_first_and_keeps_parked_and_running_ones(
    tmp_path, monkeypatch
):
    checkout, home, daemon = _stack(tmp_path)
    oldest = _run(home, checkout, 1, timedelta(hours=5))
    (oldest / "README.md").write_text("never committed\n", encoding="utf-8")
    _run(home, checkout, 2, timedelta(hours=3))
    _run(home, checkout, 3, timedelta(hours=1))  # the drive is over its reserve before this one's turn
    _run(home, checkout, 4, timedelta(days=2), state="parked")
    _run(home, checkout, 5, None)  # running
    drive = _Drive(home, 5.0, 10.0)
    monkeypatch.setattr(daemon_cleanup.health, "disk", drive.disk)

    assert asyncio.run(daemon.cleaner.make_room()) == [1, 2]
    assert not home.worktree_path("demo", 1).exists() and home.load_run(1) is None
    kept = (3, 4, 5)
    assert all(home.worktree_path("demo", run_id).is_dir() for run_id in kept), "newer, parked and running stay"
    ref = f"{daemon_cleanup.gitops.RECOVERY_REFS}run-1/{oldest.name}"
    assert _git(checkout, "show", f"{ref}:README.md") == "never committed", "the uncommitted change was kept"
    assert "evo-run/1" not in _git(checkout, "branch", "--list", "evo-run/*")
    assert asyncio.run(daemon.cleaner.make_room()) == [], "over the reserve, nothing more goes"


def test_a_daemon_short_of_its_reserve_claims_nothing_and_says_its_reserve_to_the_hub(tmp_path, monkeypatch):
    checkout, home, daemon = _stack(tmp_path, reserve=25)
    _run(home, checkout, 1, timedelta(days=1), state="parked")  # nothing it may remove
    monkeypatch.setattr(daemon_cleanup.health, "disk", _Drive(home, 3.0, 0.0).disk)

    async def held() -> bool:
        return await daemon.claims._held()

    assert daemon.cleaner.reserve_gb == 25
    assert asyncio.run(held()) is True, "under its reserve it claims nothing"
    assert home.worktree_path("demo", 1).is_dir(), "the parked run's worktree stays"
    report = daemon_cleanup.health.report(home.worktrees_dir, {}, daemon.cleaner.reserve_gb)
    assert report["disk"]["reserve_gb"] == 25 and report["disk"]["free_gb"] == 3.0

    monkeypatch.setattr(daemon_cleanup.health, "disk", _Drive(home, 30.0, 0.0).disk)
    assert asyncio.run(daemon.cleaner.make_room()) == []
    assert asyncio.run(daemon.claims._short_of_room()) is False, "back over its reserve it claims again"
    daemon.env[daemon_cleanup.RESERVE_VARIABLE] = "50"
    assert daemon.cleaner.reserve_gb == 50 and daemon.cleaner.short_of_room(), "the variable wins over the config"


def test_a_parked_runs_worktree_is_reserved_for_its_resume_past_the_7_days_and_goes_after_30(tmp_path):
    checkout, home, daemon = _stack(tmp_path)
    _run(home, checkout, 1, timedelta(days=8), state="parked")
    _run(home, checkout, 2, timedelta(days=31), state="parked")
    _run(home, checkout, 3, timedelta(days=8))
    assert asyncio.run(daemon.cleanup()) == [2, 3]
    assert home.worktree_path("demo", 1).is_dir() and home.load_run(1) is not None


def test_a_reserve_set_by_hand_that_is_not_a_size_falls_back_to_the_default_reserve():
    data = {"url": "https://hub.example.org", "worker_id": 1, "name": "mac", "projects": ["demo"]}
    assert WorkerConfig.from_json(data).disk_reserve_gb == DISK_RESERVE_GB
    assert WorkerConfig.from_json({**data, "disk_reserve_gb": 50}).disk_reserve_gb == 50
    for wrong in (-1, "lots", True, None):
        assert WorkerConfig.from_json({**data, "disk_reserve_gb": wrong}).disk_reserve_gb == DISK_RESERVE_GB


def test_below_its_reserve_the_daemon_makes_room_up_to_the_reserve_and_its_headroom_not_to_the_reserve_alone(
    tmp_path, monkeypatch
):
    checkout, home, daemon = _stack(tmp_path)
    for run_id, hours in ((1, 5), (2, 4), (3, 3)):
        _run(home, checkout, run_id, timedelta(hours=hours))
    monkeypatch.setattr(daemon_cleanup.health, "disk", _Drive(home, 26.0, 5.0).disk)
    assert daemon.cleaner.headroom_gb == 5.0, "the larger of 5 GB and a quarter of a 20 GB reserve"
    assert asyncio.run(daemon.cleaner.make_room()) == [], "over the reserve and its headroom: nothing goes"

    monkeypatch.setattr(daemon_cleanup.health, "disk", _Drive(home, 15.0, 5.0).disk)
    assert asyncio.run(daemon.cleaner.make_room()) == [1, 2], "15 to 20 is the reserve alone; it goes on to 25"
    assert home.worktree_path("demo", 3).is_dir()

    daemon.config.disk_reserve_gb = 40.0
    assert daemon.cleaner.headroom_gb == 10.0, "a quarter of a 40 GB reserve"
    daemon.config.disk_headroom_gb = 0.0
    assert daemon.cleaner.headroom_gb == 0.0, "the config's headroom wins"


def _beat(daemon: Daemon, before: dict | None) -> dict:
    """The health of ``daemon``'s heartbeat as the hub stores it, with its band (``ops_events.with_band``)."""
    cleaner = daemon.cleaner
    found = daemon_cleanup.health.report(
        daemon.home.worktrees_dir, {}, cleaner.reserve_gb, cleaner.reported_headroom_gb
    )
    return ops_events.with_band(found, before)


def test_a_cleanup_that_stalls_between_the_reserve_and_its_headroom_says_a_headroom_of_0_so_runs_come_again(
    tmp_path, monkeypatch
):
    """The drive dips under the reserve, the hub's band goes low, and the cleanup frees all it may but stops short of
    the reserve and its headroom: the heartbeat then says a headroom of 0, so the band turns ok and the worker is given
    runs again, also when the drive slips a little while it stays over the reserve. A later cleanup goes on to the
    target, and the full headroom is said again there."""
    checkout, home, daemon = _stack(tmp_path)
    _run(home, checkout, 1, timedelta(hours=5))
    drive = _Drive(home, 19.0, 3.0)
    monkeypatch.setattr(daemon_cleanup.health, "disk", drive.disk)
    cleaner = daemon.cleaner

    dipped = _beat(daemon, None)
    assert dipped["disk"]["band"] == "low" and ops_events.disk_band(dipped) == "low"
    assert asyncio.run(cleaner.make_room()) == [1], "19 to 22: the only ended run goes, short of 25"
    assert cleaner.stalled and cleaner.reported_headroom_gb == 0.0
    after = _beat(daemon, dipped)
    assert after["disk"]["free_gb"] == 22.0 and after["disk"]["band"] == "ok", "the hub gives it runs again"
    drive.free = 18.97  # running runs wrote 30 MB since
    assert _beat(daemon, after)["disk"]["band"] == "ok"

    _run(home, checkout, 2, timedelta(hours=1))  # a run ended since: the next cleanup goes on to the target
    monkeypatch.setattr(daemon_cleanup.health, "disk", _Drive(home, 22.0, 3.0).disk)
    assert asyncio.run(daemon.claims._short_of_room()) is False and home.worktree_path("demo", 2).is_dir()
    assert asyncio.run(cleaner.make_room()) == [2]
    assert not cleaner.stalled and cleaner.reported_headroom_gb == 5.0, "at 25 it is done; the full headroom again"


def test_a_dip_only_a_heartbeat_saw_ends_with_the_band_ok_once_the_claims_make_room(tmp_path, monkeypatch):
    """A running run's file takes the drive under the reserve for one heartbeat and goes before any cleanup ran: the
    band is low, and back over the reserve the claims make room up to the target, or say a headroom of 0 when nothing
    more may go, so the band turns ok."""
    checkout, home, daemon = _stack(tmp_path)
    _run(home, checkout, 1, timedelta(days=1), state="parked")  # nothing it may remove
    drive = _Drive(home, 18.0, 0.0)
    monkeypatch.setattr(daemon_cleanup.health, "disk", drive.disk)
    dipped = _beat(daemon, None)
    assert dipped["disk"]["band"] == "low"
    drive.free = 21.0
    assert _beat(daemon, dipped)["disk"]["band"] == "low", "between the reserve and its headroom the band holds"

    assert asyncio.run(daemon.claims._short_of_room()) is False
    assert daemon.cleaner.stalled and home.worktree_path("demo", 1).is_dir()
    assert _beat(daemon, dipped)["disk"]["band"] == "ok"


def test_a_daemon_restarted_between_the_reserve_and_its_headroom_makes_room_and_ends_with_the_band_ok(
    tmp_path, monkeypatch
):
    """After a dip, the drive is back at 22 GB and the daemon restarts: the new daemon has no memory of the dip, makes
    room from the free disk it reads, and the band the hub held low turns ok."""
    checkout, home, daemon = _stack(tmp_path)
    _run(home, checkout, 1, timedelta(hours=5))
    monkeypatch.setattr(daemon_cleanup.health, "disk", _Drive(home, 19.0, 0.0).disk)
    dipped = _beat(daemon, None)
    assert dipped["disk"]["band"] == "low"

    restarted = Daemon(home, daemon.config, "evw_x", adapters={}, env=daemon.env)
    monkeypatch.setattr(daemon_cleanup.health, "disk", _Drive(home, 22.0, 3.0).disk)
    assert asyncio.run(restarted.cleaner.make_room()) == [1], "22 to 25: the ended run goes"
    assert _beat(restarted, dipped)["disk"]["band"] == "ok"


def test_a_headroom_set_by_hand_that_is_not_a_size_falls_back_to_the_default_headroom():
    data = {"url": "https://hub.example.org", "worker_id": 1, "name": "mac", "projects": ["demo"]}
    assert WorkerConfig.from_json(data).disk_headroom_gb is None
    assert WorkerConfig.from_json({**data, "disk_headroom_gb": 8}).disk_headroom_gb == 8
    for wrong in (-1, "lots", True):
        assert WorkerConfig.from_json({**data, "disk_headroom_gb": wrong}).disk_headroom_gb is None


class _SharedDrive:
    """A drive shared by worker homes, whose free gigabytes grow by ``per_worktree`` for each worktree removed."""

    def __init__(self, homes: list[WorkerHome], free_gb: float, per_worktree: float):
        self.homes, self.free, self.per = homes, free_gb, per_worktree
        self.start = self._count()

    def _count(self) -> int:
        return sum(1 for home in self.homes for _ in home.worktrees_dir.iterdir())

    def disk(self, _path) -> dict:
        return {"free_gb": self.free + self.per * (self.start - self._count()), "free_pct": 5.0}


def _sibling(tmp_path: Path, name: str = "worker-other") -> WorkerHome:
    home = WorkerHome(tmp_path / name)
    home.ensure()
    return home


def test_with_nothing_of_its_own_left_to_remove_the_daemon_makes_room_in_sibling_homes_oldest_first(
    tmp_path, monkeypatch
):
    checkout, home, daemon = _stack(tmp_path)
    sibling = _sibling(tmp_path)
    _run(home, checkout, 1, timedelta(hours=6))
    oldest = _run(sibling, checkout, 10, timedelta(hours=5))
    (oldest / "README.md").write_text("never committed in a sibling\n", encoding="utf-8")
    _run(sibling, checkout, 11, timedelta(hours=3))  # the drive is over its reserve before this one's turn
    drive = _SharedDrive([home, sibling], 5.0, 10.0)
    monkeypatch.setattr(daemon_cleanup.health, "disk", drive.disk)

    assert asyncio.run(daemon.cleaner.make_room()) == [1, 10], "its own first, then the sibling's oldest"
    assert not sibling.worktree_path("demo", 10).exists() and sibling.load_run(10) is None
    assert sibling.worktree_path("demo", 11).is_dir() and sibling.load_run(11) is not None
    ref = f"{daemon_cleanup.gitops.RECOVERY_REFS}run-10/{oldest.name}"
    assert _git(checkout, "show", f"{ref}:README.md") == "never committed in a sibling"
    assert '"run": 10' in sibling.recovery_path.read_text(encoding="utf-8"), "noted in the sibling's recovery file"
    assert "evo-run/10" not in _git(checkout, "branch", "--list", "evo-run/*")


def test_running_parked_adopted_and_just_ended_runs_of_a_sibling_home_stay_however_short_the_drive(
    tmp_path, monkeypatch
):
    checkout, home, daemon = _stack(tmp_path)
    sibling = _sibling(tmp_path)
    _run(sibling, checkout, 10, timedelta(hours=5))
    _run(sibling, checkout, 12, timedelta(days=2), state="parked")
    _run(sibling, checkout, 13, None)  # running
    _run(sibling, checkout, 14, timedelta(days=1), resumed_by=20)  # a run went on from it
    _run(sibling, checkout, 15, timedelta(days=1), moved_to="/elsewhere")
    _run(sibling, checkout, 16, timedelta(minutes=1))  # its own daemon may still hold it
    shared = sibling.worktree_path("demo", 13) / "inner"
    shared.mkdir()
    sibling.save_run(
        {"id": 17, "worktree": str(shared), "finished_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    )
    other = tmp_path / "elsewhere"
    other.mkdir()
    sibling.save_run({"id": 18, "worktree": str(other), "finished_at": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(daemon_cleanup.health, "disk", _SharedDrive([home, sibling], 5.0, 0.0).disk)

    assert asyncio.run(daemon.cleaner.make_room()) == [10]
    for run_id in (12, 13, 14, 15, 16):
        assert sibling.worktree_path("demo", run_id).is_dir() and sibling.load_run(run_id) is not None, run_id
    assert shared.is_dir() and sibling.load_run(17) is not None, "inside a running run's worktree"
    assert other.is_dir() and sibling.load_run(18) is not None, "outside the sibling's worktrees"


def test_a_sibling_home_whose_cleanup_lock_is_held_or_that_is_not_a_worker_home_is_left_alone(tmp_path, monkeypatch):
    checkout, home, daemon = _stack(tmp_path)
    locked = _sibling(tmp_path, "worker-busy")
    stranger = _sibling(tmp_path, "notes")
    _run(locked, checkout, 10, timedelta(hours=5))
    _run(stranger, checkout, 11, timedelta(hours=5))
    monkeypatch.setattr(daemon_cleanup.health, "disk", _SharedDrive([home, locked, stranger], 5.0, 0.0).disk)

    fd = daemon_cleanup._try_lock(locked.root)
    assert fd is not None
    try:
        assert asyncio.run(daemon.cleaner.make_room()) == []
    finally:
        daemon_cleanup._unlock(fd)
    assert locked.worktree_path("demo", 10).is_dir() and stranger.worktree_path("demo", 11).is_dir()
    assert asyncio.run(daemon.cleaner.make_room()) == [10], "once the lock is free it goes"
    assert stranger.worktree_path("demo", 11).is_dir()


def test_own_home_cleanup_skips_when_cleanup_lock_stays_held_past_wait(tmp_path, monkeypatch):
    checkout, home, daemon = _stack(tmp_path)
    _run(home, checkout, 1, timedelta(days=8))
    monkeypatch.setattr(daemon_cleanup, "LOCK_WAIT", 0.02)
    monkeypatch.setattr(daemon_cleanup.health, "disk", _SharedDrive([home], 5.0, 10.0).disk)

    fd = daemon_cleanup._try_lock(home.root)
    assert fd is not None
    try:
        assert asyncio.run(daemon.cleaner.sweep()) == []
        assert asyncio.run(daemon.cleaner.make_room()) == []
        assert home.worktree_path("demo", 1).is_dir() and home.load_run(1) is not None
    finally:
        daemon_cleanup._unlock(fd)
    assert asyncio.run(daemon.cleaner.make_room()) == [1]
