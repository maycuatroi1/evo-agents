"""The look at the machine before a heartbeat (``daemon_heartbeat.Heartbeat._look``): a look that takes longer than
LOOK_WAIT goes on in the background and the heartbeat goes without it, so the leases of the runs held never wait for a
runtime's --version that hangs or for threads busy with the runs' git."""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker import daemon_heartbeat, health, list_cli  # noqa: E402
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


# The health the heartbeat reports (plan hub-steward, criterion a3)


def _program(where, name: str, out: str, status: int = 0, wait: float = 0) -> None:
    """A fake runtime ``name`` in ``where`` that prints ``out`` and exits ``status``; it reads no credential."""
    script = where / name
    script.write_text(f"#!/bin/sh\nsleep {wait}\nprintf '%s' '{out}'\nexit {status}\n")
    script.chmod(0o755)


@pytest.mark.parametrize(
    ("runtime", "out", "status", "expected"),
    [
        ("claude-code", '{"loggedIn": true, "authMethod": "claude.ai"}', 0, "ok"),
        ("claude-code", '{"loggedIn": false}', 1, "unauthorized"),
        ("claude-code", "not json", 0, "unknown"),
        ("codex", "Logged in using ChatGPT", 0, "ok"),
        ("codex", "Not logged in", 1, "unauthorized"),
        ("opencode", "\x1b[90m0 credentials\x1b[0m\n1 environment variable", 0, "ok"),
        ("opencode", "0 credentials\n0 environment variables", 0, "unauthorized"),
        ("opencode", "something else", 0, "unknown"),
    ],
)
def test_a_runtimes_login_comes_from_its_own_status_command(tmp_path, runtime, out, status, expected):
    _program(tmp_path, health.BINARIES[runtime], out, status)
    assert health.login(runtime, path=str(tmp_path)) == expected


def test_a_runtime_not_on_path_is_missing_and_one_that_hangs_is_unknown(tmp_path):
    _program(tmp_path, "codex", "", 0, wait=1)
    assert health.login("claude-code", path=str(tmp_path)) == "missing"
    assert health.login("codex", path=str(tmp_path), timeout=0.2) == "unknown"
    assert set(health.logins(path=str(tmp_path))) <= set(health.BINARIES)


def test_the_heartbeat_carries_the_health_and_worker_list_shows_it(tmp_path):
    found = health.report(tmp_path / "worktrees" / "not-yet", {"claude-code": "ok", "codex": "unauthorized"})
    assert found["disk"]["free_gb"] >= 0 and 0 <= found["disk"]["free_pct"] <= 100
    daemon = SimpleNamespace(
        runtimes={},
        checkouts={},
        free_slots=1,
        says=lambda name: False,
        updater=SimpleNamespace(heartbeat_fields=dict),
        graphs=SimpleNamespace(report=lambda: None),
        browser=SimpleNamespace(report=None, asked=None),
        health=found,
    )
    assert Heartbeat(daemon).body([])["health"] == found
    assert "health" not in Heartbeat(SimpleNamespace(**{**vars(daemon), "health": None})).body([])
    worker = {"id": 7, "owner": "someone", "name": "mac-mini", "status": "online", "health": found}
    worker["health"]["disk"] = {"free_gb": 12.5, "free_pct": 4.0}
    line = list_cli.describe(worker, datetime.now(UTC))
    assert "disk 12.5 GB free (4%), login claude-code=ok codex=unauthorized" in line
    assert "disk" not in list_cli.describe({**worker, "health": None}, datetime.now(UTC))
