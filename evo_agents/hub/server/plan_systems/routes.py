"""Run a plan in an agent system: POST /v1/projects/{p}/plans/{plan_id}/system-dispatch (writer), body
``{"system": slug}``, hands the plan to the system's entry agent as the caller would by hand.

The hub writes the caller's own message in their direct chat with the entry agent (``chats.service.direct.direct_chat``, made
when there is none): it names the plan, its revision, its page on the web and its pending steps, and says how to run it.
Read the plan with plan_show; take the pending steps in the order their depends_on allows; hand each step to the
system's agents along its interacts edges with a brief (goal, repo, criteria, verify, done when), marking it in_progress
as it goes out, so the plan's page shows it moving; mark a step done with its evidence once its verify passes, and one
that comes back unfinished pending again; ask the person about any decision it may not take; and report which steps are
done and which are left. The message goes through ``chats.service.messages.deliver_message`` as a typed one does, so it wakes the entry
agent and ``system_traces.join`` opens a trace at the system's revision now; the names the text mentions are not read as
mentions (``skip``), so the message hands work to the entry agent alone and opens no group. The trace then takes the
plan's id and revision (system_traces.plan_id and plan_revision, 0237), and the dispatch is audited once
(run.dispatch_system), naming the plan, the run, the system and the trace.

Refused with 403 without the writer role on the project (``dispatch.check_dispatcher``), 404 for a plan the caller
cannot read, 422 for a system the project lacks, and 409 for: a plan the Curator made (the night shift alone runs
it), a system with no entry agent, a plan with an active plan run or step run, a plan a trace runs whose runs have
not all ended, a plan with no pending step, and a message the entry agent took no run for, or whose run opened no
trace of the system (its refusal named). A refusal writes nothing: the message, the chat it would open and the run
roll back with it.

Every dispatch of a plan takes the plan's dispatch lock (``views.lock_plan``) before it reads the plan's activity, as
a plan run and a step run do, so two dispatches at once, or a dispatch and a plan run, give one trace or one run.
While a trace of the plan has a run queued, running, waiting or parked, a plan run and a step run of the plan get 409
naming the trace and its system, and ready-steps shows the trace (``views.plan_trace``); once every run of the trace
has ended, the plan is free again.

A chat run of the trace marks steps as any agent run does: with the MCP tool plan_step, which writes under the role of
the run's owner (``plans.patch``), the dispatcher here, a writer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plan_systems import service
from evo_agents.hub.server.plans.models import PlanId
from evo_agents.hub.server.runs.models import REFUSALS, SystemDispatch, SystemDispatched
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})


@router.post(
    "/{project}/plans/{plan_id}/system-dispatch",
    status_code=201,
    response_model=SystemDispatched,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def system_dispatch(
    request: Request, project: ProjectName, plan_id: PlanId, body: SystemDispatch, user: CurrentUser
) -> SystemDispatched:
    """Hand the plan to the system's entry agent: the caller's message in their direct chat with it names the plan and
    how to run it, and the trace it opens runs the plan."""
    public_url = request.app.state.config.public_url
    found = await service.dispatch(request.app.state.engine, user, project, plan_id, body.system, public_url)
    log.info(
        "plan handed to a system",
        extra={"project": project, "plan_id": plan_id, "system": body.system, "trace_id": found.trace_id},
    )
    return found
