"""Workers: the machines members register to run plan steps for them, how a machine joins, and how it is stopped.

A machine joins in one of two ways. A member creates a pairing (POST /v1/workers/pairings, with a web session and its
CSRF header or with a machine token) naming the worker, its projects, slots, labels and whether it allows the web
terminal; every project must be one the member holds the writer role on. The answer carries a code of CODE_LENGTH
characters of Crockford base32, written XXXX-XXXX, shown once: the hub keeps an HMAC-SHA256 of the code under the
session secret (EVO_HUB_SESSION_SECRET; without it pairing and joining answer 503) and its first SELECTOR_LENGTH
characters in clear, the selector, which no two unused pairings share. A code lasts PAIRING_TTL, and a member has at
most MAX_LIVE_PAIRINGS codes that are neither used nor expired; a locked code counts until it expires, so locking codes
on purpose cannot take more selectors. The web follows a pairing with GET /v1/workers/pairings/{id}. On the machine,
the daemon sends the code and its host facts to POST /v1/worker/join (``worker_join``), the one public worker route,
which takes a pool's join token instead of a code too (``worker_pools``): the hub finds the
pairing by the selector and compares the HMAC of the whole code, so a wrong rest counts against that pairing, and
MAX_WRONG_TRIES wrong tries lock it for good. Unknown, wrong, expired, used and locked codes all get the same 403,
which names none of them, and an address with JOIN_REFUSALS of them within JOIN_WINDOW gets 429 with Retry-After
until the oldest leaves the window (``RefusalLimit`` in this process's memory, or on Redis when the cache is shared:
``cache.Window``; behind a reverse proxy the address is the one it forwards only for a proxy listed in
EVO_HUB_FORWARDED_ALLOW_IPS). A code that matches makes the worker and
its ``evw_`` token, which the answer carries once; the pairing is used from then on. A machine signed in with
``evo-agents hub login`` registers directly instead, with POST /v1/workers and its machine token.

A worker belongs to the member who registered it. Its owner may share it (``shared``, ``docs/workers.md``, Shared
workers): it then takes the runs of every member who holds writer on a project it serves too, and those members see it,
with only the projects they hold a grant on and without kg_sync, but change nothing. GET /v1/workers lists the caller's
workers and those shared with them, every worker for a hub admin; GET /v1/workers/{id} shows one; drain stops new
claims, undrain resumes them, and revoke ends the worker and its token at once. Each of them but GET answers 404 for
another member's worker, shared or not, so an id tells nothing about workers one does not own; a hub admin may see,
drain and revoke any worker, but only its owner undrains one (403 for an admin). Revoking releases the runs the worker
holds as the reaper would (``run_reaper.release_runs``): a run whose cancel was asked for is cancelled, a run pinned to
this worker or on its last attempt fails, and any other becomes lost with a new attempt queued for the same step; the
steps of the runs that ended go back to pending in their plans. Every lease of the worker's runs is given back with
them, and their GitHub tokens are revoked once the revocation commits (``credentials``). Revoking a worker's token
(DELETE /v1/tokens/{id}, DELETE /v1/admin/tokens/{id}) revokes its worker the same way, in the same transaction
(``lock_worker_of_token``, then ``end_worker``).

POST /v1/workers/{id}/dispatch-from {"value": "any" | "web"} sets who may hand the worker its runs (``dispatch_from``,
``evo_agents.hub.credentials.DISPATCH_FROM``): with ``web`` it claims only runs dispatched from a web session, and a
dispatch pinned to it with a token gets 403 (``runs``). Only its owner sets it, signed in on the web: a machine token
gets 403 whoever holds it, so a token that leaked cannot open the worker again, a hub admin who does not own the
worker gets 403, and anyone else 404. Setting the value it has changes nothing and is not audited.

PATCH /v1/workers/{id} changes the worker's slots (1 to ``runs.MAX_SLOTS``), labels (as a pairing takes them), name
(unique among the owner's workers that are not revoked, 409 otherwise), plugins (the ids of at most
``runs.MAX_WORKER_PLUGINS`` plugins whose runtimes and environments it takes, ``docs/plugins.md``), version_locked
(true: its daemon stays on the evo-agents it runs; false, every worker's default: it updates itself to the tag nightly
once idle), shared (true: it takes the runs of the writers of its projects too; false, every worker's default) or
projects, the whole list of projects it serves (``worker_projects``: the writer role for one added, none dropped that
a run or a night shift still needs it for), each field optional but at least one given. Only its owner changes them,
from a web session with its CSRF header or with their machine token: a hub admin who does not own the worker gets 403, anyone else 404, and a revoked
worker 409. Giving the values it has changes nothing and is not audited. The daemon learns the new settings from the
answer to its next heartbeat (``settings``), and the hub's claim counts the runs a worker holds against its slots as
they are now. A worker shows what its last heartbeat said of its daemon's install: agent_commit, the commit of
evo-agents it was built from, and self_update, what the daemon found of updating itself (``runs.SELF_UPDATE_STATES``),
with since, when the hub first got that state; kg_sync, what a daemon on duty for night shifts last did for their
projects' graphs, the error of each sync that failed included (``evo_agents.worker.kg_sync``); update_now, its last
Update now (``update_now``); browser_install, its last Install browser (``browser_install``); and nightly, where the
tag nightly pointed when the hub last read it.

POST /v1/workers/{id}/update-now asks the worker's daemon to update itself to nightly now, and POST
/v1/workers/{id}/update-now/cancel withdraws that, with the rights and refusals of drain (``update_now``). POST
/v1/workers/{id}/browser-install asks the daemon to install Chromium, and POST .../browser-install/cancel withdraws
that, with the same rights (``browser_install``).

Every pairing, join, registration, drain, undrain, Update now, Install browser and their cancels, change of dispatch_from or of settings
and revocation adds an audit row naming the pairing or worker, never the code or the token, and neither ever reaches
a log line.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response

from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.paging import PAGED, WHOLE, Paging
from evo_agents.hub.server.security import MACHINE, WEB, CurrentUser
from evo_agents.hub.server.workers.models.workers import (
    DISPATCH_FROM_SESSION,
    MAX_ID,
    DispatchFrom,
    Pairing,
    PairingRequest,
    PairingState,
    Worker,
    WorkerCredential,
    WorkerRegistration,
    WorkerUpdate,
)
from evo_agents.hub.server.workers.service import pairings, workers
from evo_agents.hub.server.workers.service.codes import pairing_secret

router = APIRouter(prefix="/v1/workers", tags=["workers"], responses={401: {"model": ErrorBody}})

WorkerId = Annotated[int, Path(ge=1, le=MAX_ID)]
REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": ErrorBody},
}


# Pairings


@router.post(
    "/pairings",
    status_code=201,
    response_model=Pairing,
    responses={**REFUSALS, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def create_pairing(request: Request, body: PairingRequest, user: CurrentUser, response: Response) -> Pairing:
    """A code a machine joins with as a worker of the caller; shown once."""
    secret = pairing_secret(request)
    pairing = await pairings.create(request.app.state.engine, user, body, secret)
    response.headers.update(NO_STORE)
    return pairing


@router.get("/pairings/{pairing_id}", response_model=PairingState, responses={404: {"model": ErrorBody}})
async def pairing_state(request: Request, pairing_id: WorkerId, user: CurrentUser, response: Response) -> PairingState:
    """Whether a machine has joined with one of the caller's pairings yet."""
    state = await pairings.state(request.app.state.engine, user, pairing_id)
    response.headers.update(NO_STORE)
    return state


# Registering with a machine token


@router.post("", status_code=201, response_model=WorkerCredential, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def register(
    request: Request, body: WorkerRegistration, user: CurrentUser, response: Response
) -> WorkerCredential:
    """Register the calling machine as a worker of the caller, with its machine token; the worker token is shown
    once."""
    if user.kind != MACHINE:
        raise HTTPException(
            403,
            "registering a worker directly takes the machine token of that machine (`evo-agents hub login` on it); "
            "on the web, create a pairing code instead",
        )
    credential = await workers.register(request.app.state.engine, user, body)
    response.headers.update(NO_STORE)
    return credential


# Listing and changing workers


@router.get("", response_model=list[Worker], responses=PAGED)
async def list_workers(
    request: Request,
    user: CurrentUser,
    revoked: Annotated[bool, Query(description="also list revoked workers")] = False,
    page: Paging = WHOLE,
) -> list[Worker]:
    """The caller's workers and the live ones other members share with the caller in a project both write to, newest
    first; every worker for a hub admin."""
    return await workers.listed(request.app.state.engine, user, revoked, page)


@router.get("/{worker_id}", response_model=Worker, responses={404: {"model": ErrorBody}})
async def show_worker(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """One worker of the caller's, or one another member shares with them; any worker for a hub admin."""
    return await workers.shown_worker(request.app.state.engine, user, worker_id)


@router.post("/{worker_id}/drain", response_model=Worker, responses=REFUSALS)
async def drain(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """Stop new claims; the worker finishes the runs it holds."""
    return await workers.set_drain(request.app.state.engine, user, worker_id, True)


@router.post("/{worker_id}/undrain", response_model=Worker, responses=REFUSALS)
async def undrain(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """Let a drained worker claim runs again; its owner only."""
    return await workers.set_drain(request.app.state.engine, user, worker_id, False)


@router.post("/{worker_id}/update-now", response_model=Worker, responses=REFUSALS)
async def ask_update_now(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """Ask the worker's daemon to update itself to nightly now: it claims no new run, lets the held ones end, updates
    and claims again. 409 with why when its version is locked, its daemon is too old, or it runs nightly already."""
    return await workers.set_update_now(request.app.state.engine, user, worker_id, True)


@router.post("/{worker_id}/update-now/cancel", response_model=Worker, responses=REFUSALS)
async def cancel_update_now(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """Withdraw the worker's open Update now; the daemon claims runs again."""
    return await workers.set_update_now(request.app.state.engine, user, worker_id, False)


@router.post("/{worker_id}/browser-install", response_model=Worker, responses=REFUSALS)
async def ask_browser_install(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """Ask the worker's daemon to install Chromium for the runs dispatched with a browser, as `evo-agents worker
    browser install` does on the machine; its runs and claims go on meanwhile. 409 with why when its daemon is too old
    or has Chromium already."""
    return await workers.set_browser_install(request.app.state.engine, user, worker_id, True)


@router.post("/{worker_id}/browser-install/cancel", response_model=Worker, responses=REFUSALS)
async def cancel_browser_install(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """Withdraw the worker's open Install browser; an install the daemon began goes on to its end."""
    return await workers.set_browser_install(request.app.state.engine, user, worker_id, False)


@router.post("/{worker_id}/dispatch-from", response_model=Worker, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def set_dispatch_from(request: Request, worker_id: WorkerId, body: DispatchFrom, user: CurrentUser) -> Worker:
    """Set who may hand the worker its runs: its owner, from a web session only."""
    if user.kind != WEB:
        raise HTTPException(403, DISPATCH_FROM_SESSION)
    return await workers.set_dispatch_from(request.app.state.engine, user, worker_id, body.value)


@router.patch("/{worker_id}", response_model=Worker, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def update_worker(request: Request, worker_id: WorkerId, body: WorkerUpdate, user: CurrentUser) -> Worker:
    """Change the worker's slots, labels, taints, name, plugins, projects, version lock or whether it is shared: its
    owner only, from a web session or with their machine token."""
    return await workers.update(request.app.state.engine, user, worker_id, body)


@router.post("/{worker_id}/revoke", response_model=Worker, responses=REFUSALS)
async def revoke(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """End the worker and its token at once, and release the runs it holds."""
    worker = await workers.revoke(request.app.state.engine, user, worker_id)
    await workers.revoke_leased_tokens(request.app.state, worker_id)
    return worker
