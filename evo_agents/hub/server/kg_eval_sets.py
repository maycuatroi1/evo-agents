"""The retrieval question sets of a project's knowledge graph, versioned on the hub (kg_eval_sets, migration 0179).

A set is what ``evo-agents kg eval`` reads (``evo_agents.kg.evaluate``, docs/kg-eval.md): a question a JSON object,
each in the ``tune`` or the ``holdout`` part by the hash of its id. The hub keeps it so that every machine and the
hub's own evaluation of its graph (``evo_agents.hub.server.kg_health``) ask the same questions.

- PUT /v1/kg/{project}/eval-set {set, questions}: a writer puts the set; it is the next version, or the latest one
  again when its questions are the same (the same digest). 422 names the bad questions.
- GET /v1/kg/{project}/eval-set?version=&split=&answers=: the latest version (or the one named) for a reader whose
  grant reaches the project's default label through the sink (X-Evo-Sink, the hub sink by default), all of it or one
  part. A holdout question never carries its answers here: tuning reads the tune part, and the list of holdout
  questions, without answers, is all a machine gets of the other part. ``answers=false`` strips the tune answers too.

Only the hub's evaluation reads holdout answers, through ``latest_set``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub.kg_eval_starter import set_digest as set_digest
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.eval_sets import EvalSetBody, EvalSetPut, EvalSetView
from evo_agents.hub.server.kg.service import eval_sets as service
from evo_agents.hub.server.kg.service.eval_sets import holdout_questions as holdout_questions
from evo_agents.hub.server.kg.service.eval_sets import latest_set as latest_set
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg.evaluate import ALL_TYPES

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 422)},
)


@router.put("/{project}/eval-set", response_model=EvalSetPut)
async def put_eval_set(request: Request, project: ProjectName, body: EvalSetBody, user: CurrentUser) -> EvalSetPut:
    """Put the project's retrieval question set: the next version, or the latest again when it asks the same."""
    return await service.put_set(request.app.state.engine, user, project, body)


@router.get("/{project}/eval-set", response_model=EvalSetView)
async def get_eval_set(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    version: Annotated[int | None, Query(ge=1, description="this version, not the latest")] = None,
    split: Annotated[Literal["all", "tune", "holdout"], Query(description="only this part of the set")] = ALL_TYPES,
    answers: Annotated[bool, Query(description="false: no question carries its answers")] = True,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> EvalSetView:
    """The project's retrieval question set, or one part of it; holdout questions come without their answers."""
    return await service.read_set(request.app.state.engine, user, project, version, split, answers, sink)
