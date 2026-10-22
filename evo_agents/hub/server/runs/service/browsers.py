"""The browser of a run on the hub's side (plan agent-browser, criterion a3): which worker may take a run dispatched
with browser, and what a rerun asks.

A worker whose heartbeat lists the feature browser (``runs.BROWSER``) reports its Chromium in workers.browser
(``models.browser.BrowserReport``), max among it: how many browser runs it holds at once. The claim hands it a browser
run only while it holds fewer than that (``claim_gate``; the claim locks the worker's row, so two claims of one worker
never both take the last one), and never hands one to any other worker; a run without a browser claims as before. A
dispatch pinned to a worker that does not report the feature is refused (``unfit``) rather than queued for good."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.runs.models import Rerun

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
    return or_(r.c.browser.is_(False), holding < room)


def unfit(features, browser: bool) -> list[str]:
    """What keeps a worker with ``features`` from ever claiming a run that asks for a ``browser``; empty when nothing
    does."""
    return [NO_BROWSER] if browser and runs.BROWSER not in features else []


async def rerun_browser(conn: AsyncConnection, run_id: int, body: Rerun | None) -> bool:
    """Whether the rerun of run ``run_id`` gets a browser: as ``body`` asks, else as the run had one."""
    if body is not None and body.browser is not None:
        return body.browser
    r = tables.runs
    return bool((await conn.execute(select(r.c.browser).where(r.c.id == run_id))).scalar_one())
