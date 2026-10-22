"""The broker of a run's browser live view on the worker (plan agent-browser, criterion a5), against the Chromium
`evo-agents worker browser install` put in Playwright's cache (skipped on a machine where that command never ran) and
the in-memory hub, whose end of WS /v1/worker/runs/{id}/browser the test reads and writes as the hub's relay would.

- The broker connects as soon as the run's Chromium listens and says the mode and the tabs (STATE); it screencasts only
  while someone views (VIEW), frames of at most 1280x800, the next one only once the viewer acked the last.
- Agent mode: the viewer picks the tab it watches and nothing else it sends reaches the browser; a tab it watches behind
  the agent's, where Chromium paints nothing, comes as a still. Human mode (the run interactive, or waiting on a login
  decision): mouse, keys, composed and pasted text, back, forward, reload and a URL of http or https drive the tab
  watched; any other scheme is refused with a notice, and a tab on file: is closed.
- A popup is followed, and its closing brings the tab that opened it back.
- The broker closes its end with 1000 when the browser closes, connects again after a drop, and not after a refusal.
- In the daemon: a run dispatched with browser connects its broker as soon as its browser is built and closes it as
  the run ends; a run without browser opens none (criterion a10)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import sys
import types

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

import aiohttp
from aiohttp import web

from evo_agents.hub import browser_view as frames
from evo_agents.worker.browser import install, live
from evo_agents.worker.browser import session as browser_session
from evo_agents.worker.home import WorkerHome
from tests.worker.fake_hub import TOKEN, FakeHub
from tests.worker.test_browser_session import installed  # noqa: F401 (a fixture)
from tests.worker.test_plan_runs import (
    PLAN,
    machine,  # noqa: F401 (a fixture)
    plan_body,
    run_repos,
    wait_for,
    with_daemon,
)

RUN = 7
WAIT = 15.0
FIELD = '<input id="q" style="position:absolute;left:0;top:0;width:600px;height:120px" oninput="document.title=\'typed:\'+this.value">'
PAGES = {
    "/form": f"<title>form</title>{FIELD}",
    "/opener": (
        '<title>opener</title><a href="/next" target="_blank" '
        'style="position:absolute;left:0;top:0;width:600px;height:120px;display:block">next</a>'
    ),
    "/next": "<title>next</title><p>the popup</p>",
    "/moving": "<title>moving</title><p id=t></p><script>setInterval(()=>t.textContent=Date.now(),30)</script>",
}


# The helpers' own rules


def test_browser_view_mode_is_human_while_interactive_or_waiting_on_a_login():
    assert live.mode("interactive", False) == frames.HUMAN
    assert live.mode("waiting", True) == frames.HUMAN
    assert live.mode("waiting", False) == frames.AGENT
    assert live.mode("running", True) == frames.AGENT, "a login decision answered: the agent drives again"
    assert live.mode("leased", False) == frames.AGENT


def test_browser_view_url_box_opens_http_and_https_alone():
    assert live.openable("northwind.test/orders") == "https://northwind.test/orders"
    assert live.openable("  http://lumen.test:8080/a?b=c ") == "http://lumen.test:8080/a?b=c"
    assert live.openable("localhost:8080") == "https://localhost:8080"
    for refused in (
        "javascript:alert(1)",
        "file:///etc/hosts",
        "chrome://settings",
        "data:text/html,x",
        "",
        "https://",
    ):
        assert live.openable(refused) is None, refused
    assert live.allowed("") and live.allowed("about:blank") and live.allowed("blob:https://lumen.test/1")
    assert not live.allowed("file:///etc/hosts") and not live.allowed("chrome://version")


def test_browser_view_reads_a_stills_size_from_its_jpeg_header():
    app0 = b"\xff\xe0\x00\x10JFIF\x00" + bytes(9)
    start = b"\xff\xc0\x00\x11\x08" + (800).to_bytes(2, "big") + (1280).to_bytes(2, "big") + bytes(12)
    assert live.jpeg_size(b"\xff\xd8" + app0 + b"\xff" + start) == (1280, 800)
    assert live.jpeg_size(b"\xff\xd8\xff\xda\x00\x0c" + bytes(12)) is None, "image data before a size"
    assert live.jpeg_size(b"\x89PNG\r\n\x1a\n" + bytes(16)) is None


def test_browser_view_worker_end_is_on_the_hubs_worker_routes():
    assert live.ws_url("https://hub.northwind.test/", 7) == "wss://hub.northwind.test/v1/worker/runs/7/browser"
    assert live.ws_url("http://127.0.0.1:8000", 9) == "ws://127.0.0.1:8000/v1/worker/runs/9/browser"
    with pytest.raises(ValueError):
        live.ws_url("ftp://hub.northwind.test", 7)


# A broker against a real Chromium


class View:
    """The test's side of the broker of run RUN: the hub's end, and what it read of it."""

    def __init__(self, hub: FakeHub, browser: browser_session.BrowserSession, run, site: str):
        self.hub, self.browser, self.run, self.site = hub, browser, run, site
        self.link = None
        self.screens: list[dict] = []
        self.states: list[dict] = []
        self.ack = True  # ack each screen frame as it comes

    async def connected(self, index: int = 0):
        self.link = await self.hub.browser_link(RUN, index=index)
        return self.link

    async def frame(self, timeout: float = WAIT) -> tuple[int, bytes]:
        try:
            data = await self.link.next(timeout)
        except TimeoutError:
            raise AssertionError(f"no frame within {timeout:g}s; the last STATEs: {self.states[-3:]}") from None
        kind, payload = frames.parse(data)
        if kind == frames.SCREEN:
            header, jpeg = frames.parse_screen(payload)
            assert jpeg[:2] == b"\xff\xd8", "a JPEG"
            self.screens.append(header)
            if self.ack:
                await self.link.send(frames.message(frames.ACK, {"seq": header["seq"]}))
        else:
            assert kind == frames.STATE, kind
            self.states.append(frames.body_of(payload))
        return kind, payload

    async def state(self, until=lambda body: True, timeout: float = WAIT) -> dict:
        """The next STATE for which ``until`` holds."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise AssertionError(f"no such STATE; the last ones: {self.states[-3:]}")
            kind, payload = await self.frame(left)
            if kind == frames.STATE and until(frames.body_of(payload)):
                return frames.body_of(payload)

    async def screen(self, timeout: float = WAIT) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise AssertionError("no screen frame")
            kind, payload = await self.frame(left)
            if kind == frames.SCREEN:
                return frames.parse_screen(payload)[0]

    async def quiet(self, seconds: float) -> None:
        """Read what comes for ``seconds``."""
        with contextlib.suppress(TimeoutError, AssertionError):
            async with asyncio.timeout(seconds):
                while True:
                    await self.frame(seconds)

    async def view(self, viewing: bool = True, login: bool = False) -> None:
        await self.link.send(frames.message(frames.VIEW, {"viewing": viewing, "login": login}))

    async def send(self, event: dict) -> None:
        await self.link.send(frames.message(frames.INPUT, event))

    async def click(self, x: float = 40, y: float = 40) -> None:
        for action, buttons in (("down", 1), ("up", 0)):
            await self.send(
                {"type": "mouse", "action": action, "x": x, "y": y, "button": "left", "buttons": buttons, "clicks": 1}
            )

    async def open(self, path: str) -> dict:
        await self.send({"type": "open", "url": self.site + path})
        return await self.state(lambda body: active_tab(body)["url"] == self.site + path)

    async def browser_call(self, method: str, params: dict) -> dict:
        """A DevTools command to the browser, as the run's agent would send it."""
        url = self.browser.cdp.replace("http://", "ws://", 1) + self.browser.socket
        async with aiohttp.ClientSession() as session, session.ws_connect(url) as socket:
            await socket.send_json({"id": 1, "method": method, "params": params})
            while True:
                answer = await socket.receive_json(timeout=WAIT)
                if answer.get("id") == 1:
                    return answer.get("result") or {}


def active_tab(body: dict) -> dict:
    return next((tab for tab in body["tabs"] if tab["id"] == body["active"]), {"url": None, "title": None})


def titled(title: str):
    return lambda body: active_tab(body)["title"] == title


async def serve_site() -> tuple[web.AppRunner, str]:
    async def page(request: web.Request) -> web.Response:
        return web.Response(
            text=f"<!doctype html><html><body>{PAGES[request.path]}</body></html>", content_type="text/html"
        )

    app = web.Application()
    app.add_routes([web.get(path, page) for path in PAGES])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


@contextlib.asynccontextmanager
async def broker(installed, tmp_path, state: str = "running", refusal: int | None = None):  # noqa: F811
    """A run RUN in ``state`` whose browser runs and whose broker connects to an in-memory hub."""
    home = WorkerHome(tmp_path / "worker")
    install.write(home, installed)
    hub = FakeHub()
    hub.browser_refusal = refusal
    await hub.start()
    site, site_url = await serve_site()
    http = aiohttp.ClientSession()
    daemon = types.SimpleNamespace(
        home=home,
        env=dict(os.environ),
        browsers={},
        hub=types.SimpleNamespace(session=http, url=hub.url),
        token=TOKEN,
    )
    browser = browser_session.BrowserSession(daemon, RUN)
    run = types.SimpleNamespace(state=state)
    try:
        await browser.open()
        browser.watch(lambda: run.state)
        yield View(hub, browser, run, site_url)
    finally:
        await browser.close(0)
        await http.close()
        await site.cleanup()
        await hub.stop()


def test_browser_view_broker_says_its_tabs_and_screencasts_only_while_someone_views(installed, tmp_path):  # noqa: F811
    found = {}

    async def go():
        async with broker(installed, tmp_path, state="interactive") as view:
            await view.connected()
            first = await view.state()
            found["first"] = first
            await view.open("/moving")
            await view.quiet(1.0)
            found["unviewed"] = len(view.screens)
            await view.view(True)
            found["header"] = await view.screen()
            view.ack = False
            seen = len(view.screens)
            await view.quiet(1.0)
            found["unacked"] = len(view.screens) - seen
            await view.link.send(frames.message(frames.ACK, {"seq": view.screens[-1]["seq"]}))
            view.ack = True
            await view.screen()
            await view.view(False)
            await view.quiet(1.0)
            seen = len(view.screens)
            await view.quiet(1.0)
            found["after"] = len(view.screens) - seen

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(live, "ACK_WAIT", 30.0)
        asyncio.run(go())
    first = found["first"]
    assert first["mode"] == frames.HUMAN and len(first["tabs"]) == 1 and first["active"] == first["tabs"][0]["id"]
    assert found["unviewed"] == 0, "nobody views: no screencast"
    header = found["header"]
    assert 0 < header["width"] <= live.MAX_WIDTH and 0 < header["height"] <= live.MAX_HEIGHT, header
    assert found["unacked"] <= 1, "the next frame waits for the viewer's ACK"
    assert found["after"] == 0, "the viewer left: the screencast stopped"


def test_browser_view_human_mode_types_keys_text_and_paste_and_agent_mode_drops_them(installed, tmp_path):  # noqa: F811
    found = {}

    async def go():
        async with broker(installed, tmp_path, state="interactive") as view:
            await view.connected()
            await view.view(True)
            await view.open("/form")
            await view.click()
            for action in ("down", "up"):
                await view.send(
                    {"type": "key", "action": action, "key": "a", "code": "KeyA", "keyCode": 65, "text": "a"}
                )
            await view.state(titled("typed:a"))
            await view.send({"type": "text", "text": "bé"})
            await view.send({"type": "paste", "text": " northwind"})
            await view.state(titled("typed:abé northwind"))
            view.run.state = "running"
            await view.state(lambda body: body["mode"] == frames.AGENT)
            await view.send({"type": "text", "text": "x"})
            await view.send({"type": "key", "action": "down", "key": "y", "code": "KeyY", "keyCode": 89, "text": "y"})
            await view.send({"type": "open", "url": view.site + "/next"})
            await view.quiet(1.0)
            view.run.state = "interactive"
            await view.state(lambda body: body["mode"] == frames.HUMAN)
            await view.send({"type": "key", "action": "down", "key": "Backspace", "code": "Backspace", "keyCode": 8})
            await view.send({"type": "key", "action": "up", "key": "Backspace", "code": "Backspace", "keyCode": 8})
            found["last"] = await view.state(titled("typed:abé northwin"))
            found["titles"] = [active_tab(body)["title"] for body in view.states]

    asyncio.run(go())
    assert active_tab(found["last"])["url"].endswith("/form"), "agent mode opened nothing"
    typed = [title.removeprefix("typed:") for title in found["titles"] if (title or "").startswith("typed:")]
    assert typed and not any("x" in text or "y" in text for text in typed), found["titles"]


def test_browser_view_agent_mode_lets_the_viewer_pick_the_tab_it_watches(installed, tmp_path):  # noqa: F811
    found = {}

    async def go():
        async with broker(installed, tmp_path, state="running") as view:
            await view.connected()
            first = await view.state()
            made = await view.browser_call("Target.createTarget", {"url": view.site + "/moving"})
            found["agent_tab"] = await view.state(lambda body: len(body["tabs"]) == 2)
            await view.view(True)
            found["behind"] = await view.screen()  # the tab watched is behind the agent's, where nothing paints
            await view.send({"type": "tab", "id": made["targetId"]})
            found["picked"] = await view.state(lambda body: body["active"] == made["targetId"])
            seen = len(view.screens)
            await view.screen()
            await view.screen()  # a second frame: the screencast, not a still
            found.update(first=first, frames=len(view.screens) - seen)
            await view.send({"type": "tab", "id": first["active"]})
            found["back"] = await view.state(lambda body: body["active"] == first["active"])
            found["still"] = await view.screen()

    asyncio.run(go())
    assert found["agent_tab"]["active"] == found["first"]["active"], "a tab the agent opens is not followed"
    assert found["picked"]["mode"] == frames.AGENT and found["frames"] >= 2
    for still in (found["behind"], found["still"]):
        assert 0 < still["width"] <= live.MAX_WIDTH and 0 < still["height"] <= live.MAX_HEIGHT, still


def test_browser_view_waiting_on_a_login_decision_opens_in_human_mode(installed, tmp_path):  # noqa: F811
    async def go():
        async with broker(installed, tmp_path, state="waiting") as view:
            await view.connected()
            assert (await view.state())["mode"] == frames.AGENT
            await view.view(True, login=True)
            await view.state(lambda body: body["mode"] == frames.HUMAN)
            await view.view(True, login=False)
            await view.state(lambda body: body["mode"] == frames.AGENT)

    asyncio.run(go())


def test_browser_view_human_mode_closes_file_tabs_and_refuses_other_schemes(installed, tmp_path):  # noqa: F811
    found = {}

    async def go():
        async with broker(installed, tmp_path, state="running") as view:
            await view.connected()
            await view.state()
            await view.browser_call("Target.createTarget", {"url": "file:///"})
            await view.state(lambda body: any(tab["url"].startswith("file:") for tab in body["tabs"]))
            view.run.state = "interactive"
            closed = await view.state(lambda body: "notice" in body)
            found["closed"] = closed
            await view.state(lambda body: not any(tab["url"].startswith("file:") for tab in body["tabs"]))
            for url in ("javascript:alert(1)", "file:///etc/hosts", "chrome://version"):
                await view.send({"type": "open", "url": url})
                found.setdefault("refused", []).append(await view.state(lambda body: "notice" in body))
            await view.browser_call("Target.createTarget", {"url": "file:///"})
            await view.state(lambda body: "notice" in body)
            found["left"] = await view.state(
                lambda body: not any(tab["url"].startswith("file:") for tab in body["tabs"])
            )

    asyncio.run(go())
    assert "file:" in found["closed"]["notice"] and found["closed"]["mode"] == frames.HUMAN
    assert all(body["notice"] == live.REFUSED_URL for body in found["refused"])
    assert all(not active_tab(body)["url"].startswith(("javascript:", "file:", "chrome:")) for body in found["refused"])
    assert found["left"]["tabs"], "a tab stays open"


def test_browser_view_follows_a_popup_and_goes_back_to_its_opener_when_it_closes(installed, tmp_path):  # noqa: F811
    found = {}

    async def go():
        async with broker(installed, tmp_path, state="interactive") as view:
            await view.connected()
            await view.view(True)
            opener = active_tab(await view.open("/opener"))
            await view.click()
            popup = await view.state(lambda body: active_tab(body)["url"] == view.site + "/next")
            found["popup"] = popup
            await view.send({"type": "back"})  # nothing behind the popup: it stays
            await view.send({"type": "tab", "id": opener["id"]})
            found["picked"] = await view.state(lambda body: body["active"] == opener["id"])
            await view.send({"type": "tab", "id": active_tab(popup)["id"]})
            await view.state(lambda body: body["active"] == popup["active"])
            await view.browser_call("Target.closeTarget", {"targetId": popup["active"]})
            found["back"] = await view.state(
                lambda body: len(body["tabs"]) == 1 and body["active"] == body["tabs"][0]["id"]
            )
            found["opener"] = opener

    asyncio.run(go())
    assert len(found["popup"]["tabs"]) == 2 and found["popup"]["active"] != found["opener"]["id"]
    assert found["picked"]["active"] == found["opener"]["id"]
    assert found["back"]["active"] == found["opener"]["id"]


def test_browser_view_navigates_back_forward_and_reload_in_human_mode(installed, tmp_path):  # noqa: F811
    async def go():
        async with broker(installed, tmp_path, state="interactive") as view:
            await view.connected()
            await view.open("/form")
            await view.open("/next")
            await view.send({"type": "back"})
            await view.state(lambda body: active_tab(body)["url"] == view.site + "/form")
            await view.send({"type": "forward"})
            await view.state(lambda body: active_tab(body)["url"] == view.site + "/next")
            await view.send({"type": "reload"})
            await view.quiet(0.5)
            await view.send({"type": "open", "url": view.site.removeprefix("http://") + "/form"})
            await view.quiet(1.0)  # https on a plain port: refused by the page, never by the broker
            assert not any("notice" in body for body in view.states)

    asyncio.run(go())


def test_browser_view_closes_with_1000_and_reconnects_after_a_drop(installed, tmp_path):  # noqa: F811
    found = {}

    async def go():
        async with broker(installed, tmp_path) as view:
            first = await view.connected()
            await view.state()
            await first.socket.close(code=1011, message=b"the hub restarts")
            second = await view.connected(index=1)
            found["again"] = await view.state()
            await view.browser.close(0)
            await asyncio.wait_for(second.closed.wait(), WAIT)
            found["code"] = second.close_code
            found["live"] = view.browser.live

    asyncio.run(go())
    assert found["again"]["mode"] == frames.AGENT, "the new connection says the state first"
    assert found["code"] == 1000 and found["live"] is None


def test_browser_view_does_not_connect_again_after_a_refusal(installed, tmp_path):  # noqa: F811
    found = {}

    async def go():
        async with broker(installed, tmp_path, refusal=4403) as view:
            await view.connected()
            watching = view.browser.live
            await wait_for(lambda: not watching.running, "the broker to stop", WAIT)
            await asyncio.sleep(live.RECONNECT_FIRST * 2)
            found.update(links=len(view.hub.browser_links[RUN]), refused=watching.refused)

    asyncio.run(go())
    assert found == {"links": 1, "refused": 4403}


# In the daemon


def test_browser_view_a_run_with_browser_connects_as_its_browser_is_built_and_one_without_never(
    installed,  # noqa: F811
    machine,  # noqa: F811
):
    install.write(machine.home, installed)
    held, release = machine.tmp / "held", machine.tmp / "release"
    code = "import os; print(bool(os.environ.get('EVO_BROWSER_CDP')))"
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"sh": f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"},
                {"touch": str(held)},
                {"wait_for": str(release)},
                {"result": {"summary": "Looked with the browser."}},
            ]
        }
    )
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()), browser=True)
        link = await hub.browser_link(run_id, timeout=WAIT)
        state = frames.body_of(frames.parse(await link.next(WAIT))[1])
        await wait_for(held.exists, "the agent to start")
        found.update(state=state, open=not link.closed.is_set())
        release.touch()
        assert await hub.wait_state(run_id, "done", "failed") == "done", hub.texts(run_id)
        await asyncio.wait_for(link.closed.wait(), WAIT)
        found["code"] = link.close_code
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        plain = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        assert await hub.wait_state(plain, "done", "failed") == "done", hub.texts(plain)
        found.update(links=sorted(hub.browser_links), run=run_id)

    with_daemon(machine, body)
    assert found["state"]["mode"] == frames.AGENT and len(found["state"]["tabs"]) == 1, found["state"]
    assert found["open"], "the view is there while the agent runs"
    assert found["code"] == 1000, "the run's end closes the view"
    assert found["links"] == [found["run"]], "a run without browser opens no view"
