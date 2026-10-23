"""The browser of a run on the hub's side (plan agent-browser, criterion a3): which worker may take a run dispatched
with browser, and what a rerun asks.

A worker whose heartbeat lists the feature browser (``runs.BROWSER``) reports its Chromium in workers.browser
(``models.browser.BrowserReport``), max among it: how many browser runs it holds at once. The claim hands it a browser
run only while it holds fewer than that (``claim_gate``; the claim locks the worker's row, so two claims of one worker
never both take the last one), and never hands one to any other worker; a run without a browser claims as before. A
dispatch pinned to a worker that does not report the feature is refused (``unfit``) rather than queued for good.

A dispatch may name browser identities (criterion a4): ``resolve`` finds each, the caller's own of that name before
the project's, and the dispatch pins the run to the one worker that holds them all. The claim hands such a run only to
that worker, and only while none of its identities is held by another run (``claim_gate``); ``browser_holds`` gives
them to the run as it is claimed and takes them back as it leaves the held states. A rerun keeps them while they are
still live and the caller's to use (``rerun_browser``)."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, any_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_login, runs, tables
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.runs.models import Rerun
from evo_agents.hub.server.security import Principal

NO_BROWSER = (
    "its daemon reports no Chromium (the feature browser): run `evo-agents worker browser install` on it and restart "
    "its daemon, or dispatch the run without a browser"
)


def claim_gate(worker_id: int, features):
    """The condition a queued run meets for worker ``worker_id`` with ``features`` to claim it: no browser asked, or a
    worker of the feature browser holding fewer browser runs than the max its last heartbeat reported (1 without
    one)."""
    r = tables.runs
    if runs.BROWSER not in (features or ()):
        return r.c.browser.is_(False)
    held, w = tables.runs.alias("browser_held"), tables.workers.alias("browser_worker")
    holding = (
        select(func.count())
        .select_from(held)
        .where(held.c.worker_id == worker_id, held.c.browser, held.c.state.in_(runs.HELD_STATES))
        .scalar_subquery()
    )
    room = select(func.coalesce(w.c.browser["max"].as_integer(), 1)).where(w.c.id == worker_id).scalar_subquery()
    b = tables.browser_identities
    elsewhere = and_(b.c.worker_id != worker_id, r.c.kind != browser_login.KIND)  # a Move's session opens it anew
    taken = exists().where(  # an identity of the run another run holds, or one this worker does not hold
        b.c.id == any_(r.c.browser_identities),
        b.c.deleted_at.is_(None),
        or_(b.c.held_by_run.is_not(None), elsewhere),
    )
    return and_(or_(r.c.browser.is_(False), holding < room), ~taken)


def unfit(features, browser: bool) -> list[str]:
    """What keeps a worker with ``features`` from ever claiming a run that asks for a ``browser``; empty when nothing
    does."""
    return [NO_BROWSER] if browser and runs.BROWSER not in features else []


async def profile_default(conn: AsyncConnection, profile_id: int, pinned) -> tuple[list[int], int]:
    """The browser identity the agent profile ``profile_id`` hands every run under it, as ``([id], worker id)``, or
    ``([], 0)`` when it has none live and not disabled. 422 when the run is ``pinned`` to another worker than the one
    that holds it."""
    b = tables.browser_identities
    query = select(b.c.id, b.c.name, b.c.worker_id).where(
        b.c.profile_id == profile_id, b.c.profile_default, b.c.deleted_at.is_(None), b.c.status != "disabled"
    )
    found = (await conn.execute(query)).one_or_none()
    if found is None:
        return [], 0
    if pinned is not None and pinned.id != found.worker_id:
        raise HTTPException(
            422,
            f"the run is pinned to worker {pinned.name}, and the browser identity {found.name} of its agent opens only "
            "on the worker that holds it: pin it there or not at all; nothing was dispatched",
        )
    return [found.id], found.worker_id


def _mine_or_project(user: Principal, *, agents: bool = False):
    """The identities ``user`` may name: their own and the project's; with ``agents`` the agents' too, which a dispatch
    never names (a profile hands its own to its runs) but a rerun of such a run keeps."""
    b = tables.browser_identities
    kinds = ("project", "agent") if agents else ("project",)
    return or_(b.c.owner_kind.in_(kinds), and_(b.c.owner_kind == "member", b.c.owner_id == user.user_id))


async def resolve(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, names, pinned
) -> tuple[list[int], int]:
    """The ids of the browser identities ``names`` a dispatch by ``user`` in project ``access`` names, in its order,
    and the worker that holds them: each the caller's own of that name, else the project's. 422 for a name that is
    neither, an identity disabled, identities on more than one worker, or a dispatch ``pinned`` to another worker."""
    names = list(dict.fromkeys(names))
    if not names:
        return [], 0
    b = tables.browser_identities
    query = select(b.c.id, b.c.name, b.c.owner_kind, b.c.worker_id, b.c.status).where(
        b.c.project_id == access.project_id, b.c.name.in_(names), b.c.deleted_at.is_(None), _mine_or_project(user)
    )
    found: dict = {}
    for row in sorted((await conn.execute(query)).all(), key=lambda row: row.owner_kind != "member"):
        found.setdefault(row.name, row)  # the caller's own first
    missing = [name for name in names if name not in found]
    if missing:
        raise HTTPException(
            422,
            f"project {access.name} has no browser identity {', '.join(missing)} of yours or its own "
            "(`evo-agents hub browser identity list`); nothing was dispatched",
        )
    disabled = [name for name in names if found[name].status == "disabled"]
    if disabled:
        raise HTTPException(422, f"browser identity {', '.join(disabled)} is disabled; nothing was dispatched")
    workers = {found[name].worker_id for name in names}
    if len(workers) > 1:
        raise HTTPException(
            422,
            "the browser identities of one run are held by one worker, and these are on several; nothing was "
            "dispatched",
        )
    worker_id = workers.pop()
    if pinned is not None and pinned.id != worker_id:
        raise HTTPException(
            422,
            f"the run is pinned to worker {pinned.name}, and its browser identities open only on the worker that "
            "holds them: pin it there or not at all; nothing was dispatched",
        )
    return [found[name].id for name in names], worker_id


async def rerun_browser(conn: AsyncConnection, user: Principal, run_id: int, body: Rerun | None, worker):
    """Whether the rerun of run ``run_id`` by ``user`` gets a browser, as ``body`` asks, else as the run had one, and
    the ids of the browser identities it keeps: the run's, unless it gets no browser. 409 when one of them is deleted,
    disabled or no longer ``user``'s to use, or the rerun goes to another ``worker`` than theirs."""
    r = tables.runs
    browser, ids = (await conn.execute(select(r.c.browser, r.c.browser_identities).where(r.c.id == run_id))).one()
    if body is not None and body.browser is not None:
        browser = body.browser
    ids = list(ids or ()) if browser else []
    if ids:
        b = tables.browser_identities
        usable = select(b.c.worker_id).where(
            b.c.id.in_(ids),
            b.c.deleted_at.is_(None),
            b.c.status != "disabled",
            _mine_or_project(user, agents=True),
        )
        workers = (await conn.execute(usable)).scalars().all()
        if len(workers) != len(set(ids)) or worker is None or {worker.id} != set(workers):
            raise HTTPException(
                409,
                f"run {run_id} opened browser identities that are deleted, disabled, not yours to use, or on another "
                "worker than the rerun's: rerun it with browser false, or dispatch the step again with identities",
            )
    return bool(browser), ids
