"""The login session on the worker (plan agent-browser, criterion a7), with a stand-in for Chromium: the kind is one the
daemon names in its heartbeat (so an old worker never claims it), a session waits for Save, and only Save sends the
profile; cancel and the timeout send nothing."""

from __future__ import annotations

import asyncio
import types

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import browser_login, runs
from evo_agents.worker.runner.browser_login import BrowserLoginRun
from evo_agents.worker.runner.common import RunFailed
from evo_agents.worker.runner.kinds import KINDS


class Reports:
    def __init__(self):
        self.states: list[str] = []
        self.ended: list[str] = []

    async def report(self, state, **_):
        self.states.append(state)

    async def end(self, state, **_):
        self.ended.append(state)


def stand_in(*, sent: list[int]):
    """A Run with a browser whose profile save sends ``sent`` and which a test stops or finishes."""
    saved: list[str] = []
    profiles = types.SimpleNamespace(login_url=None, sent=[])

    async def open_browser():
        return "http://127.0.0.1:1"

    async def save_browser():
        saved.append("saved")
        profiles.sent.extend(sent)

    run = types.SimpleNamespace(
        id=7,
        project="p",
        title="",
        spec={"title": "Log in browser identity work"},
        timeout_s=browser_login.TIMEOUT_MINUTES * 60,
        daemon=types.SimpleNamespace(config=types.SimpleNamespace(name="w")),
        finish_asked=asyncio.Event(),
        stop_event=asyncio.Event(),
        reports=Reports(),
        note=lambda _text: None,
        check=lambda: None,
        agent_starts=lambda: None,
        agent=types.SimpleNamespace(
            open_browser=open_browser,
            save_browser=save_browser,
            browser=types.SimpleNamespace(profiles=profiles, cdp=None, socket=None),
        ),
    )
    return run, saved


def test_browser_login_session_kind_is_registered_so_a_worker_without_it_never_claims_one():
    assert KINDS["browser"] is BrowserLoginRun and "browser" in runs.RUN_KINDS
    assert BrowserLoginRun.parks is False and BrowserLoginRun.chat is True


def test_browser_login_session_save_sends_the_profile_and_ends_done():
    async def body():
        run, saved = stand_in(sent=[3])
        kind = BrowserLoginRun(run)
        assert run.title == "Log in browser identity work" and kind.saves_browser is False
        task = asyncio.ensure_future(kind.steps())
        await asyncio.sleep(0.1)
        assert run.reports.states == ["interactive"] and not task.done()
        run.finish_asked.set()
        await asyncio.wait_for(task, 5)
        assert kind.saves_browser is True and saved == ["saved"]
        assert run.reports.states == ["interactive", "verifying"] and run.reports.ended == ["done"]

    asyncio.run(body())


def test_browser_login_session_save_that_sent_nothing_fails_with_cause_environment():
    async def body():
        run, _ = stand_in(sent=[])
        run.finish_asked.set()
        with pytest.raises(RunFailed) as failed:
            await BrowserLoginRun(run).steps()
        assert failed.value.fields["cause"] == "environment" and run.reports.ended == []

    asyncio.run(body())


def test_browser_login_session_cancel_saves_nothing():
    async def body():
        run, saved = stand_in(sent=[3])
        kind = BrowserLoginRun(run)
        stopped = []

        def check():
            if run.stop_event.is_set():
                stopped.append(1)
                raise asyncio.CancelledError

        run.check = check
        task = asyncio.ensure_future(kind.steps())
        await asyncio.sleep(0.1)
        run.stop_event.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert saved == [] and kind.saves_browser is False and run.reports.ended == []

    asyncio.run(body())
