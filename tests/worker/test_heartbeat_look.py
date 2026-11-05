"""The look at the machine before a heartbeat (``daemon_heartbeat.Heartbeat._look``): a look that takes longer than
LOOK_WAIT goes on in the background and the heartbeat goes without it, so the leases of the runs held never wait for a
runtime's --version that hangs or for threads busy with the runs' git."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker import daemon_heartbeat  # noqa: E402
from evo_agents.worker.daemon_heartbeat import Heartbeat  # noqa: E402


class Machine:
    """A daemon whose look at the machine waits for ``release``, or raises ``error``; it counts the looks."""

    def __init__(self, error: Exception | None = None):
        self.release = asyncio.Event()
        self.error = error
        self.looks = 0

    async def look_at_machine(self) -> None:
        self.looks += 1
        if self.error is not None:
            raise self.error
        await self.release.wait()


@pytest.fixture(autouse=True)
def _short_wait(monkeypatch):
    monkeypatch.setattr(daemon_heartbeat, "LOOK_WAIT", 0.05)


def test_a_slow_look_leaves_the_heartbeat_on_time_and_is_not_started_twice(caplog):
    async def scenario():
        daemon = Machine()
        heartbeat = Heartbeat(SimpleNamespace(look_at_machine=daemon.look_at_machine))
        started = asyncio.get_running_loop().time()
        await heartbeat._look()
        await heartbeat._look()
        waited = asyncio.get_running_loop().time() - started
        looks_while_slow = daemon.looks
        daemon.release.set()
        await asyncio.sleep(0)
        await heartbeat._look()
        await heartbeat._look()
        return waited, looks_while_slow, daemon.looks

    with caplog.at_level(logging.WARNING, logger="evo_agents.worker"):
        waited, looks_while_slow, looks = asyncio.run(scenario())
    assert waited < 1.0
    assert looks_while_slow == 1, "the second heartbeat waits for the look already running"
    assert looks == 3, "once it ended, each heartbeat starts a new look"
    slow = [r for r in caplog.records if r.getMessage().startswith("looking at the machine takes long")]
    assert len(slow) == 2


def test_a_look_that_fails_is_logged_and_the_heartbeat_goes_on(caplog):
    async def scenario():
        daemon = Machine(error=OSError("no such file: claude"))
        heartbeat = Heartbeat(SimpleNamespace(look_at_machine=daemon.look_at_machine))
        await heartbeat._look()
        return daemon.looks

    with caplog.at_level(logging.ERROR, logger="evo_agents.worker"):
        assert asyncio.run(scenario()) == 1
    (record,) = [r for r in caplog.records if r.getMessage() == "looking at the runtimes and checkouts failed"]
    assert isinstance(record.exc_info[1], OSError)
