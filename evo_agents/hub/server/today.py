"""Today, what waits for the caller and what changed since they last looked (plan hub-mobile-phase-0, criterion a7):
GET /v1/me/today and POST /v1/me/today/seen, with a web session or a machine token. ``docs/notifications.md`` is the
design; the name keeps clear of the Curator's morning brief (``brief``).

GET /v1/me/today is worked out at each call, over every project the caller holds a grant on, through the checks of
GET /v1/me/overview (``overview.scope``): a decision, run or plan of a plan whose label the caller's grant does not
reach is neither listed nor counted. It answers:

- ``needs_you``: the open decisions asked of the caller (``decision_reads.asked_person``), the oldest first, at most
  MAX_NEEDS_YOU, each with when its run parks for want of an answer (``overview.parks_at``); ``needs_you_count``
  counts them all;
- ``items``: at most MAX_ITEMS things that changed in the window, the first ones in the order they happened, each with
  its kind (ITEM_KINDS), a title, a body, its moment, its project and a link:

  - ``release``: a release cycle of a project that was published (``releases.service.watch``);
  - ``step_done``: a step of a visible plan that is done now and was not in the revision the hub held at the start of
    the window (for a plan first written in the window, in its first revision), at the first revision since that has
    it done; a step's ``done_at`` is a date, too coarse for a mark that moves within a day;
  - ``run_failed`` and ``run_parked``: a run the caller dispatched that failed or was lost, or parked for want of an
    answer; the night shift's runs (``runs.CURATOR_KINDS``) are left to the Curator's morning brief;
  - ``problem``: an open problem the caller owns whose metric, measured as the Loop measures a goal's
    (``loop.view.measure``), differs from METRIC_DAYS ago, at the newest measurement of its key in the window;
  - ``cost``: what the runs the caller dispatched that ended in the window cost, one item a project, each agent
    session once at its largest total (``night_cost.usage_cost``);
  - ``proposal``: an open proposal of the Curator recorded in the window, in a project the caller is an admin of, and
    so may answer, whose label their grant reaches;
  - ``report``: the notices that only report (``runs.REPORT_NOTICE_KINDS``: a push or merge into a default branch, a
    plan finished, a run's files) the caller got in the window in a project they hold a grant on, one item a project
    and kind of notice, at the newest of them; they are stored read and reach no channel, so Today is where they count;

- ``caught_up``: whether ``items`` holds everything that changed in the window; ``seen_until``, the mark to post once
  every item listed was shown: the moment of the answer when it is caught up, else the moment of the last item;
- ``summary``: one sentence made from English templates (``summary``), no model.

The window starts at the caller's seen mark (``today_marks.seen_at``). Without a mark it is the last DEFAULT_WINDOW,
and a mark older than MAX_WINDOW counts from MAX_WINDOW ago. POST /v1/me/today/seen moves the mark forward to its
``seen_at`` (now when left out, never past now): a mark already later stays where it is. ``today_digest`` sends the
summary push of the same answer.

The routes pass the engine to ``runs.service.today``, which works Today out in one ``UnitOfWork``; the queries are in
``runs.repository.today`` and the models in ``runs.models.today``.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models.today import ITEM_KINDS as ITEM_KINDS
from evo_agents.hub.server.runs.models.today import MAX_ITEMS as MAX_ITEMS
from evo_agents.hub.server.runs.models.today import MAX_NEEDS_YOU as MAX_NEEDS_YOU
from evo_agents.hub.server.runs.models.today import Today, TodaySeen, TodaySeenIn
from evo_agents.hub.server.runs.models.today import TodayDecision as TodayDecision
from evo_agents.hub.server.runs.models.today import TodayItem as TodayItem
from evo_agents.hub.server.runs.service import today as service
from evo_agents.hub.server.runs.service.today import compute, summary  # noqa: F401 (digests.service.today, tests)
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/me", tags=["today"], responses={401: {"model": ErrorBody}})


@router.get("/today", response_model=Today)
async def today(request: Request, user: CurrentUser) -> Today:
    """What waits for you, and what changed since you last looked, over the projects you hold a grant on."""
    wait = timedelta(seconds=request.app.state.config.decision_wait_seconds)
    return await service.today(request.app.state.engine, user, wait)


@router.post("/today/seen", response_model=TodaySeen, responses={422: {"model": ErrorBody}})
async def today_seen(request: Request, user: CurrentUser, body: TodaySeenIn | None = None) -> TodaySeen:
    """Move your seen mark forward to what you saw: Today counts what changes after it."""
    moved = await service.today_seen(request.app.state.engine, user.user_id, body.seen_at if body is not None else None)
    return TodaySeen(seen_at=moved)
