"""The health of each charter's knowledge graph, measured on the hub without any model, and the part of the night's
figures it gives (``kg_health``). ``evo_agents.hub.kg_coverage`` holds the model, ``evo_agents.hub.kg_build`` keeps
what each build covers and the files of its git sources, and ``evo_agents.hub.kg_health_eval`` scores retrieval.

``measure`` runs in each pass of the job ``curator.collect`` (``evo_agents.hub.server.collect``): every project with a
night shift (a schedule of kind night_shift, its curator feature on) and a knowledge config on the hub whose last
measure is HEALTH_EVERY old, or RETRY_AFTER old when that one failed, is measured once, and the measure is kept in
kg_health (schema 0055) with the build it read. A measure holds, for each git source of the project's knowledge
config:

- ``commit``, the commit the source read in the latest successful build (kg_source_files; the commit the build's
  coverage names as ``read``);
- what the forge says now, read with the workers' GitHub App and ``contents: read`` (``evo_agents.hub.server.merge.service.pulls``):
  the repo's default branch and the commit it points at (``head``), how many of its commits the commit read lacks
  (``behind``) and how long the oldest of them has waited (``lag_hours``, 0 when none does), and the files of the
  branch against those the build holds (``kg_coverage.compare_files``): ``missing``, ``stale`` and ``deleted``, each
  counted with a few paths, and the ``code:`` evidence a finding can cite for them; ``off``, those paths whole (up to
  ``kg_coverage.MAX_OFF_PATHS``), which the tier rules read and the figures leave out. A source with no successful
  build, a repo off GitHub, a hub without the App, an App not installed, a commit GitHub does not know, a tree GitHub
  cut: the entry says why (``why``) and keeps what was read;

and, once for the project, ``eval``: the retrieval evaluation of the latest build's graph (``kg_health_eval``), of the
holdout part of the project's question set (``evo_agents.hub.server.kg_eval_sets``) and the plans the schedule's owner
may read, asked as that owner's agents read the graph: through the agent sink (``kg_health_eval.AGENT_SINK``) when
the policy declares it, else the project's hub sink, with the owner's audience (``kg_audience.agent_audience``: the
chats they are cleared for). The same questions of the same graph read with the same audience keep the score of the
measure before, so a quiet day asks nothing again. Each measure whose eval has a score pushes the signals
EVAL_SIGNALS, kg.retrieval.hit_at_5 and kg.retrieval.tokens_per_file, as that owner: the score of the holdout part of
the set, the questions the plan's thresholds name, or of all the questions when the set has none.

It also holds ``review``: per rule, the items of the latest build's review queue (``evo_agents.hub.kg_review``) open
to that owner through the hub sink, those a writer dismissed aside, with the build they were counted in; or why there
are none (a build without clusters has no queue).

The eval and the review read one snapshot of the project's graph (``kg_snapshots.load``), loaded once per measure: on
the largest graphs a snapshot takes more than 3 GiB while it is made, and loading it for each made the worker's job
reach the 4 GiB of its container. Once the project is measured, the memory the snapshot took goes back to the system
(``give_back``): CPython's allocator keeps more than 1 GiB of it otherwise, which the next load adds to.

``figures`` counts the night's ``kg_health`` over [since, until): the latest measure before ``until`` (its time, its
build, its sources, eval and review); ``coverage``, what the latest successful build before ``until`` covers
(``kg_coverage.build_coverage``); ``builds``, the builds that finished in the span, how many succeeded, failed or
changed nothing (a build with the content hash of the succeeded build before it), with their coverage over time,
the latest first; and ``retrieval``, what agents got from the graph (``kg_coverage.retrieval``) in the sessions of the
figures and the runs that ended in the span, of a plan the owner may read or review runs. None for a project the hub
holds neither a graph nor a measure of.

GET /v1/projects/{p}/curator/kg-health lists the graph's health over time for a reader whose grant reaches the
project's default label through the sink (X-Evo-Sink, the hub sink by default), as the night's figures are read: the
latest measures, newest first, and the successful builds with what each covered and whether it changed anything.
``evo-agents hub kg health`` prints it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.health import (  # noqa: F401 (the tests and the migration's note read them here)
    ERROR_CHARS,
    GRAPHS_TRIED,
    HISTORY,
    NIGHT_SHIFT,
    NO_APP,
    NO_REPO,
    OFF_GITHUB,
    KgHealthView,
)
from evo_agents.hub.server.kg.service import health as service
from evo_agents.hub.server.kg.service.health import (  # noqa: F401 (collect, kg_init, the services and the tests read them here)
    audience_digest,
    figures,
    measure,
    measure_review,
    owner_access,
    push_eval_signals,
    snapshot_of,
)
from evo_agents.hub.server.kg.service.health import evaluate as _evaluate  # noqa: F401 (the tests call it here)
from evo_agents.hub.server.kg.service.health import owner_reads as _owner_reads  # noqa: F401 (kg_init, kg_questions)
from evo_agents.hub.server.kg.service.health import review as _review  # noqa: F401 (the tests call it here)
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}


@router.get("/{project}/curator/kg-health", response_model=KgHealthView, responses=READ_REFUSALS)
async def show_kg_health(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=HISTORY[0], le=HISTORY[1], description="measures and builds to list")] = HISTORY[2],
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> KgHealthView:
    """The health of the project's knowledge graph over time: its latest measures and its successful builds with what
    each covers, for a reader whose grant reaches the project's default label through the sink (the hub sink by
    default), as the night's figures are."""
    return await service.history(request.app.state.engine, user, project, limit, sink)
