"""The caller's overview, for the web's Home: GET /v1/me/overview, with a web session or a machine token.

One answer says what waits for the caller, what runs and what ended lately, over every project the caller holds a
grant on; a hub admin's projects without one are left out. Each project goes through the check of GET
/v1/projects/{p}/runs and GET /v1/projects/{p}/decisions, the same functions (``projects.project_access`` and
``runs.visible_plans``, through the project's hub sink), so a run or decision of a plan whose label the caller's grant
does not reach is neither listed nor counted. A decision is read as its run is (``views.readable_run``): through the
run's plan, and for an author run that has put no plan yet, through the project's default label
(``views.sees_unplanned``). The queries then take the visible (project, plan) pairs at once, and
use the indexes the runs, decisions and plans already have.

- counts: waiting_on_you, the open decisions asked of the caller (``decision_reads.asked_person``: the person a decision
  was asked of, else the principal of an autonomy gate's run, else the run's owner), the ones the caller answers;
  running, the runs in RUNNING_STATES; queued; and the runs that ended done, failed or lost on the last DAYS days in
  UTC, today included;
- done_by_day: the runs that ended done on each of those days, oldest first, so its sum is counts.done_7d;
- active_runs: at most MAX_ACTIVE runs in IN_FLIGHT, those whose agent works first, then waiting, idle, parked, queued,
  the newest first within each;
- recent_runs: the MAX_RECENT runs that ended last, in any end state;
- a local run (``local_runs``) shows in active_runs (also while it is idle), in recent_runs when it ended, in counts
  and in open_decisions, for its member alone, whatever its plan and with none: no other member sees it;
- open_decisions: at most MAX_DECISIONS open decisions, of plan runs and author runs, those asked of the caller first,
  the oldest first, each with who it is asked of and when its run parks for want of an answer
  (EVO_HUB_DECISION_WAIT_SECONDS after it started waiting); plan_id is null for the decision of an author run that has
  put no plan yet;
- author_waiting: at most MAX_AUTHOR_WAITING of the caller's own author runs (``evo_agents.hub.author``) that wait for
  their reply in the chat, waiting or parked, the latest first, each with the agent's last message: those of a new
  plan, which no plan's label covers, and those of a plan the caller sees. A run with a decision still open is left
  out: open_decisions lists it through its decision;
- projects: each with the caller's role and max level, its repos, its active plans the caller sees, the open
  decisions of those plans, and where its Curator stands when it has a charter
  (``curator.service.status.curator_overview``).

``evo_agents.hub.server.today`` builds Today on the same checks (``runs.service.overview.scope``,
``runs.repository.overview.parameters`` and the statements' helpers). The route passes the engine to
``runs.service.overview``, which reads in one ``UnitOfWork``; the queries are in ``runs.repository.overview`` and the
models in ``runs.models.overview``.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models.overview import DAYS as DAYS
from evo_agents.hub.server.runs.models.overview import MAX_ACTIVE as MAX_ACTIVE
from evo_agents.hub.server.runs.models.overview import MAX_AUTHOR_WAITING as MAX_AUTHOR_WAITING
from evo_agents.hub.server.runs.models.overview import MAX_DECISIONS as MAX_DECISIONS
from evo_agents.hub.server.runs.models.overview import MAX_RECENT as MAX_RECENT
from evo_agents.hub.server.runs.models.overview import RUNNING_STATES as RUNNING_STATES
from evo_agents.hub.server.runs.models.overview import DayCount as DayCount
from evo_agents.hub.server.runs.models.overview import Overview
from evo_agents.hub.server.runs.models.overview import OverviewAuthorWait as OverviewAuthorWait
from evo_agents.hub.server.runs.models.overview import OverviewCounts as OverviewCounts
from evo_agents.hub.server.runs.models.overview import OverviewDecision as OverviewDecision
from evo_agents.hub.server.runs.models.overview import OverviewProject as OverviewProject
from evo_agents.hub.server.runs.models.overview import OverviewRun as OverviewRun
from evo_agents.hub.server.runs.service import overview as service
from evo_agents.hub.server.runs.service.overview import running_by_member  # noqa: F401 (org)
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/me", tags=["overview"], responses={401: {"model": ErrorBody}})


@router.get("/overview", response_model=Overview)
async def overview(request: Request, user: CurrentUser) -> Overview:
    """What waits for you, what runs and what ended lately, over the projects you hold a grant on."""
    wait = timedelta(seconds=request.app.state.config.decision_wait_seconds)
    return await service.overview(request.app.state.engine, user, wait)
