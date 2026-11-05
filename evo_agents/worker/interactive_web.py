"""The worker's end of the web terminal (``evo_agents.hub.terminal`` holds the frames): a websocket to the hub's
relay joined to ``tmux attach`` of the run's session on a PTY (``interactive_pty``)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from evo_agents import __version__
from evo_agents.hub import terminal as frames
from evo_agents.hub.runs import PROTOCOL_HEADER, PROTOCOL_VERSION
from evo_agents.worker.interactive_pty import Attached, Ring, pieces
from evo_agents.worker.interactive_tmux import Tmux, session_name

log = logging.getLogger("evo_agents.worker")

DEFAULT_SIZE = (80, 24)  # columns and rows of the PTY until the browser says its size
FIRST_FRAME_WAIT = 0.5  # seconds the bridge waits for the browser's size before it attaches
CONNECT_TIMEOUT = 30.0  # seconds for the websocket's handshake
PING_SECONDS = 30.0  # websocket pings, for the proxies between the worker and the hub
CLOSE_GRACE = 10.0
USER_AGENT = f"evo-agents-worker/{__version__}"


def ws_url(hub_url: str, run_id: int) -> str:
    """The worker's end of run ``run_id``'s terminal on the hub at ``hub_url`` (https -> wss, http -> ws)."""
    scheme, _, rest = hub_url.partition("://")
    socket_scheme = {"https": "wss", "http": "ws"}.get(scheme.lower())
    if socket_scheme is None:
        raise ValueError(f"{hub_url} is neither https nor http")
    return f"{socket_scheme}://{rest.rstrip('/')}/v1/worker/runs/{int(run_id)}/terminal"


class WebTerminal:
    """The worker's end of one run's web terminal: connections to the hub's relay, one at a time, each joined to
    ``tmux attach`` of the run's session on a PTY; the output ring outlives them, for the next browser."""

    def __init__(self, run_id: int, tmux: Tmux, *, allowed: bool, worker: str, config_path: Path | None = None):
        self.run_id = int(run_id)
        self.name = session_name(run_id)
        self.tmux = tmux
        self.allowed = allowed
        self.worker = worker
        self.config_path = config_path
        self.ring = Ring()
        self.connections = 0
        self._task: asyncio.Task | None = None
        self._closing = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    def open(self, http, hub_url: str, token: str) -> bool:
        """Connect to the hub's relay, unless a connection is open already; whether one starts."""
        if self.connected:
            return False
        self._closing = asyncio.Event()
        self._task = asyncio.create_task(self._serve(http, hub_url, token))
        return True

    async def close(self) -> None:
        """End the connection open now, if any (the run left the terminal)."""
        task = self._task
        if task is None or task.done():
            return
        self._closing.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), CLOSE_GRACE)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except Exception:
            pass

    async def _serve(self, http, hub_url: str, token: str) -> None:
        import aiohttp

        headers = {PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
        try:
            ws = await asyncio.wait_for(
                http.ws_connect(
                    ws_url(hub_url, self.run_id),
                    headers=headers,
                    max_msg_size=frames.MAX_FRAME_BYTES,
                    heartbeat=PING_SECONDS,
                ),
                CONNECT_TIMEOUT,
            )
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
            log.warning("terminal not connected to the hub", extra={"run_id": self.run_id, "error": type(exc).__name__})
            return
        self.connections += 1
        log.info("terminal connected", extra={"run_id": self.run_id, "allowed": self.allowed})
        try:
            if self.allowed:
                await self._bridge(ws)
            else:
                await self._refuse(ws)
        except Exception:
            log.exception("the web terminal failed", extra={"run_id": self.run_id})
        finally:
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close(code=frames.CLOSE_NORMAL, message=b"the terminal session ended")
            code = ws.close_code
            log.info("terminal closed", extra={"run_id": self.run_id, "code": code})

    async def _refuse(self, ws) -> None:
        where = f" in {self.config_path}" if self.config_path else ""
        text = (
            f"\r\nWorker {self.worker} does not allow the web terminal: allow_web_terminal is off{where}.\r\n"
            f"On that machine, `evo-agents worker attach {self.run_id}` opens this session.\r\n"
        )
        # The hub sends the browser's size first, when it knows it, and relays only once that is sent: a socket
        # closed before it would take the message along.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ws.receive(), FIRST_FRAME_WAIT)
        if ws.closed:
            return
        await ws.send_bytes(frames.frame(frames.OUTPUT, text.encode()))
        await ws.close(
            code=frames.CLOSE_FORBIDDEN,
            message=frames.close_reason("this worker does not allow the web terminal").encode(),
        )

    async def _bridge(self, ws) -> None:
        import aiohttp

        size = DEFAULT_SIZE
        early: list[bytes] = []
        try:  # the hub sends the browser's size first when it knows it
            first = await asyncio.wait_for(ws.receive(), FIRST_FRAME_WAIT)
        except TimeoutError:
            first = None
        if first is not None:
            if first.type != aiohttp.WSMsgType.BINARY:
                self._log_end(ws, first)
                return
            try:
                kind, payload = frames.parse(first.data)
                if kind == frames.RESIZE:
                    size = frames.parse_resize(payload)
                elif kind == frames.INPUT:
                    early.append(payload)
                else:
                    raise frames.FrameError("the hub sends a worker input (0) and resize (2) frames")
            except frames.FrameError as exc:
                await ws.close(code=frames.CLOSE_UNSUPPORTED, message=frames.close_reason(str(exc)).encode())
                return
        replay = self.ring.snapshot()
        for piece in pieces(replay):
            await ws.send_bytes(frames.frame(frames.OUTPUT, piece))
        attached = await Attached.start(self.tmux, self.name, size, self.ring)
        for data in early:
            attached.write(data)
        sender = asyncio.create_task(self._send(ws, attached))
        receiver = asyncio.create_task(self._receive(ws, attached))
        closing = asyncio.create_task(self._closing.wait())
        try:
            await asyncio.wait({sender, receiver, closing}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sender, receiver, closing):
                task.cancel()
            await asyncio.gather(sender, receiver, closing, return_exceptions=True)
            await attached.close()

    async def _send(self, ws, attached: Attached) -> None:
        while (data := await attached.output.get()) is not None:
            for piece in pieces(data):
                await ws.send_bytes(frames.frame(frames.OUTPUT, piece))
            attached.sent(len(data))

    async def _receive(self, ws, attached: Attached) -> None:
        import aiohttp

        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.BINARY:
                if message.type == aiohttp.WSMsgType.TEXT:
                    reason = frames.close_reason("frames are binary")
                    await ws.close(code=frames.CLOSE_UNSUPPORTED, message=reason.encode())
                self._log_end(ws, message)
                return
            try:
                kind, payload = frames.parse(message.data)
                if kind == frames.INPUT:
                    attached.write(payload)
                elif kind == frames.RESIZE:
                    attached.resize(*frames.parse_resize(payload))
                else:
                    raise frames.FrameError("the hub sends a worker input (0) and resize (2) frames")
            except frames.FrameError as exc:
                await ws.close(code=frames.CLOSE_UNSUPPORTED, message=frames.close_reason(str(exc)).encode())
                return

    def _log_end(self, ws, message) -> None:
        code = ws.close_code if ws.close_code is not None else message.data if isinstance(message.data, int) else None
        if code not in (None, frames.CLOSE_NORMAL):
            reason = message.extra if isinstance(message.extra, str) else None
            log.info("the hub closed the terminal", extra={"run_id": self.run_id, "code": code, "reason": reason})
