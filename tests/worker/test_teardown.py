"""The end of a run on a full disk (``Run._wind_down``): every write of the spool, the log and the outputs raises
OSError, and still the run's leases go back, its record says finished_at and its branches are let go."""

import asyncio
import errno
import functools
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp", reason="the run needs the worker extra, evo-ak[worker]")

from evo_agents.worker import home as home_module  # noqa: E402
from evo_agents.worker.home import WorkerHome  # noqa: E402
from evo_agents.worker.runner.context import Run  # noqa: E402

RUN_ID = 31


def full_disk(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


async def full(*args, **kwargs):
    full_disk()


def stub_run(home: WorkerHome, done: list) -> SimpleNamespace:
    """What ``Run._wind_down`` and ``Run.event`` use of a run, each write of it failing as on a full disk."""

    async def release(**kwargs):
        done.append("leases")

    async def release_branch():
        done.append("branch")

    async def closed():
        done.append("browser")

    run = SimpleNamespace(
        id=RUN_ID,
        state="done",
        record={"id": RUN_ID, "kind": "step", "state": "done", "finished_at": None},
        daemon=SimpleNamespace(hard_stop=asyncio.Event(), home=home),
        _background=set(),
        agent=SimpleNamespace(terminal=SimpleNamespace(close_web=full), close_browser=closed),
        outputs=SimpleNamespace(close=full),
        credentials=SimpleNamespace(release=release),
        sender=SimpleNamespace(close=lambda: done.append("sender"), settled=True, poke=lambda: None),
        spool=SimpleNamespace(append=full_disk, remove=full_disk),
        kind=SimpleNamespace(release_branch=release_branch),
        watchdog=SimpleNamespace(saw=lambda kind, body: None),
        listeners=[],
        log_bytes=0,
        log_path=home.run_dir(RUN_ID) / "events.jsonl",
        spool_failed=0,
    )
    run._part = functools.partial(Run._part, run)
    run.event = functools.partial(Run.event, run)
    run.note = functools.partial(Run.note, run)
    return run


def test_disk_full_teardown_gives_the_leases_back_writes_finished_at_and_lets_the_branch_go(tmp_path, monkeypatch):
    home = WorkerHome(tmp_path / "worker")
    home.ensure()
    done: list = []
    run = stub_run(home, done)
    home.save_run(run.record)
    monkeypatch.setattr(home_module, "write_atomic", full_disk)  # no room for a temporary file either
    monkeypatch.setattr("builtins.open", full_disk)  # nor for the run's log

    async def go():
        run.note("the run ended")  # the spool and the log cannot take it: the event is lost, nothing raises
        await Run._wind_down(run, asyncio.create_task(asyncio.sleep(0)))

    asyncio.run(go())
    monkeypatch.undo()
    assert run.spool_failed == 1
    assert done == ["browser", "leases", "sender", "branch"]
    record = json.loads(home.run_dir(RUN_ID).joinpath("run.json").read_text(encoding="utf-8"))
    assert record["finished_at"] and record["state"] == "done", "the record was written in place"
