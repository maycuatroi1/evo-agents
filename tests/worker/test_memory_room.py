"""The memory gate of a worker's claims (``memory_room``): a box whose daemons share one cgroup limit claims nothing
while that limit stalls, and says no free slot to the hub meanwhile."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from evo_agents.worker import memory_room
from evo_agents.worker.home import WorkerConfig
from evo_agents.worker.memory_room import MB, Room, read_room, short_of

SLICE = "user.slice/user-1000.slice/user@1000.service/workers.slice"
SERVICE = f"{SLICE}/worker-one.service"


def _box(
    tmp_path: Path,
    *,
    high: str = str(8 * 1024 * MB),
    current: int = 8 * 1024 * MB - 100 * MB,
    full_avg60: float = 60.0,
    available_kb: int = 4 * 1024 * 1024,
    v2: bool = True,
) -> tuple[Path, Path]:
    """A fake /sys/fs/cgroup and /proc: this process in SERVICE under SLICE, which holds the limit."""
    cgroup, proc = tmp_path / "cgroup", tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "pressure").mkdir()
    (proc / "self" / "cgroup").write_text(f"0::/{SERVICE}\n" if v2 else "1:name=systemd:/x\n", encoding="utf-8")
    (proc / "meminfo").write_text(f"MemTotal: 15000000 kB\nMemAvailable: {available_kb} kB\n", encoding="utf-8")
    (proc / "pressure" / "memory").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=2.00 avg300=0.00 total=0\n",
        encoding="utf-8",
    )
    service, limited = cgroup / SERVICE, cgroup / SLICE
    service.mkdir(parents=True)
    (service / "memory.high").write_text("max\n", encoding="utf-8")
    (service / "memory.max").write_text("max\n", encoding="utf-8")
    (service / "memory.current").write_text(f"{current // 2}\n", encoding="utf-8")
    (limited / "memory.high").write_text(f"{high}\n", encoding="utf-8")
    (limited / "memory.max").write_text(f"{9 * 1024 * MB}\n", encoding="utf-8")
    (limited / "memory.current").write_text(f"{current}\n", encoding="utf-8")
    (limited / "memory.pressure").write_text(
        f"some avg10=1.00 avg60={full_avg60} avg300=0.00 total=1\nfull avg10=1.00 avg60={full_avg60} avg300=0.00 total=1\n",
        encoding="utf-8",
    )
    return cgroup, proc


def test_a_slice_at_its_limit_that_stalls_holds_claims_back(tmp_path):
    cgroup, proc = _box(tmp_path)
    room = read_room(cgroup, proc)
    assert room.where == f"/{SLICE}" and round(room.room_mb) == 100 and room.stall == 60.0
    why = short_of(room, 1024, 10)
    assert why is not None and "stalls 60%" in why and SLICE in why


def test_a_slice_near_its_limit_that_does_not_stall_still_takes_runs(tmp_path):
    """memory.current counts page cache, which the kernel gives back: near the limit without a stall is not full."""
    cgroup, proc = _box(tmp_path, full_avg60=0.0)
    room = read_room(cgroup, proc)
    assert round(room.room_mb) == 100 and short_of(room, 1024, 10) is None
    stalling = Room(room.room_mb, 3.0, room.where)
    assert "100 MB left" in short_of(stalling, 1024, 10)  # under the floor and stalling a little: held back


def test_the_lower_of_memory_high_and_memory_max_is_the_limit(tmp_path):
    cgroup, proc = _box(tmp_path, high="max", current=8 * 1024 * MB)
    assert round(read_room(cgroup, proc).room_mb) == 1024  # memory.max 9 GiB less 8 GiB


def test_without_cgroup_v2_the_machine_is_read(tmp_path):
    cgroup, proc = _box(tmp_path, v2=False, available_kb=500 * 1024)
    room = read_room(cgroup, proc)
    assert room == Room(500.0, 2.0, "machine")
    assert "500 MB left in machine" in short_of(room, 1024, 10)


def test_a_machine_that_reads_nothing_holds_nothing_back_and_zero_turns_each_test_off(tmp_path):
    room = read_room(tmp_path / "none", tmp_path / "none")
    assert room == Room(None, None, None) and short_of(room, 1024, 10) is None
    cgroup, proc = _box(tmp_path)
    assert short_of(read_room(cgroup, proc), 0, 0) is None


def test_the_config_takes_the_settings_and_keeps_the_defaults_for_values_that_are_not_sizes():
    base = {"url": "https://hub.example.org", "worker_id": 1, "name": "box", "projects": ["demo"]}
    config = WorkerConfig.from_json(base)
    assert (config.memory_floor_mb, config.memory_stall_max) == (
        memory_room.MEMORY_FLOOR_MB,
        memory_room.MEMORY_STALL_MAX,
    )
    config = WorkerConfig.from_json({**base, "memory_floor_mb": 0, "memory_stall_max": 25})
    assert (config.memory_floor_mb, config.memory_stall_max) == (0, 25)
    config = WorkerConfig.from_json({**base, "memory_floor_mb": "lots", "memory_stall_max": -1})
    assert (config.memory_floor_mb, config.memory_stall_max) == (
        memory_room.MEMORY_FLOOR_MB,
        memory_room.MEMORY_STALL_MAX,
    )


def test_a_daemon_whose_memory_stalls_claims_nothing_and_says_no_free_slot(tmp_path, monkeypatch, caplog):
    pytest.importorskip("aiohttp")
    from evo_agents.worker.daemon import Daemon
    from evo_agents.worker.home import WorkerHome

    home = WorkerHome(tmp_path / "worker")
    home.ensure()
    config = WorkerConfig(url="https://hub.example.org", worker_id=1, name="box", projects=["demo"], slots=2)
    env = {k: v for k, v in os.environ.items() if not k.startswith("EVO_WORKER_MEMORY_")}  # the config's defaults
    daemon = Daemon(home, config, "evw_x", adapters={}, env={**env, "EVO_WORKER_HEARTBEAT_SECONDS": "0.2"})
    stalled = Room(80.0, 60.0, f"/{SLICE}")
    monkeypatch.setattr(memory_room, "read_room", lambda: stalled)

    assert asyncio.run(daemon.claims._short_of_memory()) is True
    assert daemon.heartbeat.body([])["free_slots"] == 0
    assert asyncio.run(daemon.claims._short_of_memory()) is True
    assert sum("no claims" in record.getMessage() for record in caplog.records) == 1  # said once a hold

    monkeypatch.setattr(memory_room, "read_room", lambda: Room(4000.0, 0.0, "machine"))
    assert asyncio.run(daemon.claims._short_of_memory()) is False
    assert daemon.heartbeat.body([])["free_slots"] == 2

    monkeypatch.setattr(memory_room, "read_room", lambda: stalled)
    daemon.env["EVO_WORKER_MEMORY_STALL_MAX"] = "0"  # the variables win over the config, each turning its test off
    assert asyncio.run(daemon.claims._short_of_memory()) is True  # 80 MB left, under the floor, and stalling
    daemon.env["EVO_WORKER_MEMORY_FLOOR_MB"] = "0"
    assert asyncio.run(daemon.claims._short_of_memory()) is False
