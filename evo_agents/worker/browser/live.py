"""The live view of a run's browser on the worker (plan agent-browser, criterion a5): the broker between the run's
Chromium and the hub's relay (``evo_agents.hub.server.browser_view``), in the frames ``evo_agents.hub.browser_view``
describes.

``BrowserSession.watch`` starts it as soon as the run's Chromium listens, and ``BrowserSession.close`` (or ``save``,
which closes Chromium) stops it, closing its end on the hub with 1000 so the viewer learns the browser is gone. In
between it holds one websocket to the hub, WS /v1/worker/runs/{id}/browser with the worker's token, and connects again
when it drops, after RECONNECT_FIRST seconds and twice as long each time up to RECONNECT_MAX. A refusal (4401, 4403,
4409, 4426, or a handshake answered 401, 403, 404 or 426, as a hub without the live view answers) ends it.

The broker watches one tab of the browser at a time: the first, then the tab a popup or a link opens, then the tab
the viewer picks; when that tab closes, the one that opened it or the last one left. While the hub says someone views
(VIEW), it screencasts that tab (Page.startScreencast, JPEG of QUALITY, at most MAX_WIDTH x MAX_HEIGHT) and acks each
frame once the viewer drew it (ACK), or ACK_WAIT seconds after it went, so Chromium sends the next; nobody viewing, no
screencast. Chromium paints no tab in the background, so when the tab watched is behind another (the agent brought its
own tab to the front, or the viewer picked one in agent mode, where the broker brings no tab forward), the viewer gets
one still of it (Page.captureScreenshot), and the screencast again once it comes to the front. It sends STATE, the
mode and the tabs, each time they change.

The mode is ``mode(state, login)``: human while the run is interactive (Take control is the run's takeover, and Hand
back its handback) or waits on a login decision (VIEW's login), agent otherwise. In agent mode the viewer watches and
picks the tab it watches, and nothing else it sends reaches the browser. In human mode it drives the tab watched:
mouse, wheel and keys through Input.dispatchMouseEvent and Input.dispatchKeyEvent, text an input method composed and
text pasted through Input.insertText, back, forward, reload, a URL to open (http and https only: a URL without a
scheme gets https, any other scheme a notice in STATE), and the tab it picks comes to the front. In human mode a tab
on a scheme outside ALLOWED_SCHEMES (file:, chrome:, view-source: ...) is closed as soon as the broker sees it, so the
person reads nothing of the worker through it; no URL, key or text is logged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import re
from collections.abc import Callable
from urllib.parse import urlsplit

import aiohttp

from evo_agents import __version__
from evo_agents.hub import browser_view as frames
from evo_agents.hub.runs import PROTOCOL_HEADER, PROTOCOL_VERSION
from evo_agents.hub.terminal import FrameError
from evo_agents.worker.runner.common import log

QUALITY = 65  # JPEG quality of the screencast, of 60 to 75
MAX_WIDTH, MAX_HEIGHT = 1280, 800  # the screencast's frames fit in this
ACK_WAIT = 2.0  # seconds a frame waits for the viewer's ACK before Chromium is told to send the next anyway
MODE_POLL = 0.5  # seconds between two looks at the run's state, for the mode
CDP_TIMEOUT = 10.0  # seconds a DevTools command has to answer
CDP_MESSAGE_BYTES = 64 * 1024 * 1024  # the largest DevTools message read: a frame in base64 is far less
CONNECT_TIMEOUT = 30.0  # seconds for the handshake with the hub
PING_SECONDS = 30.0  # websocket pings, for the proxies between the worker and the hub
RECONNECT_FIRST, RECONNECT_MAX = 1.0, 30.0  # seconds before connecting to the hub again
STOP_GRACE = 5.0  # seconds the broker has to close its sockets when it stops
REFUSED = frozenset({4401, 4403, 4409, 4426})  # close codes after which the broker does not connect again
REFUSED_STATUS = frozenset({401, 403, 404, 426})  # the same, as the answer to the handshake
CLOSED = 1000  # the code of the broker's end on the hub when the run's browser closes
ALLOWED_SCHEMES = frozenset({"http", "https", "about", "data", "blob"})  # a tab may stay open on these in human mode
OPEN_SCHEMES = frozenset({"http", "https"})  # the viewer's URL box opens these
MAX_TITLE_CHARS = 300
_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)")  # a scheme, and not host:port
_MOUSE = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}
USER_AGENT = f"evo-agents-worker/{__version__}"
REFUSED_URL = "the URL box opens http and https addresses only"


def mode(state: str, login: bool) -> str:
    """The mode of the browser of a run in ``state``: human while a person drives the run (interactive), or while it
    waits on a login decision (``login``, as the hub said); agent otherwise."""
    return frames.HUMAN if state == "interactive" or (login and state == "waiting") else frames.AGENT


def ws_url(hub_url: str, run_id: int) -> str:
    """The worker's end of run ``run_id``'s live view on the hub at ``hub_url`` (https -> wss, http -> ws)."""
    scheme, _, rest = hub_url.partition("://")
    socket_scheme = {"https": "wss", "http": "ws"}.get(scheme.lower())
    if socket_scheme is None:
        raise ValueError(f"{hub_url} is neither https nor http")
    return f"{socket_scheme}://{rest.rstrip('/')}/v1/worker/runs/{int(run_id)}/browser"


def scheme_of(url: str) -> str:
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


def allowed(url: str) -> bool:
    """Whether a tab on ``url`` stays open while a person drives the browser; a tab that has none yet does."""
    return not url or scheme_of(url) in ALLOWED_SCHEMES


def openable(text: str) -> str | None:
    """The URL the viewer's URL box asks for, with https when it names no scheme; None for a scheme but http and
    https, or no host."""
    url = text.strip()
    if not url:
        return None
    if "://" not in url and _SCHEME.match(url) is None:
        url = "https://" + url
    try:
        split = urlsplit(url)
    except ValueError:
        return None
    return url if split.scheme.lower() in OPEN_SCHEMES and split.hostname else None


_START_OF_FRAME = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """The width and height a JPEG's start of frame says; None for bytes that are not a JPEG with one."""
    index = 2 if data[:2] == b"\xff\xd8" else len(data)
    while index + 9 <= len(data) and data[index] == 0xFF:
        marker = data[index + 1]
        if marker == 0xFF:  # a fill byte before a marker
            index += 1
        elif marker in _START_OF_FRAME:
            height, width = data[index + 5 : index + 7], data[index + 7 : index + 9]
            return int.from_bytes(width, "big"), int.from_bytes(height, "big")
        elif marker == 0xDA:  # the image data starts: no size before it
            return None
        else:
            index += 2 + int.from_bytes(data[index + 2 : index + 4], "big")
    return None


class CdpError(Exception):
    """A DevTools command that failed or did not answer, or the DevTools socket closed."""


class Cdp:
    """The browser's DevTools socket, shared by the commands (``call``) and the events ``read`` hands on."""

    def __init__(self, socket: aiohttp.ClientWebSocketResponse):
        self.socket = socket
        self.last = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.closed = False

    async def call(self, method: str, params: dict | None = None, *, session: str | None = None) -> dict:
        """Send a command, to a tab's ``session`` or to the browser, and wait for its result."""
        if self.closed:
            raise CdpError(f"{method}: the browser's DevTools socket closed")
        self.last += 1
        number = self.last
        message = {"id": number, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        answer = asyncio.get_running_loop().create_future()
        self.pending[number] = answer
        try:
            await self.socket.send_str(json.dumps(message))
            result = await asyncio.wait_for(answer, CDP_TIMEOUT)
        except (TimeoutError, aiohttp.ClientError, ConnectionError, RuntimeError) as exc:
            raise CdpError(f"{method}: {exc or type(exc).__name__}") from None
        finally:
            self.pending.pop(number, None)
        if "error" in result:
            raise CdpError(f"{method}: {result['error'].get('message', 'refused')}")
        return result.get("result") or {}

    async def read(self, on_event: Callable[[str, dict, str | None], None]) -> None:
        """Hand each answer to its command and each event to ``on_event(method, params, session)`` until the socket
        closes; the commands still waiting then fail."""
        try:
            async for message in self.socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                except ValueError:
                    continue
                waiting = self.pending.get(data.get("id")) if "id" in data else None
                if waiting is not None and not waiting.done():
                    waiting.set_result(data)
                elif "method" in data and "id" not in data:
                    on_event(data["method"], data.get("params") or {}, data.get("sessionId"))
        finally:
            self.closed = True
            for waiting in self.pending.values():
                if not waiting.done():
                    waiting.set_exception(CdpError("the browser's DevTools socket closed"))


class LiveView:
    """The broker of run ``run_id``'s browser, whose DevTools socket is ``devtools``
    (ws://127.0.0.1:<port>/devtools/browser/<id>), to the hub at ``hub_url`` over ``http`` with the worker's
    ``token``. ``state_of()`` is the run's state now, of which the mode follows (``mode``)."""

    def __init__(self, run_id: int, devtools: str, http, hub_url: str, token: str, state_of: Callable[[], str]):
        self.run_id = run_id
        self.devtools = devtools
        self.http = http
        self.hub_url = hub_url
        self.token = token
        self.state_of = state_of
        self.cdp: Cdp | None = None
        self.hub: aiohttp.ClientWebSocketResponse | None = None
        self.send_lock = asyncio.Lock()
        self.watch_lock = asyncio.Lock()
        self.tabs: dict[str, dict] = {}  # the browser's pages, by target id, in the order they opened
        self.active: str | None = None  # the tab watched
        self.session: str | None = None  # the DevTools session of the tab watched
        self.ready = False  # the first tab is watched: a tab that opens later is followed when a page opened it
        self.casting = False
        self.viewing = False
        self.login = False
        self.mode = frames.AGENT
        self.seq = 0  # of the screen frames sent
        self.unacked: tuple[int, str | None, int | None] | None = None  # (seq, session, Chromium's number): sent
        self.held: tuple[str | None, int | None, dict] | None = None  # (session, number, params): the next to send
        self.ack_timer: asyncio.TimerHandle | None = None
        self.sent: bytes | None = None  # the last STATE sent on this connection
        self.connections = 0
        self.refused: int | None = None  # the code or status the hub refused this end with
        self.closing = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self._task: asyncio.Task | None = None
        self._events = {
            "Page.screencastFrame": self._frame,
            "Page.screencastVisibilityChanged": self._visibility,
            "Target.targetCreated": self._created,
            "Target.targetInfoChanged": self._changed,
            "Target.targetDestroyed": self._destroyed,
            "Target.detachedFromTarget": self._detached,
        }
        self._inputs = {
            "mouse": self._mouse,
            "wheel": self._wheel,
            "key": self._key,
            "text": self._text,
            "paste": self._text,
        }
        self._navigations = {
            "back": self._history,
            "forward": self._history,
            "reload": self._reload,
            "open": self._open,
        }

    # Start and stop

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._serve(), name=f"browser-view-{self.run_id}")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        """Close the end on the hub with CLOSED and the DevTools socket, within STOP_GRACE seconds."""
        task = self._task
        if task is None or task.done():
            return
        self.closing.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), STOP_GRACE)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except Exception:
            pass

    async def _serve(self) -> None:
        try:
            async with (
                aiohttp.ClientSession() as local,  # the hub's session carries nothing to the browser's port
                local.ws_connect(self.devtools, max_msg_size=CDP_MESSAGE_BYTES) as socket,
            ):
                self.cdp = Cdp(socket)
                reader = asyncio.create_task(self.cdp.read(self._on_event))
                try:
                    await self._discover()
                    await self._until_closed(reader)
                finally:
                    reader.cancel()
                    await self._end_tasks(reader)
        except (aiohttp.ClientError, OSError, CdpError, KeyError) as exc:
            log.warning("browser view stopped", extra={"run_id": self.run_id, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            await self._end_tasks()
            log.info("browser view ended", extra={"run_id": self.run_id, "screens": self.seq})

    async def _until_closed(self, reader: asyncio.Task) -> None:
        """Serve the hub until the broker stops, the browser closes or the hub refuses this end."""
        linked = asyncio.create_task(self._hub_loop())
        ticker = asyncio.create_task(self._tick())
        closing = asyncio.create_task(self.closing.wait())
        try:
            await asyncio.wait({reader, linked, closing}, return_when=asyncio.FIRST_COMPLETED)
            if linked.done() and not linked.cancelled() and linked.exception() is not None:
                failed = linked.exception()
                log.warning("browser view link failed", extra={"run_id": self.run_id}, exc_info=failed)
        finally:
            await self._end_tasks(linked, ticker, closing)

    async def _end_tasks(self, *tasks: asyncio.Task) -> None:
        """Cancel ``tasks``, or every task the broker spawned, and wait for them."""
        ending = list(tasks or self.tasks)
        for task in ending:
            task.cancel()
        await asyncio.gather(*ending, return_exceptions=True)

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if task.cancelled() or task.exception() is None or isinstance(task.exception(), CdpError):
            return  # a command the browser did not take: the tab went, or the browser closes
        log.warning("browser view task failed", extra={"run_id": self.run_id}, exc_info=task.exception())

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(MODE_POLL)
            with contextlib.suppress(CdpError):
                await self._check_mode()
                if self.viewing:
                    await self._refresh()

    async def _refresh(self) -> None:
        """Read the tabs' titles again: Chromium says when a tab's URL changes, not when its title does."""
        for info in (await self.cdp.call("Target.getTargets")).get("targetInfos") or ():
            if info.get("targetId") in self.tabs:
                self._changed({"targetInfo": info}, None)
        await self._publish()

    # The hub

    async def _hub_loop(self) -> None:
        """Hold the end on the hub, connecting again after a drop, until the hub refuses it."""
        loop = asyncio.get_running_loop()
        delay = RECONNECT_FIRST
        while not self.closing.is_set():
            socket = await self._connect()
            if socket is not None:
                started = loop.time()
                code = await self._linked(socket)
                if code in REFUSED:
                    self.refused = code
                    log.warning("browser view refused by the hub", extra={"run_id": self.run_id, "code": code})
                    return
                if loop.time() - started > RECONNECT_MAX:
                    delay = RECONNECT_FIRST
            elif self.refused is not None:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.closing.wait(), delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _connect(self) -> aiohttp.ClientWebSocketResponse | None:
        headers = {PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT}
        try:
            return await asyncio.wait_for(
                self.http.ws_connect(
                    ws_url(self.hub_url, self.run_id),
                    headers=headers,
                    max_msg_size=frames.MAX_FRAME_BYTES,
                    heartbeat=PING_SECONDS,
                ),
                CONNECT_TIMEOUT,
            )
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in REFUSED_STATUS:
                self.refused = exc.status
                log.warning("browser view refused by the hub", extra={"run_id": self.run_id, "status": exc.status})
            else:
                log.info("browser view not connected", extra={"run_id": self.run_id, "status": exc.status})
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
            log.info("browser view not connected", extra={"run_id": self.run_id, "error": type(exc).__name__})
        return None

    async def _linked(self, socket: aiohttp.ClientWebSocketResponse) -> int | None:
        """Serve one connection to the hub; the code it closed with."""
        self.hub, self.sent = socket, None
        self.connections += 1
        log.info("browser view connected", extra={"run_id": self.run_id})
        try:
            await self._check_mode()  # the mode the run is in now, before the viewer sees a STATE
            await self._publish()
            async for message in socket:
                if message.type != aiohttp.WSMsgType.BINARY:
                    continue
                try:
                    await self._from_hub(message.data)
                except CdpError as exc:  # the tab went while the viewer acted on it: the next frame tells
                    log.info("browser view frame not served", extra={"run_id": self.run_id, "error": str(exc)})
            return socket.close_code
        except asyncio.CancelledError:  # the broker stops: the run's browser closes
            with contextlib.suppress(Exception):
                await asyncio.wait_for(socket.close(code=CLOSED, message=b"the run's browser closed"), STOP_GRACE)
            raise
        finally:
            self.hub = None
            await self._nobody_views()

    async def _send(self, data: bytes) -> bool:
        socket = self.hub
        if socket is None or socket.closed:
            return False
        async with self.send_lock:
            try:
                await socket.send_bytes(data)
            except (aiohttp.ClientError, ConnectionError, RuntimeError):
                return False
        return True

    async def _from_hub(self, data: bytes) -> None:
        try:
            kind, payload = frames.parse(data)
            body = frames.body_of(payload) if kind in (frames.VIEW, frames.ACK) else {}
            event = frames.parse_event(payload) if kind == frames.INPUT else None
        except FrameError as exc:
            log.warning("browser view frame dropped", extra={"run_id": self.run_id, "error": str(exc)})
            return
        if kind == frames.VIEW:
            self.viewing, self.login = body.get("viewing") is True, body.get("login") is True
            await self._check_mode()
            if self.viewing:
                await self._cast()
            else:
                await self._uncast()
        elif kind == frames.ACK:
            self._acked(body.get("seq"))
        elif event is not None:
            await self._input(event)

    async def _nobody_views(self) -> None:
        self.viewing = False
        with contextlib.suppress(CdpError):
            await self._uncast()

    # The mode and what the viewer sees

    async def _check_mode(self) -> None:
        now = mode(self.state_of(), self.login)
        if now == self.mode:
            return
        self.mode = now
        log.info("browser view mode", extra={"run_id": self.run_id, "mode": now})
        if now == frames.HUMAN:
            for target_id, tab in list(self.tabs.items()):
                if not allowed(tab["url"]):
                    await self._close_tab(target_id)
        await self._publish()

    def _state(self, notice: str | None) -> bytes:
        tabs = [{"id": key, "url": tab["url"], "title": tab["title"]} for key, tab in self.tabs.items()]
        body = {"mode": self.mode, "tabs": tabs, "active": self.active}
        if notice is not None:
            body["notice"] = notice
        try:
            return frames.message(frames.STATE, body)
        except FrameError:  # tabs past what a frame holds: the one watched alone
            body["tabs"] = [tab for tab in tabs if tab["id"] == self.active]
            return frames.message(frames.STATE, body)

    async def _publish(self, notice: str | None = None) -> None:
        """Send STATE when it changed, or to say ``notice``."""
        data = self._state(notice)
        if notice is None and data == self.sent:
            return
        self.sent = None if notice is not None else data
        await self._send(data)

    async def _cast(self) -> None:
        if self.casting or self.session is None:
            return
        self.casting = True
        params = {"format": "jpeg", "quality": QUALITY, "maxWidth": MAX_WIDTH, "maxHeight": MAX_HEIGHT}
        await self.cdp.call("Page.startScreencast", {**params, "everyNthFrame": 1}, session=self.session)

    async def _uncast(self) -> None:
        self._ack_waiting()
        casting, self.casting = self.casting, False
        if casting and self.session is not None:
            await self.cdp.call("Page.stopScreencast", session=self.session)

    def _frame(self, params: dict, session: str | None) -> None:
        """A screencast frame, or a still (no ``sessionId``: Chromium waits on no ack for it): sent to the viewer when
        it drew the last one, held until it did otherwise, and acked to Chromium once the viewer drew it, so the viewer
        sets the pace."""
        number = params.get("sessionId")
        tab = self.tabs.get(self.active or "")
        shown = tab is not None and (self.mode == frames.AGENT or allowed(tab["url"]))
        if session != self.session or not self.viewing or self.hub is None or not shown:
            self._spawn(self._ack(session, number))
        elif self.unacked is not None:
            if self.held is not None:  # a newer frame replaces the one held
                self._spawn(self._ack(*self.held[:2]))
            self.held = (session, number, params)
        else:
            self._show(session, number, params)

    def _visibility(self, params: dict, session: str | None) -> None:
        """The tab watched went to the background, or was there when its screencast started: Chromium paints it no
        more, so the viewer gets a still of it."""
        if params.get("visible") is False and session == self.session and self.casting:
            self._spawn(self._still(session))

    async def _still(self, session: str) -> None:
        """One picture of the tab watched while it is in the background, sent as a screen frame with nothing to ack to
        Chromium; its header gives its size in pixels, the CSS pixels of the worker's Chromium (scale factor 1). The
        broker brings no tab forward for it, since that would send the agent's tab to the background; a picture over
        MAX_WIDTH x MAX_HEIGHT (a screen the agent emulates) is dropped."""
        shot = await self.cdp.call("Page.captureScreenshot", {"format": "jpeg", "quality": QUALITY}, session=session)
        data = shot.get("data") or ""
        try:
            size = jpeg_size(base64.b64decode(data, validate=True))
        except (binascii.Error, ValueError):
            size = None
        if size is None or size[0] > MAX_WIDTH or size[1] > MAX_HEIGHT:
            log.info("browser view still dropped", extra={"run_id": self.run_id, "size": size})
            return
        metadata = {"deviceWidth": size[0], "deviceHeight": size[1], "offsetTop": 0, "pageScaleFactor": 1}
        self._frame({"data": data, "metadata": metadata}, session)

    def _show(self, session: str | None, number, params: dict) -> None:
        metadata = params.get("metadata") or {}
        header = {
            "seq": self.seq + 1,
            "width": metadata.get("deviceWidth"),
            "height": metadata.get("deviceHeight"),
            "top": metadata.get("offsetTop", 0),
            "scale": metadata.get("pageScaleFactor", 1),
        }
        try:
            data = frames.screen(header, base64.b64decode(params.get("data") or "", validate=True))
        except (FrameError, binascii.Error, ValueError):  # over MAX_FRAME_BYTES: dropped, the next may fit
            self._spawn(self._ack(session, number))
            return
        self.seq += 1
        self.unacked = (self.seq, session, number)
        self.ack_timer = asyncio.get_running_loop().call_later(ACK_WAIT, self._acked, self.seq)
        self._spawn(self._send(data))

    def _acked(self, seq) -> None:
        """The viewer drew screen frame ``seq`` (or ACK_WAIT passed): send the frame held, and Chromium may send the
        next."""
        if self.unacked is not None and isinstance(seq, int) and seq >= self.unacked[0]:
            held, self.held = self.held, None
            self._ack_waiting()
            if held is not None:
                self._show(*held)

    def _ack_waiting(self) -> None:
        """Ack to Chromium the frames the viewer has not drawn: it sends none of this tab, or the viewer left."""
        waiting, held, self.unacked, self.held = self.unacked, self.held, None, None
        if self.ack_timer is not None:
            self.ack_timer.cancel()
            self.ack_timer = None
        for frame in (waiting[1:] if waiting else None, held[:2] if held else None):
            if frame is not None:
                self._spawn(self._ack(*frame))

    async def _ack(self, session: str | None, number) -> None:
        if session is not None and number is not None:
            await self.cdp.call("Page.screencastFrameAck", {"sessionId": number}, session=session)

    # The tabs

    def _on_event(self, method: str, params: dict, session: str | None) -> None:
        handler = self._events.get(method)
        if handler is not None:
            handler(params, session)

    async def _discover(self) -> None:
        await self.cdp.call("Target.setDiscoverTargets", {"discover": True})
        for info in (await self.cdp.call("Target.getTargets")).get("targetInfos") or ():
            self._seen(info)
        first = next(iter(self.tabs), None)
        if first is None:
            first = (await self.cdp.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
            self.tabs.setdefault(first, {"url": "about:blank", "title": "", "opener": None})
        await self._watch(first)
        self.ready = True

    def _seen(self, info: dict) -> bool:
        """Note a page of the browser, or what changed of it; whether it is one."""
        if info.get("type") != "page" or not info.get("targetId"):
            return False
        tab = self.tabs.setdefault(info["targetId"], {"opener": info.get("openerId")})
        tab["url"] = str(info.get("url") or "")[: frames.MAX_URL_CHARS]
        tab["title"] = str(info.get("title") or "")[:MAX_TITLE_CHARS]
        return True

    def _created(self, params: dict, _session) -> None:
        info = params.get("targetInfo") or {}
        if self._seen(info):
            self._spawn(self._opened(info["targetId"], bool(info.get("openerId"))))

    async def _opened(self, target_id: str, opened_by_a_page: bool) -> None:
        if self.mode == frames.HUMAN and not allowed(self.tabs.get(target_id, {}).get("url", "")):
            await self._close_tab(target_id)
        elif self.ready and opened_by_a_page:  # a popup, or a link to a new tab: the viewer follows it
            await self._watch(target_id, front=self.mode == frames.HUMAN)
        await self._publish()

    def _changed(self, params: dict, _session) -> None:
        info = params.get("targetInfo") or {}
        if info.get("targetId") in self.tabs and self._seen(info):
            if self.mode == frames.HUMAN and not allowed(self.tabs[info["targetId"]]["url"]):
                self._spawn(self._close_tab(info["targetId"]))
            else:
                self._spawn(self._publish())

    def _destroyed(self, params: dict, _session) -> None:
        target_id = params.get("targetId")
        tab = self.tabs.pop(target_id, None)
        if tab is not None:
            self._spawn(self._gone(target_id, tab))

    def _detached(self, params: dict, _session) -> None:
        if params.get("sessionId") == self.session and self.session is not None:
            self.session, self.casting = None, False
            self._ack_waiting()

    async def _gone(self, target_id: str, tab: dict) -> None:
        """A tab closed: when it was the one watched, watch the one that opened it, or the last one."""
        if target_id == self.active and not self.closing.is_set():
            self.active = self.session = None
            self.casting = False
            self._ack_waiting()
            following = tab.get("opener") if tab.get("opener") in self.tabs else next(reversed(self.tabs), None)
            if following is None:
                following = (await self.cdp.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
                self.tabs.setdefault(following, {"url": "about:blank", "title": "", "opener": None})
            await self._watch(following, front=self.mode == frames.HUMAN)
        await self._publish()

    async def _watch(self, target_id: str, *, front: bool = False) -> None:
        """Watch the tab ``target_id``: attach to it, and screencast it while someone views; ``front`` brings it to
        the front."""
        async with self.watch_lock:
            if target_id not in self.tabs:
                return
            if target_id != self.active or self.session is None:
                old, self.session, self.casting = self.session, None, False
                self._ack_waiting()
                if old is not None:
                    with contextlib.suppress(CdpError):
                        await self.cdp.call("Target.detachFromTarget", {"sessionId": old})
                self.active = target_id
                attached = await self.cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
                self.session = attached["sessionId"]
                await self.cdp.call("Page.enable", session=self.session)
            if front:
                await self.cdp.call("Target.activateTarget", {"targetId": target_id})
            if self.viewing:
                await self._cast()
        await self._publish()

    async def _close_tab(self, target_id: str) -> None:
        """Close a tab on a scheme a person may not see, opening a blank one first when it is the last."""
        tab = self.tabs.get(target_id)
        if tab is None:
            return
        scheme = scheme_of(tab["url"]) or "an unknown scheme"
        if len(self.tabs) == 1:
            with contextlib.suppress(CdpError, KeyError):
                blank = (await self.cdp.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
                self.tabs.setdefault(blank, {"url": "about:blank", "title": "", "opener": None})
        log.info("browser view closed a tab", extra={"run_id": self.run_id, "scheme": scheme})
        with contextlib.suppress(CdpError):
            await self.cdp.call("Target.closeTarget", {"targetId": target_id})
        allowed_text = ", ".join(sorted(ALLOWED_SCHEMES))
        await self._publish(f"a tab on {scheme}: was closed; while you drive, tabs stay open on {allowed_text}")

    # The viewer's events

    async def _input(self, event: dict) -> None:
        kind = event["type"]
        human = self.mode == frames.HUMAN
        if kind == "tab":
            await self._watch(event["id"], front=human)
            return
        if not human or self.session is None:
            return  # the agent drives the browser: the viewer watches
        if kind in self._navigations:
            self._spawn(self._navigations[kind](event))  # a page that loads slowly holds no event behind it
            return
        await self._inputs[kind](event)

    async def _page(self, method: str, params: dict | None = None) -> dict:
        return await self.cdp.call(method, params, session=self.session)

    async def _mouse(self, event: dict) -> None:
        pressing = event["action"] != "move" and event["button"] != "none"
        await self._page(
            "Input.dispatchMouseEvent",
            {
                "type": _MOUSE[event["action"]],
                "x": event["x"],
                "y": event["y"],
                "button": event["button"],
                "buttons": event["buttons"],
                "clickCount": event["clicks"] or (1 if pressing else 0),
                "modifiers": event["modifiers"],
            },
        )

    async def _wheel(self, event: dict) -> None:
        params = {"type": "mouseWheel", "x": event["x"], "y": event["y"], "modifiers": event["modifiers"]}
        await self._page("Input.dispatchMouseEvent", {**params, "deltaX": event["dx"], "deltaY": event["dy"]})

    async def _key(self, event: dict) -> None:
        typing = event["action"] == "down" and event["text"]
        params = {
            "type": "keyUp" if event["action"] == "up" else ("keyDown" if typing else "rawKeyDown"),
            "key": event["key"],
            "code": event["code"],
            "windowsVirtualKeyCode": event["keyCode"],
            "nativeVirtualKeyCode": event["keyCode"],
            "modifiers": event["modifiers"],
        }
        if typing:
            params["text"] = params["unmodifiedText"] = event["text"]
        await self._page("Input.dispatchKeyEvent", params)

    async def _text(self, event: dict) -> None:
        """Text an input method composed, or text pasted: typed as it is, never as keys."""
        if event["text"]:
            await self._page("Input.insertText", {"text": event["text"]})

    async def _history(self, event: dict) -> None:
        history = await self._page("Page.getNavigationHistory")
        entries = history.get("entries") or []
        index = int(history.get("currentIndex", 0)) + (-1 if event["type"] == "back" else 1)
        if 0 <= index < len(entries):
            await self._page("Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]})

    async def _reload(self, _event: dict) -> None:
        await self._page("Page.reload")

    async def _open(self, event: dict) -> None:
        url = openable(event["url"])
        if url is None:
            await self._publish(REFUSED_URL)
            return
        await self._page("Page.navigate", {"url": url})
