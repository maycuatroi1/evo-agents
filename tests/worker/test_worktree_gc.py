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

from evo_agents.worker import daemon_cleanup
from evo_agents.worker.home import DISK_RESERVE_GB, WorkerConfig, WorkerHome

pytest.importorskip("aiohttp")

from evo_agents.worker.daemon import Daemon  # noqa: E402

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
