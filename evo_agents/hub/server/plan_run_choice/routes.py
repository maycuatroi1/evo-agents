"""A member's last choice in the Run plan dialog of a project: GET and PUT /v1/projects/{p}/plan-run-choice.

The hub keeps one choice a member and project (plan_run_choices, 0238), so the dialog opens filled in with it: where
the plan runs (``target`` worker, a plan run on a worker, or system, the plan handed to the entry agent of the system
``system`` names, ``plan_systems``), the agent, the runtime, the model, the mode, the timeout in hours and whether each
step gets a session of its own. It never keeps as_me, browser identities or an account: a body naming one is 422. Each
member reads and writes their own and no one else's; the command line neither reads nor writes it. Reading needs a
grant on the project, writing the writer role, as a dispatch does.

A choice is checked when read, against the project as it is then. A field that names what can no longer be used goes
back to its default, and ``defaulted`` lists it: ``system`` for a system deleted, or without an entry agent (the
target is then worker); ``agent`` for an agent deleted or one a run of the project may not go under
(``runs.service.profiles.profile_named``); ``runtime`` for a runtime the project does not register or the agent does not
allow; ``model`` for a model the agent does not allow; ``mode`` and ``timeout_h`` for a value the hub no longer takes.
A member without a choice gets the defaults, with ``saved`` false. A dispatch still checks everything again.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plan_run_choice import service
from evo_agents.hub.server.plan_run_choice.models import REFUSALS, PlanRunChoice, PlanRunChoiceIn
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})


@router.get("/{project}/plan-run-choice", response_model=PlanRunChoice, responses=REFUSALS)
async def read_plan_run_choice(request: Request, project: ProjectName, user: CurrentUser) -> PlanRunChoice:
    """The caller's last choice in the Run plan dialog of the project, checked against the project now; the defaults
    when they saved none."""
    return await service.read(request.app.state.engine, user, project)


@router.put("/{project}/plan-run-choice", response_model=PlanRunChoice, responses=REFUSALS)
async def put_plan_run_choice(
    request: Request, project: ProjectName, body: PlanRunChoiceIn, user: CurrentUser
) -> PlanRunChoice:
    """Keep the caller's choice in the Run plan dialog of the project, in place of the one before; the answer is the
    choice as GET reads it."""
    return await service.keep(request.app.state.engine, user, project, body)
