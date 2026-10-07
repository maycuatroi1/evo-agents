"""The web terminal: two websockets the api joins in its own memory, the browser's and the worker's.
``docs/workers.md`` is the protocol and ``evo_agents.hub.terminal`` the wire format and close codes.

``Authenticate`` reads no credential of a websocket and lets only these two through
(``security.SELF_CHECKED_WEBSOCKETS``), so each end checks its own. Both accept the socket before checking, so the
client sees the close code: a websocket refused during its handshake shows a page nothing but 1006.

WS /v1/projects/{p}/runs/{id}/terminal is the browser's end. It closes at the first check that fails, in this order:
an Origin equal to EVO_HUB_PUBLIC_URL's (CLOSE_FORBIDDEN, before anything about the session is looked at, so a page
of another site learns nothing), the session cookie (CLOSE_UNAUTHENTICATED without one), the hello within
HELLO_SECONDS (CLOSE_TIMEOUT) carrying the session's X-Evo-CSRF value (CLOSE_FORBIDDEN), a live web session
(CLOSE_UNAUTHENTICATED) created within SESSION_MAX_AGE (CLOSE_FORBIDDEN), a run the caller may read, which it
dispatched, held by a worker it owns that allows the web terminal, in a state of OPEN_STATES (CLOSE_FORBIDDEN), and no
other browser on the run's terminal (CLOSE_BUSY). A run that is leased or running is asked for a takeover then, as
POST .../takeover asks, since a person drives the agent only in an interactive run; the session is audited
(terminal.open) in the same transaction.

The heartbeat's terminal_open is true for a run while a browser waits for the worker's end (``Terminals.waiting``).
WS /v1/worker/runs/{id}/terminal is that end: the worker protocol header (CLOSE_UPGRADE), the worker's evw_ token as
``Authorization: Bearer`` (CLOSE_UNAUTHENTICATED without one or for one that is not live, CLOSE_FORBIDDEN for another
kind of credential), the live worker of that token holding the run, which is interactive (CLOSE_FORBIDDEN), and a
browser waiting on the run (CLOSE_FORBIDDEN), whose worker's end is not connected yet (CLOSE_BUSY).

Once both ends are there, the worker gets the browser's size first (a RESIZE frame, when the hello or a resize since
gave one), then every frame as it comes: INPUT and RESIZE from the browser, OUTPUT from the worker. Input that comes
before the worker's end does is dropped, and a resize then only sets the size the worker gets first. A text message
after the hello, an empty frame or a type the end may not send closes the session with CLOSE_UNSUPPORTED, a frame over
MAX_FRAME_BYTES with CLOSE_TOO_BIG. The session ends when either end leaves (the other gets CLOSE_NORMAL), after
IDLE_SECONDS without a frame either way, and after MAX_SECONDS in all (both get CLOSE_TIMEOUT); it is audited
(terminal.close) with the bytes relayed each way and how it ended.

Everything lives in this process (``Terminals``), as the hub runs one api process: another process would not know a
browser waits.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from urllib.parse import urlsplit

import anyio
import psycopg
from fastapi import APIRouter, HTTPException, WebSocket
from sqlalchemy import exc as sa_exc
from starlette.websockets import WebSocketDisconnect, WebSocketState

from evo_agents.hub import runs
from evo_agents.hub import terminal as frames
from evo_agents.hub.db import legacy
from evo_agents.hub.runs import PROTOCOL_HEADER, PROTOCOL_VERSION
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.runs import ASK_TAKEOVER, NOT_HELD, RunId, _run_target, readable_run
from evo_agents.hub.server.security import (
    JOIN_HINT,
    PREFIXES,
    SESSION_COOKIE,
    WEB,
    WORKER,
    Principal,
    authenticate,
    csrf_token,
    hash_token,
    same,
)

log = logging.getLogger(__name__)

HELLO_SECONDS = 10.0  # the browser's first message, its CSRF value, comes within this
IDLE_SECONDS = 15 * 60  # a session without a frame either way for this long is closed
MAX_SECONDS = 4 * 60 * 60  # a session is closed this long after it opened, busy or not
SESSION_MAX_AGE = timedelta(hours=12)  # a web session older than this opens no terminal: sign in again
OPEN_STATES = ("leased", "running", "interactive")  # a browser opens the terminal of a run in one of these
WORKER_STATES = ("interactive",)  # the worker connects its end only once a person may drive the agent
DEFAULT_PORTS = {"http": 80, "https": 443}
SOCKET_ERRORS = (WebSocketDisconnect, RuntimeError, OSError)  # a socket that went away while being written to

router = APIRouter(prefix="/v1/projects", tags=["runs"])
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"])


class Refusal(Exception):
    """Close the socket with ``code`` and ``reason``: the request may not open the terminal."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class Session:
    """One browser on one run's terminal, and the worker's end once it connects."""

    def __init__(self, terminals: Terminals, *, run_id: int, browser: WebSocket, user: Principal):
        self.terminals = terminals
        self.run_id = run_id
        self.worker_id: int | None = None  # the worker that held the run when the browser opened it
        self.browser = browser
        self.worker: WebSocket | None = None
        self.user = user
        self.size: tuple[int, int] | None = None  # the browser's columns and rows, sent to the worker first
        self.relaying = False  # both ends are there and frames pass
        self.ended = asyncio.Event()  # set once, by the first end()
        self.closed = asyncio.Event()  # set when that end() has audited the session and closed both ends
        self.how = ""  # one word for the audit: browser, worker, idle, timeout, protocol, ...
        self.to_worker = 0  # bytes of the frames the hub sent the worker
        self.to_browser = 0
        self.dropped = 0  # bytes of input that came before the worker's end
        self.project_id: int | None = None
        self.target = ""  # the run as audit names it
        self._locks = {id(browser): asyncio.Lock()}
        loop = asyncio.get_running_loop()
        self.opened = self.active = loop.time()

    def touch(self) -> None:
        self.active = asyncio.get_running_loop().time()

    async def send(self, websocket: WebSocket, data: bytes) -> bool:
        """Send a frame to one end; False when that end is gone."""
        async with self._locks.setdefault(id(websocket), asyncio.Lock()):
            try:
                await websocket.send_bytes(data)
            except SOCKET_ERRORS:
                return False
        if websocket is self.browser:
            self.to_browser += len(data)
        else:
            self.to_worker += len(data)
        return True

    async def attach(self, websocket: WebSocket) -> None:
        """The worker's end connected: send it the browser's size, then let frames pass."""
        self.worker = websocket
        sent = None
        while self.size is not None and self.size != sent and not self.ended.is_set():
            sent = self.size  # a resize may come while this one is sent; the worker gets the last one
            if not await self.send(websocket, frames.resize(*sent)):
                await self.end("the worker's end went away", "worker")
                return
        self.relaying = True
        self.touch()

    async def end(self, reason: str, how: str, *, browser_code: int = 1000, worker_code: int = 1000) -> None:
        """End the session once: audit it, then close both ends. A later call waits until the first is done, so no
        handler returns while its socket is still being closed."""
        if self.ended.is_set():
            await self.closed.wait()
            return
        self.ended.set()
        self.how = how
        self.terminals.drop(self)
        with anyio.CancelScope(shield=True):  # recorded even when the browser's task is being cancelled
            try:
                await self._record_close()
                await self._close(self.browser, browser_code, reason)
                if self.worker is not None:
                    await self._close(self.worker, worker_code, reason)
            finally:
                self.closed.set()
        log.info(
            "terminal closed",
            extra={
                "run_id": self.run_id,
                "how": how,
                "to_worker": self.to_worker,
                "to_browser": self.to_browser,
                "dropped": self.dropped,
                "login": self.user.login,
            },
        )

    async def _close(self, websocket: WebSocket, code: int, reason: str) -> None:
        async with self._locks.setdefault(id(websocket), asyncio.Lock()):
            await close(websocket, code, reason)

    async def _record_close(self) -> None:
        if self.project_id is None:
            return
        target = f"{self.target} to_worker={self.to_worker} to_browser={self.to_browser} end={self.how}"
        try:
            async with self.browser.app.state.engine.begin() as conn:
                await audit.record(
                    conn,
                    actor_id=self.user.user_id,
                    token_id=self.user.token_id,
                    action=audit.TERMINAL_CLOSE,
                    target=target,
                    project_id=self.project_id,
                )
        except (
            psycopg.Error,
            sa_exc.DBAPIError,
            OSError,
        ) as exc:  # the session is over either way; the log keeps what was lost
            log.warning(
                "terminal close not audited",
                extra={"run_id": self.run_id, "target": target, "error": type(exc).__name__},
            )


class Terminals:
    """The terminal sessions of this api process, at most one per run."""

    def __init__(self):
        self._sessions: dict[int, Session] = {}

    def waiting(self, run_id: int) -> bool:
        """Whether a browser waits for the worker's end of run ``run_id``: the heartbeat's terminal_open."""
        session = self._sessions.get(run_id)
        return session is not None and session.worker is None and not session.ended.is_set()

    def get(self, run_id: int) -> Session | None:
        session = self._sessions.get(run_id)
        return None if session is None or session.ended.is_set() else session

    def reserve(self, session: Session) -> bool:
        if self.get(session.run_id) is not None:
            return False
        self._sessions[session.run_id] = session
        return True

    def drop(self, session: Session) -> None:
        if self._sessions.get(session.run_id) is session:
            del self._sessions[session.run_id]

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.end("the hub is shutting down", "shutdown", browser_code=1001, worker_code=1001)


async def close(websocket: WebSocket, code: int, reason: str) -> None:
    """Close ``websocket`` unless it is closed already."""
    if websocket.application_state == WebSocketState.DISCONNECTED:
        return
    try:
        await websocket.close(code, frames.close_reason(reason))
    except SOCKET_ERRORS:
        pass


def _origin(url: str | None) -> str | None:
    """``url`` as a browser writes its origin, scheme://host[:port] in lower case without a default port."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}" if port in (None, DEFAULT_PORTS.get(scheme)) else f"{scheme}://{host}:{port}"


# The browser's end


async def _hello(websocket: WebSocket) -> dict | None:
    """The browser's first message as a dict, or None when the browser left first."""
    try:
        message = await asyncio.wait_for(websocket.receive(), HELLO_SECONDS)
    except TimeoutError:
        raise Refusal(frames.CLOSE_TIMEOUT, f"no hello within {HELLO_SECONDS:g} seconds") from None
    if message["type"] == "websocket.disconnect":
        return None
    text = message.get("text")
    hello = None
    if text is not None and len(text) <= frames.MAX_FRAME_BYTES:
        try:
            hello = json.loads(text)
        except ValueError:
            hello = None
    if not isinstance(hello, dict):
        hint = 'the first message is the hello: {"csrf": ..., "cols": ..., "rows": ...}'
        raise Refusal(frames.CLOSE_UNSUPPORTED, hint)
    return hello


async def _open(websocket: WebSocket, project: str, run_id: int) -> Session | None:
    """Check the browser's request and open its session; Refusal when it may not, None when it left first."""
    app = websocket.app
    config = app.state.config
    if config.web_login_missing():
        raise Refusal(frames.CLOSE_FORBIDDEN, "the web terminal needs web sign-in, which this hub has not set up")
    if _origin(websocket.headers.get("origin")) != _origin(config.public_url):
        raise Refusal(frames.CLOSE_FORBIDDEN, "the terminal opens only from a page of this hub (Origin)")
    token = websocket.cookies.get(SESSION_COOKIE)
    if not token:
        raise Refusal(frames.CLOSE_UNAUTHENTICATED, "sign in on the web to open a terminal")
    hello = await _hello(websocket)
    if hello is None:
        return None
    given = hello.get("csrf")
    if not isinstance(given, str) or not same(given, csrf_token(config.session_secret, hash_token(token))):
        raise Refusal(frames.CLOSE_FORBIDDEN, "the hello needs the X-Evo-CSRF value of GET /v1/auth/web/csrf")
    size = None
    if "cols" in hello or "rows" in hello:
        try:
            frames.check_size(hello.get("cols"), hello.get("rows"))
        except frames.FrameError as exc:
            raise Refusal(frames.CLOSE_UNSUPPORTED, f"the hello's size: {exc}") from None
        size = (hello["cols"], hello["rows"])
    engine = app.state.engine
    terminals: Terminals = app.state.terminals
    try:
        user = await authenticate(engine, token, WEB, config)
        if user is None:
            raise Refusal(frames.CLOSE_UNAUTHENTICATED, "the web session is revoked, expired or unknown: sign in again")
        session = Session(terminals, run_id=run_id, browser=websocket, user=user)
        session.size = size
        try:
            async with engine.begin() as conn:  # the reservation stands only once the transaction committed
                await _open_session(conn, session, project)
        except BaseException:
            terminals.drop(session)
            raise
        return session
    except (
        psycopg.OperationalError,
        sa_exc.OperationalError,
        OSError,
    ) as exc:  # PoolTimeout is an OperationalError; bugs stay errors
        log.warning("cannot open a terminal: database unavailable", extra={"error": type(exc).__name__})
        raise Refusal(frames.CLOSE_UNAVAILABLE, "the hub database is unavailable; try again shortly") from None


FRESH_SESSION = "SELECT created_at > now() - %s FROM tokens WHERE id = %s"
TERMINAL_RUN = """
SELECT r.dispatched_by, u.login, r.state, r.plan_id, r.step_key, r.worker_id, w.owner_id, w.name,
       w.allow_web_terminal
  FROM runs r JOIN users u ON u.id = r.dispatched_by LEFT JOIN workers w ON w.id = r.worker_id
 WHERE r.id = %s AND r.project_id = %s
   FOR UPDATE OF r
"""


async def _open_session(conn, session: Session, project: str) -> None:
    """Check the run and the session's age, reserve the run's terminal for ``session`` and audit it."""
    user, run_id = session.user, session.run_id
    fresh = await (await legacy(conn, FRESH_SESSION, (SESSION_MAX_AGE, user.token_id))).fetchone()
    if not fresh or not fresh[0]:
        hours = int(SESSION_MAX_AGE.total_seconds() // 3600)
        raise Refusal(frames.CLOSE_FORBIDDEN, f"the web session is older than {hours} hours: sign in again")
    try:
        access = await readable_run(conn, user, project, run_id, None)
    except HTTPException as exc:
        raise Refusal(frames.CLOSE_FORBIDDEN, str(exc.detail)) from None
    row = await (await legacy(conn, TERMINAL_RUN, (run_id, access.project_id))).fetchone()
    dispatched_by, owner, state, plan_id, key, worker_id, worker_owner, worker, allowed = row
    if dispatched_by != user.user_id:
        raise Refusal(frames.CLOSE_FORBIDDEN, f"only {owner}, who dispatched run {run_id}, may open its terminal")
    if state not in OPEN_STATES or worker_id is None:
        raise Refusal(
            frames.CLOSE_FORBIDDEN,
            f"run {run_id} is {state}: a terminal opens on a run that is leased, running or interactive",
        )
    if worker_owner != user.user_id:
        raise Refusal(frames.CLOSE_FORBIDDEN, f"run {run_id} is on worker {worker}, which is not yours")
    if not allowed:
        raise Refusal(frames.CLOSE_FORBIDDEN, f"worker {worker} does not allow the web terminal")
    session.worker_id = worker_id
    if not session.terminals.reserve(session):
        raise Refusal(frames.CLOSE_BUSY, f"the terminal of run {run_id} is open in another browser")
    target = _run_target(project, plan_id, key, run_id)
    if state in runs.TAKEOVER_STATES and await (await legacy(conn, ASK_TAKEOVER, (run_id,))).fetchone():
        await _audit(conn, user, access.project_id, audit.RUN_TAKEOVER, target)
    await _audit(conn, user, access.project_id, audit.TERMINAL_OPEN, target)
    session.project_id = access.project_id
    session.target = target


async def _audit(conn, user: Principal, project_id: int, action: str, target: str) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=project_id
    )


async def _from_browser(session: Session) -> None:
    """Relay the browser's frames to the worker until the session ends."""
    websocket = session.browser
    while not session.ended.is_set():
        try:
            message = await websocket.receive()
        except RuntimeError:  # the browser is gone already
            message = {"type": "websocket.disconnect"}
        if message["type"] == "websocket.disconnect":
            await session.end("the browser closed the terminal", "browser")
            return
        data = message.get("bytes")
        if data is None:
            await session.end("frames are binary after the hello", "protocol", browser_code=frames.CLOSE_UNSUPPORTED)
            return
        try:
            kind, payload = frames.parse(data)
            if kind not in frames.FROM_BROWSER:
                raise frames.FrameError("a browser sends input (0) and resize (2) frames")
            size = frames.parse_resize(payload) if kind == frames.RESIZE else None
        except frames.FrameError as exc:
            code = frames.CLOSE_TOO_BIG if len(data) > frames.MAX_FRAME_BYTES else frames.CLOSE_UNSUPPORTED
            await session.end(str(exc), "protocol", browser_code=code)
            return
        session.touch()
        if size is not None:
            session.size = size
        if not session.relaying:  # the worker gets the size when it connects; input before that is lost
            if kind == frames.INPUT:
                session.dropped += len(data)
            continue
        if not await session.send(session.worker, data):
            await session.end("the worker's end went away", "worker")
            return


async def _watch(session: Session) -> None:
    """End the session once it was idle for IDLE_SECONDS, or open for MAX_SECONDS."""
    loop = asyncio.get_running_loop()
    while not session.ended.is_set():
        now = loop.time()
        idle_left = session.active + IDLE_SECONDS - now
        open_left = session.opened + MAX_SECONDS - now
        if open_left <= 0:
            reason = f"a terminal stays open at most {MAX_SECONDS / 3600:g} hours"
            await session.end(reason, "timeout", browser_code=frames.CLOSE_TIMEOUT, worker_code=frames.CLOSE_TIMEOUT)
            return
        if idle_left <= 0:
            reason = f"closed after {IDLE_SECONDS / 60:g} idle minutes"
            await session.end(reason, "idle", browser_code=frames.CLOSE_TIMEOUT, worker_code=frames.CLOSE_TIMEOUT)
            return
        try:
            await asyncio.wait_for(session.ended.wait(), min(idle_left, open_left))
        except TimeoutError:
            pass


@router.websocket("/{project}/runs/{run_id}/terminal")
async def browser_terminal(websocket: WebSocket, project: ProjectName, run_id: RunId) -> None:
    """The browser's end of the run's terminal (the run's owner, on a worker of theirs that allows it)."""
    await websocket.accept()
    try:
        session = await _open(websocket, project, run_id)
    except Refusal as refusal:
        log.info("terminal refused", extra={"run_id": run_id, "end": "browser", "code": refusal.code})
        await close(websocket, refusal.code, refusal.reason)
        return
    if session is None:  # the browser left before its hello
        return
    log.info("terminal opened", extra={"run_id": run_id, "login": session.user.login})
    watcher = asyncio.create_task(_watch(session))
    try:
        await _from_browser(session)
    finally:
        watcher.cancel()
        await session.end("the browser closed the terminal", "browser")


# The worker's end


def _bearer(websocket: WebSocket) -> str:
    authorization = websocket.headers.get("authorization")
    if authorization is None:
        raise Refusal(frames.CLOSE_UNAUTHENTICATED, f"a worker token is required: {JOIN_HINT}")
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Refusal(frames.CLOSE_UNAUTHENTICATED, f"the Authorization header must be `Bearer <token>`: {JOIN_HINT}")
    token = token.strip()
    if not token.startswith(PREFIXES[WORKER]):
        raise Refusal(frames.CLOSE_FORBIDDEN, f"the worker's end takes a worker token (evw_...): {JOIN_HINT}")
    return token


HOLDING_WORKER = "SELECT id, name, revoked_at FROM workers WHERE token_id = %s"
HELD_RUN = "SELECT worker_id, state FROM runs WHERE id = %s"


async def _attach(websocket: WebSocket, run_id: int) -> Session:
    """Check the worker's request and join it to the browser waiting on the run; Refusal when it may not."""
    given = websocket.headers.get(PROTOCOL_HEADER)
    if given is None or given.strip() != PROTOCOL_VERSION:
        raise Refusal(
            frames.CLOSE_UPGRADE,
            f"this hub speaks version {PROTOCOL_VERSION} of the worker protocol: upgrade evo-agents on the worker",
        )
    token = _bearer(websocket)
    app = websocket.app
    try:
        user = await authenticate(app.state.engine, token, WORKER, app.state.config)
        if user is None:
            raise Refusal(frames.CLOSE_UNAUTHENTICATED, f"the worker token is revoked, expired or unknown: {JOIN_HINT}")
        async with app.state.engine.begin() as conn:
            worker = await (await legacy(conn, HOLDING_WORKER, (user.token_id,))).fetchone()
            held = await (await legacy(conn, HELD_RUN, (run_id,))).fetchone()
    except (psycopg.OperationalError, sa_exc.OperationalError, OSError) as exc:
        log.warning("cannot attach a terminal: database unavailable", extra={"error": type(exc).__name__})
        raise Refusal(frames.CLOSE_UNAVAILABLE, "the hub database is unavailable; try again shortly") from None
    if worker is None or worker[2] is not None:
        raise Refusal(frames.CLOSE_FORBIDDEN, "this worker token belongs to no live worker: join the machine again")
    worker_id, name, _ = worker
    if held is None or held[0] != worker_id or held[1] not in runs.HELD_STATES:
        raise Refusal(frames.CLOSE_FORBIDDEN, NOT_HELD.format(id=run_id))
    if held[1] not in WORKER_STATES:
        raise Refusal(frames.CLOSE_FORBIDDEN, f"run {run_id} is {held[1]}: connect its terminal once it is interactive")
    session = app.state.terminals.get(run_id)
    if session is None or session.worker_id != worker_id:
        raise Refusal(frames.CLOSE_FORBIDDEN, f"no browser waits for the terminal of run {run_id}")
    if session.worker is not None:
        raise Refusal(frames.CLOSE_BUSY, f"the worker's end of run {run_id}'s terminal is connected already")
    log.info("terminal attached", extra={"run_id": run_id, "worker_id": worker_id, "worker": name})
    await session.attach(websocket)
    return session


async def _from_worker(session: Session, websocket: WebSocket) -> None:
    """Relay the worker's frames to the browser until the session ends."""
    while not session.ended.is_set():
        try:
            message = await websocket.receive()
        except RuntimeError:
            message = {"type": "websocket.disconnect"}
        if message["type"] == "websocket.disconnect":
            await session.end("the worker closed the terminal", "worker")
            return
        data = message.get("bytes")
        if data is None:
            await session.end("the worker's frames are binary", "protocol", worker_code=frames.CLOSE_UNSUPPORTED)
            return
        try:
            kind, _ = frames.parse(data)
            if kind not in frames.FROM_WORKER:
                raise frames.FrameError("a worker sends output (1) frames")
        except frames.FrameError as exc:
            code = frames.CLOSE_TOO_BIG if len(data) > frames.MAX_FRAME_BYTES else frames.CLOSE_UNSUPPORTED
            await session.end(str(exc), "protocol", worker_code=code)
            return
        session.touch()
        if not await session.send(session.browser, data):
            await session.end("the browser closed the terminal", "browser")
            return


@worker_router.websocket("/runs/{run_id}/terminal")
async def worker_terminal(websocket: WebSocket, run_id: RunId) -> None:
    """The worker's end of the run's terminal, connected when the heartbeat says terminal_open."""
    await websocket.accept()
    try:
        session = await _attach(websocket, run_id)
    except Refusal as refusal:
        log.info("terminal refused", extra={"run_id": run_id, "end": "worker", "code": refusal.code})
        await close(websocket, refusal.code, refusal.reason)
        return
    try:
        await _from_worker(session, websocket)
    finally:
        if session.worker is websocket:
            await session.end("the worker closed the terminal", "worker")
        await close(websocket, frames.CLOSE_NORMAL, "the terminal session ended")
