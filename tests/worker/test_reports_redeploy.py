"""A run's reports (``runner.reports``) while the hub is redeployed: the proxy in front of it answers 404 for a moment
before the new hub takes requests. That 404 is not the hub's, so the report is sent again until the hub takes it;
only the hub's own 404 says the run is not this worker's. Runs #119 and #483 were dropped by a proxy's 404 and ended
lost while their agents worked."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp", reason="the reports need the worker extra, evo-ak[worker]")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from evo_agents.worker.hubapi import Backoff, WorkerHub  # noqa: E402
from evo_agents.worker.runner import reports as reports_module  # noqa: E402
from evo_agents.worker.runner.common import ReportRefused  # noqa: E402
from evo_agents.worker.runner.reports import Reports  # noqa: E402
from tests.worker.test_sender import RUN_ID, bad_gateway, hub_not_found, proxy_not_found  # noqa: E402


class StateHub:
    """``POST /v1/worker/runs/{id}/state``: each call takes the next of ``answers``; with none left, it takes the
    move and answers the run's new state."""

    def __init__(self, answers: list):
        self.answers = list(answers)
        self.calls: list[str] = []
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/worker/runs/{id}/state", self._state)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _state(self, request: web.Request) -> web.Response:
        state = (await request.json())["state"]
        self.calls.append(state)
        if self.answers:
            return self.answers.pop(0)()
        return web.json_response({"id": RUN_ID, "state": state})


async def report(hub: StateHub, state: str) -> tuple[dict, SimpleNamespace, list]:
    """Report ``state`` of run RUN_ID to ``hub``: the hub's answer, the run, and what reached the daemon's fatal."""
    fatals: list = []
    url = await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            daemon = SimpleNamespace(
                hub=WorkerHub(url, "evw_" + "f" * 43, session),
                hard_stop=asyncio.Event(),
                fatal=fatals.append,
                home=SimpleNamespace(save_run=lambda record: None),
            )
            run = SimpleNamespace(id=RUN_ID, daemon=daemon, state="leased", record={"id": RUN_ID})
            answer = await asyncio.wait_for(Reports(run).report(state), 10)
    finally:
        await hub.stop()
    return answer, run, fatals


@pytest.fixture(autouse=True)
def _quick_backoff(monkeypatch):
    monkeypatch.setattr(reports_module, "Backoff", lambda: Backoff(first=0.01, most=0.05))


def test_a_502_then_a_404_of_the_proxy_send_the_report_again_until_the_hub_takes_it(caplog):
    hub = StateHub([bad_gateway, proxy_not_found, proxy_not_found])
    with caplog.at_level(logging.WARNING, logger="evo_agents.worker"):
        answer, run, fatals = asyncio.run(report(hub, "running"))
    assert answer == {"id": RUN_ID, "state": "running"}
    assert (run.state, run.record["state"]) == ("running", "running")
    assert hub.calls == ["running"] * 4
    assert [record.message for record in caplog.records].count("report not sent; trying again") == 3
    assert not fatals


def test_the_hubs_own_404_refuses_the_report():
    hub = StateHub([hub_not_found])
    with pytest.raises(ReportRefused) as refused:
        asyncio.run(report(hub, "running"))
    assert refused.value.status == 404
    assert hub.calls == ["running"]
