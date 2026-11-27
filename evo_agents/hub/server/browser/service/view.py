"""What the live view of a run's browser does (see ``routes.view``): the two ends of a run's channel, joined in this
process's memory (``BrowserViews``), their checks, the relay of frames either way, and the audit of each view and of
each time the viewer drives, in the viewer's name.

The limits (``models.view``) are read from their module as they are used, so a test may change one.
"""

from __future__ import annotations

import asyncio
import json
import logging

import anyio
import psycopg
from fastapi import HTTPException, WebSocket
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_view as frames
from evo_agents.hub import runs
from evo_agents.hub import terminal as codes
from evo_agents.hub.runs import PROTOCOL_HEADER, PROTOCOL_VERSION
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.browser.models import view as models
from evo_agents.hub.server.browser.repository import view as repository
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.audits import run_target
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import (
    JOIN_HINT,
    SESSION_COOKIE,
    WEB,
    WORKER,
    Principal,
    authenticate,
    csrf_token,
    hash_token,
    same,
)
from evo_agents.hub.server.terminal import (
    SESSION_MAX_AGE,
    SOCKET_ERRORS,
    Refusal,
    bearer,
    close,
    origin_of,
)

log = logging.getLogger(__name__)


class End:
    """One websocket of a run's channel, whose sends take turns."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.lock = asyncio.Lock()

    async def send(self, data: bytes) -> bool:
        """Send a frame; False when this end is gone."""
        async with self.lock:
            try:
                await self.websocket.send_bytes(data)
            except SOCKET_ERRORS:
                return False
        return True

    async def close(self, code: int, reason: str) -> None:
        async with self.lock:
            await close(self.websocket, code, reason)


class Viewer(End):
    """The dispatcher's websocket on the run's browser."""

    def __init__(self, websocket: WebSocket, user: Principal, *, project_id: int, target: str, login: bool):
        super().__init__(websocket)
        self.user = user
        self.project_id = project_id
        self.target = target  # the run as audit names it
        self.login = login  # the run waits on a login decision: the broker opens it in human mode
        self.frames = 0  # screen frames relayed to it
        self.inputs = 0  # events relayed from it to the worker
        self.controlled = 0  # of those, since it got control
        self.controlling = False  # the worker's STATE says human: browser.control start is audited
        self.how = ""
        self.ended = asyncio.Event()  # set once, by the first end_view
        self.closed = asyncio.Event()  # set once that end_view audited the view and closed the socket
        loop = asyncio.get_running_loop()
        self.opened = self.active = loop.time()

    def touch(self) -> None:
        self.active = asyncio.get_running_loop().time()


class Channel:
    """One run's browser: its worker's end, once connected, and its viewer, while someone watches."""

    def __init__(self, run_id: int):
        self.run_id = run_id
        self.worker: End | None = None
        self.worker_id: int | None = None
        self.viewer: Viewer | None = None
        self.state: bytes | None = None  # the worker's last STATE frame, for a viewer that joins
        self.mode = frames.AGENT

    def view(self) -> bytes:
        viewer = self.viewer
        login = viewer is not None and viewer.login
        return frames.message(frames.VIEW, {"viewing": viewer is not None, "login": login})

    def link(self) -> bytes:
        return frames.message(frames.LINK, {"worker": self.worker is not None})


class BrowserViews:
    """The browser channels of this api process, one per run."""

    def __init__(self):
        self._channels: dict[int, Channel] = {}

    def channel(self, run_id: int) -> Channel:
        return self._channels.setdefault(run_id, Channel(run_id))

    def get(self, run_id: int) -> Channel | None:
        return self._channels.get(run_id)

    def forget(self, channel: Channel) -> None:
        if channel.worker is None and channel.viewer is None and self._channels.get(channel.run_id) is channel:
            del self._channels[channel.run_id]

    async def close_all(self) -> None:
        for channel in list(self._channels.values()):
            if channel.viewer is not None:
                await end_view(self, channel, channel.viewer, "the hub is shutting down", "shutdown", code=1001)
            if channel.worker is not None:
                worker, channel.worker = channel.worker, None
                await worker.close(1001, "the hub is shutting down")
            self.forget(channel)


async def _record(viewer: Viewer, action: str, target: str) -> None:
    """An audit row in the viewer's name, in a transaction of its own; the log keeps one the database lost."""
    try:
        async with viewer.websocket.app.state.engine.begin() as conn:
            await Auditor(conn, viewer.project_id, viewer.user).record(action, target)
    except (psycopg.Error, sa_exc.DBAPIError, OSError) as exc:
        log.warning("browser view not audited", extra={"action": action, "target": target, "error": type(exc).__name__})


async def _control(channel: Channel) -> None:
    """Audit the viewer getting the browser to drive, or giving it back, when the mode the worker says changed."""
    viewer = channel.viewer
    if viewer is None or viewer.ended.is_set():
        return
    if channel.mode == frames.HUMAN and not viewer.controlling:
        viewer.controlling, viewer.controlled = True, 0
        await _record(viewer, audit.BROWSER_CONTROL, f"{viewer.target} start")
    elif channel.mode == frames.AGENT and viewer.controlling:
        viewer.controlling = False
        await _record(viewer, audit.BROWSER_CONTROL, f"{viewer.target} end=agent inputs={viewer.controlled}")


async def end_view(views: BrowserViews, channel: Channel, viewer: Viewer, reason: str, how: str, *, code=1000) -> None:
    """End a view once: tell the worker nobody watches, audit it and close the viewer's socket. A later call waits
    until the first is done."""
    if viewer.ended.is_set():
        await viewer.closed.wait()
        return
    viewer.ended.set()
    viewer.how = how
    with anyio.CancelScope(shield=True):  # recorded even when the viewer's task is being cancelled
        try:
            if channel.viewer is viewer:
                channel.viewer = None
                if channel.worker is not None:
                    await channel.worker.send(channel.view())
            if viewer.controlling:
                viewer.controlling = False
                await _record(viewer, audit.BROWSER_CONTROL, f"{viewer.target} end={how} inputs={viewer.controlled}")
            ended = f"{viewer.target} end={how} frames={viewer.frames} inputs={viewer.inputs}"
            await _record(viewer, audit.BROWSER_VIEW, ended)
            await viewer.close(code, reason)
        finally:
            viewer.closed.set()
            views.forget(channel)
    log.info(
        "browser view closed",
        extra={"run_id": channel.run_id, "how": how, "frames": viewer.frames, "inputs": viewer.inputs},
    )


# The viewer's end


async def _hello(websocket: WebSocket) -> dict | None:
    """The viewer's first message as a dict, or None when it left first."""
    try:
        message = await asyncio.wait_for(websocket.receive(), models.HELLO_SECONDS)
    except TimeoutError:
        raise Refusal(codes.CLOSE_TIMEOUT, f"no hello within {models.HELLO_SECONDS:g} seconds") from None
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
        raise Refusal(codes.CLOSE_UNSUPPORTED, 'the first message is the hello: {"csrf": ...}')
    return hello


async def _check_view(conn: AsyncConnection, websocket: WebSocket, user: Principal, project: str, run_id: int):
    """The viewer of run ``run_id``, audited, when ``user`` may watch its browser; Refusal otherwise."""
    if not await repository.fresh(conn, user.token_id, SESSION_MAX_AGE):
        hours = int(SESSION_MAX_AGE.total_seconds() // 3600)
        raise Refusal(codes.CLOSE_FORBIDDEN, f"the web session is older than {hours} hours: sign in again")
    try:
        access = await readable_run(conn, user, project, run_id, None)
    except HTTPException as exc:
        raise Refusal(codes.CLOSE_FORBIDDEN, str(exc.detail)) from None
    run = await repository.viewed_run(conn, run_id, access.project_id)
    if run.dispatched_by != user.user_id:
        raise Refusal(codes.CLOSE_FORBIDDEN, f"only {run.owner}, who dispatched run {run_id}, may view its browser")
    if not run.browser:
        raise Refusal(codes.CLOSE_FORBIDDEN, f"run {run_id} was dispatched without a browser")
    if run.state not in runs.HELD_STATES or run.worker_id is None:
        raise Refusal(codes.CLOSE_FORBIDDEN, f"run {run_id} is {run.state}: its browser lives while a worker holds it")
    login = run.state == "waiting" and await repository.login_open(conn, run_id)
    target = run_target(project, run.plan_id, run.step_key, run_id)
    await Auditor(conn, access.project_id, user).record(audit.BROWSER_VIEW, f"{target} start")
    return Viewer(websocket, user, project_id=access.project_id, target=target, login=login)


async def _open_viewer(websocket: WebSocket, project: str, run_id: int) -> Viewer | None:
    """Check the viewer's request; Refusal when it may not watch, None when it left before its hello."""
    app = websocket.app
    config = app.state.config
    if config.web_login_missing():
        raise Refusal(codes.CLOSE_FORBIDDEN, "the live view needs web sign-in, which this hub has not set up")
    if origin_of(websocket.headers.get("origin")) != origin_of(config.public_url):
        raise Refusal(codes.CLOSE_FORBIDDEN, "the live view opens only from a page of this hub (Origin)")
    token = websocket.cookies.get(SESSION_COOKIE)
    if not token:
        raise Refusal(codes.CLOSE_UNAUTHENTICATED, "sign in on the web to open the live view")
    hello = await _hello(websocket)
    if hello is None:
        return None
    given = hello.get("csrf")
    if not isinstance(given, str) or not same(given, csrf_token(config.session_secret, hash_token(token))):
        raise Refusal(codes.CLOSE_FORBIDDEN, "the hello needs the X-Evo-CSRF value of GET /v1/auth/web/csrf")
    try:
        user = await authenticate(app.state.engine, token, WEB, config)
        if user is None:
            raise Refusal(codes.CLOSE_UNAUTHENTICATED, "the web session is revoked, expired or unknown: sign in again")
        async with app.state.engine.begin() as conn:
            return await _check_view(conn, websocket, user, project, run_id)
    except models.DATABASE_ERRORS as exc:
        log.warning("cannot open a browser view: database unavailable", extra={"error": type(exc).__name__})
        raise Refusal(codes.CLOSE_UNAVAILABLE, "the hub database is unavailable; try again shortly") from None


async def _join(views: BrowserViews, channel: Channel, viewer: Viewer) -> None:
    """Make ``viewer`` the channel's, ending the one before; tell it whether the worker is there, and the worker that
    someone watches."""
    before, channel.viewer = channel.viewer, viewer
    if before is not None:
        await end_view(
            views, channel, before, "the live view opened in another window", "replaced", code=codes.CLOSE_BUSY
        )
    await viewer.send(channel.link())
    worker = channel.worker
    if worker is not None:
        if channel.state is not None:
            await viewer.send(channel.state)
        await worker.send(channel.view())
    await _control(channel)


async def _from_viewer(views: BrowserViews, channel: Channel, viewer: Viewer) -> None:
    """Relay the viewer's events and acks to the worker until the view ends."""
    websocket = viewer.websocket
    while not viewer.ended.is_set():
        try:
            message = await websocket.receive()
        except RuntimeError:  # the viewer is gone already
            message = {"type": "websocket.disconnect"}
        if message["type"] == "websocket.disconnect":
            await end_view(views, channel, viewer, "the viewer left", "viewer")
            return
        data = message.get("bytes")
        if data is None:
            await end_view(
                views, channel, viewer, "frames are binary after the hello", "protocol", code=codes.CLOSE_UNSUPPORTED
            )
            return
        try:
            kind, payload = frames.parse(data)  # pyright: ignore[reportArgumentType]
            if kind not in frames.FROM_VIEWER:
                raise codes.FrameError("a viewer sends input (0) and ack (3) frames")
            if kind == frames.INPUT:
                frames.parse_event(payload)
            else:
                frames.body_of(payload)
        except codes.FrameError as exc:
            code = codes.CLOSE_TOO_BIG if len(data) > frames.MAX_FRAME_BYTES else codes.CLOSE_UNSUPPORTED
            await end_view(views, channel, viewer, str(exc), "protocol", code=code)
            return
        viewer.touch()
        worker = channel.worker
        if worker is None or channel.viewer is not viewer:
            continue  # nobody to take it: an event the worker was not there for is lost
        if await worker.send(data) and kind == frames.INPUT:  # pyright: ignore[reportArgumentType]
            viewer.inputs += 1
            if viewer.controlling:
                viewer.controlled += 1


async def _watch(views: BrowserViews, channel: Channel, viewer: Viewer) -> None:
    """End the view once it was idle for IDLE_SECONDS, or open for MAX_SECONDS."""
    loop = asyncio.get_running_loop()
    while not viewer.ended.is_set():
        now = loop.time()
        idle_left = viewer.active + models.IDLE_SECONDS - now
        open_left = viewer.opened + models.MAX_SECONDS - now
        if open_left <= 0:
            reason = f"a live view stays open at most {models.MAX_SECONDS / 3600:g} hours"
            await end_view(views, channel, viewer, reason, "timeout", code=codes.CLOSE_TIMEOUT)
            return
        if idle_left <= 0:
            reason = f"closed after {models.IDLE_SECONDS / 60:g} idle minutes"
            await end_view(views, channel, viewer, reason, "idle", code=codes.CLOSE_TIMEOUT)
            return
        try:
            await asyncio.wait_for(viewer.ended.wait(), min(idle_left, open_left))
        except TimeoutError:
            pass


async def serve_viewer(websocket: WebSocket, project: str, run_id: int) -> None:
    """Check the viewer's end of run ``run_id``'s browser, then relay it until the view ends."""
    await websocket.accept()
    try:
        viewer = await _open_viewer(websocket, project, run_id)
    except Refusal as refusal:
        log.info("browser view refused", extra={"run_id": run_id, "end": "viewer", "code": refusal.code})
        await close(websocket, refusal.code, refusal.reason)
        return
    if viewer is None:  # the viewer left before its hello
        return
    views: BrowserViews = websocket.app.state.browser_views
    channel = views.channel(run_id)
    log.info("browser view opened", extra={"run_id": run_id, "login": viewer.user.login})
    watcher = asyncio.create_task(_watch(views, channel, viewer))
    try:
        await _join(views, channel, viewer)
        await _from_viewer(views, channel, viewer)
    finally:
        watcher.cancel()
        await end_view(views, channel, viewer, "the viewer left", "viewer")


# The worker's end


async def _check_worker(websocket: WebSocket, run_id: int) -> int:
    """The id of the worker whose request this is, when it holds run ``run_id`` and the run has a browser."""
    given = websocket.headers.get(PROTOCOL_HEADER)
    if given is None or given.strip() != PROTOCOL_VERSION:
        raise Refusal(
            codes.CLOSE_UPGRADE,
            f"this hub speaks version {PROTOCOL_VERSION} of the worker protocol: upgrade evo-agents on the worker",
        )
    token = bearer(websocket)
    app = websocket.app
    try:
        user = await authenticate(app.state.engine, token, WORKER, app.state.config)
        if user is None:
            raise Refusal(codes.CLOSE_UNAUTHENTICATED, f"the worker token is revoked, expired or unknown: {JOIN_HINT}")
        async with app.state.engine.begin() as conn:
            worker, held = await repository.worker_and_run(conn, user.token_id, run_id)
    except models.DATABASE_ERRORS as exc:
        log.warning("cannot connect a browser: database unavailable", extra={"error": type(exc).__name__})
        raise Refusal(codes.CLOSE_UNAVAILABLE, "the hub database is unavailable; try again shortly") from None
    if worker is None or worker.revoked_at is not None:
        raise Refusal(codes.CLOSE_FORBIDDEN, "this worker token belongs to no live worker: join the machine again")
    if held is None or held.worker_id != worker.id or held.state not in runs.HELD_STATES:
        raise Refusal(codes.CLOSE_FORBIDDEN, NOT_HELD.format(id=run_id))
    if not held.browser:
        raise Refusal(codes.CLOSE_FORBIDDEN, f"run {run_id} was dispatched without a browser")
    return worker.id


async def _attach(views: BrowserViews, channel: Channel, end: End, worker_id: int) -> None:
    """Make ``end`` the channel's worker's end, closing the one before; tell a viewer it is there."""
    before = channel.worker
    channel.worker, channel.worker_id, channel.state, channel.mode = end, worker_id, None, frames.AGENT
    if before is not None:
        await before.close(codes.CLOSE_BUSY, "a newer connection of the worker took the run's browser")
    if channel.viewer is not None:
        await channel.viewer.send(channel.link())
        await end.send(channel.view())


async def _from_worker(views: BrowserViews, channel: Channel, end: End) -> int:
    """Relay the worker's screen frames and states to the viewer until this end leaves; the code it closed with."""
    while channel.worker is end:
        try:
            message = await end.websocket.receive()
        except RuntimeError:
            message = {"type": "websocket.disconnect", "code": 1006}
        if message["type"] == "websocket.disconnect":
            return message.get("code") or 1005
        data = message.get("bytes")
        code = None
        try:
            if data is None:
                code = codes.CLOSE_UNSUPPORTED
                raise codes.FrameError("the worker's frames are binary")
            kind, payload = frames.parse(data)
            if kind not in frames.FROM_WORKER:
                raise codes.FrameError("a worker sends screen (1) and state (2) frames")
            mode = frames.mode_of(frames.body_of(payload)) if kind == frames.STATE else None
        except codes.FrameError as exc:
            if code is None:
                code = codes.CLOSE_TOO_BIG if len(data) > frames.MAX_FRAME_BYTES else codes.CLOSE_UNSUPPORTED  # pyright: ignore[reportArgumentType]
            await end.close(code, str(exc))
            return code
        viewer = channel.viewer
        if kind == frames.STATE:
            channel.state, channel.mode = data, mode or frames.AGENT
        if viewer is not None and await viewer.send(data):
            viewer.touch()
            viewer.frames += kind == frames.SCREEN
        if kind == frames.STATE:
            await _control(channel)
    return codes.CLOSE_BUSY


async def _detach(views: BrowserViews, channel: Channel, end: End, code: int) -> None:
    """The worker's end left: a viewer waits for the next, unless the worker closed the browser (CLOSE_NORMAL)."""
    if channel.worker is end:
        channel.worker, channel.state, channel.mode = None, None, frames.AGENT
        viewer = channel.viewer
        if viewer is not None and code == codes.CLOSE_NORMAL:
            await end_view(views, channel, viewer, "the run's browser closed", "worker")
        elif viewer is not None:
            await viewer.send(channel.link())
    views.forget(channel)


async def serve_worker(websocket: WebSocket, run_id: int) -> None:
    """Check the worker's end of run ``run_id``'s browser, then relay it until it leaves."""
    await websocket.accept()
    try:
        worker_id = await _check_worker(websocket, run_id)
    except Refusal as refusal:
        log.info("browser view refused", extra={"run_id": run_id, "end": "worker", "code": refusal.code})
        await close(websocket, refusal.code, refusal.reason)
        return
    views: BrowserViews = websocket.app.state.browser_views
    channel = views.channel(run_id)
    end = End(websocket)
    log.info("browser attached", extra={"run_id": run_id, "worker_id": worker_id})
    code = 1006
    try:
        await _attach(views, channel, end, worker_id)
        code = await _from_worker(views, channel, end)
    finally:
        with anyio.CancelScope(shield=True):
            await _detach(views, channel, end, code)
            await end.close(codes.CLOSE_NORMAL, "the browser channel ended")
        log.info("browser detached", extra={"run_id": run_id, "worker_id": worker_id, "code": code})
