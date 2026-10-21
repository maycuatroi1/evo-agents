"""The events of a run on their way to the hub (``runner.sender``) while the hub is redeployed: the proxy in front of
it answers 502 for a few seconds, then 404 for a moment before the new hub takes requests. Neither is the hub's
answer, so the events wait in the spool and are sent again from the last ack; only the hub's own 404 (its
``{error, message}`` body: the run is not this worker's) ends the sending and drops what is left."""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp", reason="the sender needs the worker extra, evo-ak[worker]")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from evo_agents.worker.hubapi import Backoff, WorkerHub  # noqa: E402
from evo_agents.worker.runner import sender as sender_module  # noqa: E402
from evo_agents.worker.runner.sender import Sender  # noqa: E402
from evo_agents.worker.spool import Spool, SpoolBudget  # noqa: E402

RUN_ID = 143
AT = datetime(2026, 10, 18, 12, 28, 21, tzinfo=UTC)


def bad_gateway() -> web.Response:
    return web.Response(status=502, text="Bad Gateway")


def proxy_not_found() -> web.Response:
    """What the proxy answers while no hub is registered behind it: plain text, not the hub's error body."""
    return web.Response(status=404, text="404 page not found")


def hub_not_found() -> web.Response:
    return web.json_response(
        {"error": "not_found", "message": f"this worker did not claim run {RUN_ID}", "request_id": "r1"}, status=404
    )


class EventsHub:
    """``POST /v1/worker/runs/{id}/events``: each call takes the next of ``answers``; with none left, it stores the
    events that follow the ones stored, as the hub does, and answers ``ack_seq``."""

    def __init__(self, answers: list):
        self.answers = list(answers)
        self.stored: list[int] = []
        self.firsts: list[int] = []  # the first seq of each batch the worker sent
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/worker/runs/{id}/events", self._events)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self.url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _events(self, request: web.Request) -> web.Response:
        events = (await request.json())["events"]
        self.firsts.append(events[0]["seq"])
        if self.answers:
            return self.answers.pop(0)()
        for event in sorted(events, key=lambda item: item["seq"]):
            if event["seq"] == len(self.stored) + 1:
                self.stored.append(event["seq"])
        return web.json_response({"ack_seq": len(self.stored), "stored": len(self.stored)})


async def eventually(check, what: str, seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


async def send(tmp_path, hub: EventsHub, script) -> tuple[Sender, list]:
    """Run a Sender for a spool of run RUN_ID against ``hub`` while ``script(sender, spool)`` writes events; close
    it and wait until it settles. The Sender and the problems it handed to the daemon's ``fatal``."""
    fatals: list = []
    url = await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            daemon = SimpleNamespace(
                hub=WorkerHub(url, "evw_" + "f" * 43, session), hard_stop=asyncio.Event(), fatal=fatals.append
            )
            spool = Spool(tmp_path, RUN_ID, SpoolBudget())
            sender = Sender(daemon, spool)
            task = asyncio.create_task(sender.run())
            await script(sender, spool)
            sender.close()
            await asyncio.wait_for(sender.drained(), 10)
            await asyncio.wait_for(task, 5)
    finally:
        await hub.stop()
    return sender, fatals


@pytest.fixture(autouse=True)
def _quick_backoff(monkeypatch):
    monkeypatch.setattr(sender_module, "Backoff", lambda: Backoff(first=0.01, most=0.05))


def test_a_502_then_a_404_of_the_proxy_keep_the_events_in_the_spool_until_the_hub_takes_them_from_the_last_ack(
    tmp_path, caplog
):
    hub = EventsHub([])

    async def script(sender: Sender, spool: Spool) -> None:
        for number in range(1, 4):
            spool.append("agent_message_chunk", {"text": f"chunk {number}"}, AT)
        sender.poke()
        await eventually(lambda: spool.ack_seq == 3, "the first events to be acknowledged")
        hub.answers += [bad_gateway, proxy_not_found]  # the hub is being redeployed
        for number in range(4, 9):
            spool.append("agent_message_chunk", {"text": f"chunk {number}"}, AT)
        sender.poke()
        await eventually(lambda: spool.ack_seq == 8, "the events written during the redeploy to be acknowledged")
        spool.append("system", {"text": "after the redeploy"}, AT)
        sender.poke()

    with caplog.at_level(logging.INFO, logger="evo_agents.worker"):
        sender, fatals = asyncio.run(send(tmp_path, hub, script))

    assert sender.gave_up is None and not fatals
    assert hub.stored == list(range(1, 10)), "every spooled event reached the hub, in seq order, once"
    assert hub.firsts[:4] == [1, 4, 4, 4], "the batch after the 502 and the 404 was sent again from the last ack"
    statuses = [getattr(record, "status", None) for record in caplog.records if "wait in the spool" in record.message]
    assert statuses == [502, 404]
    assert not any("dropping them" in record.message for record in caplog.records)


def test_the_hubs_own_404_ends_the_sending_and_drops_the_events(tmp_path, caplog):
    hub = EventsHub([hub_not_found])

    async def script(sender: Sender, spool: Spool) -> None:
        for number in range(1, 4):
            spool.append("agent_message_chunk", {"text": f"chunk {number}"}, AT)
        sender.poke()
        await eventually(lambda: sender.settled, "the sending to give up")

    with caplog.at_level(logging.INFO, logger="evo_agents.worker"):
        sender, fatals = asyncio.run(send(tmp_path, hub, script))

    assert sender.gave_up == f"this worker did not claim run {RUN_ID}"
    assert not fatals, "a run that is not this worker's does not stop the daemon"
    assert hub.firsts == [1] and hub.stored == [], "the hub's own 404 is not sent again"
    dropped = [record for record in caplog.records if "dropping them" in record.message]
    assert [(record.status, record.dropped) for record in dropped] == [(404, 3)]
