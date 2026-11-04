"""A verify command that starts a server in the background (``cmd &``) leaves nothing running once it exited: what it
left is killed, so no port stays taken and the output ends, both for the verify of ``evo-agents worker step``
(``step_verify.verify``) and for the one the daemon runs again at the end of a run (``runner.verify.shell``)."""

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from evo_agents.worker import step_verify

LEAVE = "sleep 300 & echo $! > leftover.pid; (setsid sleep 300 & echo $! > escaped.pid) ; echo started"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:  # reaped by init, or a zombie of nobody's
        return handle.read().rsplit(")", 1)[1].split()[0] != "Z"


def pids(path) -> list[int]:
    return [int((path / name).read_text()) for name in ("leftover.pid", "escaped.pid")]


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")
def test_verify_leftover_of_worker_step_is_killed_and_the_command_returns(tmp_path):
    started = time.monotonic()
    results = asyncio.run(asyncio.wait_for(step_verify.verify(tmp_path, [LEAVE, "true"]), 30))
    assert [item["exit_code"] for item in results] == [0, 0]
    assert time.monotonic() - started < 15, "the output of the command ended with it"
    assert not any(alive(pid) for pid in pids(tmp_path)), "the server it started in the background is gone"


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")
def test_verify_leftover_of_the_runs_verify_is_killed(tmp_path):
    pytest.importorskip("aiohttp", reason="the run needs the worker extra, evo-ak[worker]")
    from evo_agents.worker.runner import verify

    async def go():
        run = SimpleNamespace(
            id=7, worktree=tmp_path, stop_event=asyncio.Event(), agent=SimpleNamespace(env=lambda: dict(os.environ))
        )
        return await verify.shell(run, LEAVE, 30)

    code, output = asyncio.run(asyncio.wait_for(go(), 30))
    assert code == 0 and "started" in output
    assert not any(alive(pid) for pid in pids(tmp_path))
