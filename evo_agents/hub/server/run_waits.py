"""The agent of a plan run waits for a time or an event of the hub (``evo-agents worker wait``) instead of asking its
owner to say when it came: POST /v1/worker/runs/{run}/waits, and the hub's own answer when it comes.

The route stores a decision of category ``wait`` (``runs.WAIT_DECISION_CATEGORY``) whose question is the agent's reason,
whose context says what the run waits for, and whose options let the person asked cut it short: Go on now and Stop
waiting. It notifies nobody, since it asks nobody anything, but it is open like any decision: the daemon moves the run
to waiting at the end of the agent's turn, and the reaper parks it at its next pass, since nobody answers it, so the
wait holds no worker (``run_reaper.park_waiting``); a wait that wakes within ``runs.DECISION_WAIT_SECONDS`` keeps the
slot as a decision does that long. ``wake_at`` is when it wakes at the latest: ``until``, or MAX_WAIT_DAYS from now when
only ``on`` is given, and never later than that, so a parked run is still there (``run_reaper.expire_parked`` leaves a
run alone while a wait of it has not come). ``wake_on`` is the event that wakes it sooner: an event of the run's project
in hub_events of kind ``on`` (``hub_events.KINDS``, or a kind a core plugin records of its own) whose payload contains
``contains``, written after the wait was asked (``after`` is the project's last event then). A wait on another plan's
step names ``plan.step`` (``evo_agents.hub.plan_step_status``) with the plan, the step and the status, such as
``{"plan": "northwind-launch", "step": "8", "status": "done"}``; the payload holds a plan's id and a step as text, so a
number given for one of TEXT_KEYS is taken as its text (``--contains step=8`` reads 8 as JSON). A wait on a run of a
GitHub Actions workflow names ``github.workflow_run`` with the run's id, and its repo unless the project has one on
GitHub (``workflow_waits``): the route takes the run to be one the hub can read, and the hub reads it on each pass of the
reaper, recording the event when the run completed or a job of it failed.

``wake_waits``, on each pass of the reaper (``run_reaper.recover_runs``), answers every open wait whose event or time
came, each in a transaction of its own under the locks an answer takes (``decision_resume.lock_run_of``): the decision
is answered by nobody (``answered_by`` NULL), its ``answer_text`` the words the agent gets (within MAX_ANSWER_BYTES; the
inbox takes an asked agent's answer whole), and those go to the run's inbox as a message naming the decision, as an owner's answer does; a parked run is resumed first, by the reaper
(``decision_resume.resume_parked``). A wait of a run that ended was cancelled with the run's other decisions.

A run still waiting when its wait wakes takes the message in its inbox from its worker; but when the reaper ran late,
the same pass parks it right after (``run_reaper.park_waiting``), and no worker hands a parked run its inbox: runs #1568
and #1582 stayed parked on no open decision, the answer untaken. ``resume_answered``, right after the parking, resumes
each parked run with no open decision and a message of its inbox no worker took, and moves those messages to the run
that resumes it; an owner's answer given just before the run parked is taken the same way.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.decisions.models import Decision
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import REFUSALS, RunId
from evo_agents.hub.server.runs.models.run_waits import (  # noqa: F401 (agents.service.asks, merge.service.check)
    MAX_WAIT,
    OPTIONS,
)
from evo_agents.hub.server.runs.models.run_waits import WaitAsk as WaitAsk
from evo_agents.hub.server.runs.models.run_waits import WaitIn as WaitIn
from evo_agents.hub.server.runs.service import run_waits as service
from evo_agents.hub.server.runs.service.run_waits import (  # noqa: F401 (run_reaper, wait_cycles, ops, orchestrator, agents, merge)
    resume_answered,
    wake,
    wake_waits,
)
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post(
    "/runs/{run_id}/waits",
    status_code=201,
    response_model=Decision,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def wait(request: Request, run_id: RunId, body: WaitIn, user: CurrentUser) -> Decision:
    """Wait, in a plan run this worker holds, for a time or an event of the hub: a decision of category wait the hub
    answers when either comes."""
    return await service.wait(request.app.state.engine, request.app.state.github_app, user, run_id, body)
