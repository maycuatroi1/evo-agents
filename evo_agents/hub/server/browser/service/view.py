"""The live view of a run's browser (plan agent-browser, criterion a5): two websockets the api joins in its own memory,
the viewer's and the worker's, as it joins the two ends of a web terminal (``evo_agents.hub.server.terminal``).
``evo_agents.hub.browser_view`` is the wire format; ``docs/workers.md`` the protocol.

``Authenticate`` lets these two through (``security.SELF_CHECKED_WEBSOCKETS``) and each checks its own credential,
after accepting the socket, so the client sees the close code.

WS /v1/projects/{p}/runs/{id}/browser is the viewer's end. It checks what the web terminal's browser end checks, in
the same order: an Origin equal to EVO_HUB_PUBLIC_URL's (CLOSE_FORBIDDEN), the session cookie (CLOSE_UNAUTHENTICATED),
the hello within HELLO_SECONDS (CLOSE_TIMEOUT) with the session's X-Evo-CSRF value (CLOSE_FORBIDDEN), a live web
session (CLOSE_UNAUTHENTICATED) created within SESSION_MAX_AGE, and a run the caller may read; then that the caller
dispatched the run, on whatever worker it runs: any other member, the owner of the worker that holds it included, is
closed with CLOSE_FORBIDDEN, as is a run dispatched without a browser or that no worker holds (HELD_STATES). One
viewer a run: a newer one takes the view, and the one before gets CLOSE_BUSY. The view asks no takeover: Take control
is POST .../takeover and Hand back POST .../handback, and the worker's broker says the mode they make in its STATE.

WS /v1/worker/runs/{id}/browser is the worker's end, which the daemon opens as soon as it started the run's browser
and holds until it closes it: the worker protocol header (CLOSE_UPGRADE), the worker's evw_ token as ``Authorization:
Bearer`` (CLOSE_UNAUTHENTICATED, CLOSE_FORBIDDEN for another kind), and the live worker of that token holding the
run, which has a browser (CLOSE_FORBIDDEN). A newer connection of the worker takes the run's channel, and the one
before gets CLOSE_BUSY.

The worker gets VIEW when a viewer comes or goes, so it screencasts only while someone watches, with ``login`` true
for a run that waits on an open decision of LOGIN_CATEGORY, which the broker then opens in human mode. The viewer gets
LINK when the worker's end comes or goes, and the worker's last STATE as it joins. INPUT and ACK pass to the worker,
SCREEN and STATE to the viewer, while both are there; nothing is kept but the last STATE, in memory. A text message
after the hello, an empty frame or a type the end may not send closes that end with CLOSE_UNSUPPORTED, a frame over
MAX_FRAME_BYTES with CLOSE_TOO_BIG. A worker's end that closes with CLOSE_NORMAL closed the browser, and the viewer
gets CLOSE_NORMAL; one that drops leaves the viewer waiting for the next. A view ends after IDLE_SECONDS without a
frame either way, and after MAX_SECONDS in all (CLOSE_TIMEOUT).

The audit has browser.view when a view opens ("<run> start") and when it ends ("<run> end=<how> frames=<n>
inputs=<n>"), and browser.control when the viewer gets the browser to drive, as the worker's STATE says mode human
("<run> start"), and when it no longer has it ("<run> end=<agent|how the view ended> inputs=<n>"), in the viewer's
name: never a key, a text, a URL or a frame.

Everything lives in this process (``BrowserViews``), as the hub runs one api process.
"""

from __future__ import annotations

import asyncio
import json
import logging

import anyio
import psycopg
from fastapi import APIRouter, HTTPException, WebSocket
from sqlalchemy import exc as sa_exc
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_view as frames
from evo_agents.hub import runs, tables
from evo_agents.hub import terminal as codes
from evo_agents.hub.runs import PROTOCOL_HEADER, PROTOCOL_VERSION
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.runs.models import NOT_HELD, RunId
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
    fresh_session,
    holding_worker,
    origin_of,
)

log = logging.getLogger(__name__)

HELLO_SECONDS = 10.0  # the viewer's first message, its CSRF value, comes within this
IDLE_SECONDS = 15 * 60  # a view without a frame either way for this long is closed
MAX_SECONDS = 4 * 60 * 60  # a view is closed this long after it opened, busy or not
LOGIN_CATEGORY = "login"  # a run waiting on an open decision of this category opens in human mode
DATABASE_ERRORS = (psycopg.OperationalError, sa_exc.OperationalError, OSError)  # PoolTimeout is an OperationalError

router = APIRouter(prefix="/v1/projects", tags=["runs"])
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"])


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
            await audit.record(
                conn,
                actor_id=viewer.user.user_id,
                token_id=viewer.user.token_id,
                action=action,
                target=target,
                project_id=viewer.project_id,
            )
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
        message = await asyncio.wait_for(websocket.receive(), HELLO_SECONDS)
    except TimeoutError:
        raise Refusal(codes.CLOSE_TIMEOUT, f"no hello within {HELLO_SECONDS:g} seconds") from None
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


def _viewed_run(run_id: int, project_id: int):
    """Run ``run_id`` of the project, with the login of who dispatched it."""
    runs_, users = tables.runs, tables.users
    return (
        select(
            runs_.c.dispatched_by,
            users.c.login.label("owner"),
            runs_.c.state,
            runs_.c.browser,
            runs_.c.worker_id,
            runs_.c.plan_id,
            runs_.c.step_key,
        )
        .select_from(runs_.join(users, users.c.id == runs_.c.dispatched_by))
        .where(runs_.c.id == run_id, runs_.c.project_id == project_id)
    )


def _login_open(run_id: int):
    """Whether run ``run_id`` has an open decision of LOGIN_CATEGORY."""
    decisions = tables.decisions
    return select(
        exists().where(
            decisions.c.run_id == run_id, decisions.c.state == "open", decisions.c.category == LOGIN_CATEGORY
        )
    )


async def _check_view(conn: AsyncConnection, websocket: WebSocket, user: Principal, project: str, run_id: int):
    """The viewer of run ``run_id``, audited, when ``user`` may watch its browser; Refusal otherwise."""
    if not (await conn.execute(fresh_session(user.token_id))).scalar_one_or_none():
        hours = int(SESSION_MAX_AGE.total_seconds() // 3600)
        raise Refusal(codes.CLOSE_FORBIDDEN, f"the web session is older than {hours} hours: sign in again")
    try:
        access = await readable_run(conn, user, project, run_id, None)
    except HTTPException as exc:
        raise Refusal(codes.CLOSE_FORBIDDEN, str(exc.detail)) from None
    run = (await conn.execute(_viewed_run(run_id, access.project_id))).one()
    if run.dispatched_by != user.user_id:
        raise Refusal(codes.CLOSE_FORBIDDEN, f"only {run.owner}, who dispatched run {run_id}, may view its browser")
    if not run.browser:
        raise Refusal(codes.CLOSE_FORBIDDEN, f"run {run_id} was dispatched without a browser")
    if run.state not in runs.HELD_STATES or run.worker_id is None:
        raise Refusal(codes.CLOSE_FORBIDDEN, f"run {run_id} is {run.state}: its browser lives while a worker holds it")
    login = run.state == "waiting" and bool((await conn.execute(_login_open(run_id))).scalar())
    target = run_target(project, run.plan_id, run.step_key, run_id)
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.BROWSER_VIEW,
        target=f"{target} start",
        project_id=access.project_id,
    )
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
    except DATABASE_ERRORS as exc:
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
            kind, payload = frames.parse(data)
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
        if await worker.send(data) and kind == frames.INPUT:
            viewer.inputs += 1
            if viewer.controlling:
                viewer.controlled += 1


async def _watch(views: BrowserViews, channel: Channel, viewer: Viewer) -> None:
    """End the view once it was idle for IDLE_SECONDS, or open for MAX_SECONDS."""
    loop = asyncio.get_running_loop()
    while not viewer.ended.is_set():
        now = loop.time()
        idle_left = viewer.active + IDLE_SECONDS - now
        open_left = viewer.opened + MAX_SECONDS - now
        if open_left <= 0:
            reason = f"a live view stays open at most {MAX_SECONDS / 3600:g} hours"
            await end_view(views, channel, viewer, reason, "timeout", code=codes.CLOSE_TIMEOUT)
            return
        if idle_left <= 0:
            reason = f"closed after {IDLE_SECONDS / 60:g} idle minutes"
            await end_view(views, channel, viewer, reason, "idle", code=codes.CLOSE_TIMEOUT)
            return
        try:
            await asyncio.wait_for(viewer.ended.wait(), min(idle_left, open_left))
        except TimeoutError:
            pass


@router.websocket("/{project}/runs/{run_id}/browser")
async def browser_view(websocket: WebSocket, project: ProjectName, run_id: RunId) -> None:
    """The viewer's end of the run's browser (the member who dispatched the run, on any worker)."""
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


def _browser_run(run_id: int):
    """The worker that holds run ``run_id``, its state and whether it has a browser."""
    runs_ = tables.runs
    return select(runs_.c.worker_id, runs_.c.state, runs_.c.browser).where(runs_.c.id == run_id)


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
            worker = (await conn.execute(holding_worker(user.token_id))).one_or_none()
            held = (await conn.execute(_browser_run(run_id))).one_or_none()
    except DATABASE_ERRORS as exc:
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
                code = codes.CLOSE_TOO_BIG if len(data) > frames.MAX_FRAME_BYTES else codes.CLOSE_UNSUPPORTED
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


@worker_router.websocket("/runs/{run_id}/browser")
async def worker_browser(websocket: WebSocket, run_id: RunId) -> None:
    """The worker's end of the run's browser, connected while the daemon holds the run's Chromium."""
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
