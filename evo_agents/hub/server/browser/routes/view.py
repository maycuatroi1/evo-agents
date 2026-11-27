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

from fastapi import APIRouter, WebSocket

from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.browser.service import view as service
from evo_agents.hub.server.runs.models import RunId

router = APIRouter(prefix="/v1/projects", tags=["runs"])
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"])


@router.websocket("/{project}/runs/{run_id}/browser")
async def browser_view(websocket: WebSocket, project: ProjectName, run_id: RunId) -> None:
    """The viewer's end of the run's browser (the member who dispatched the run, on any worker)."""
    await service.serve_viewer(websocket, project, run_id)


@worker_router.websocket("/runs/{run_id}/browser")
async def worker_browser(websocket: WebSocket, run_id: RunId) -> None:
    """The worker's end of the run's browser, connected while the daemon holds the run's Chromium."""
    await service.serve_worker(websocket, run_id)
