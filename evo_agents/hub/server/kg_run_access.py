"""The token of a KG run: what the agent of a review run of lens kg_questions or kg_init, or of a batch of an answer
job, calls the hub's REST API with, so the commands its prompt names (``evo-agents hub kg init --questions FILE``,
``hub kg init --decide FILE``, ``hub kg answer --reply FILE``) work on a node with no ``hub login`` of its own.

Such a run is the Curator's, so it never runs as its owner (``run_access.may_run_as_owner``). While its worker asks
for its leases, ``run_access.lease`` hands it instead a token of kind run (``evr_...``) of the member who dispatched it,
whose host is ``kg-run:<run id>`` (``host``), in a lease of kind hub and provider run-access named ``kg-run:<project>``
(``lease_name``), with the variable EVO_HUB_TOKEN; its worker puts EVO_HUB_TOKEN and EVO_HUB_URL into the agent's
environment, a guarded run's too (``evo_agents.worker.credential_handover.run_access_env``). The token lives as a run's
token does (``security._live_run_token``): while its lease is out, the run is held by that worker, and the member still
holds writer on the project; it ends with the lease.

What it reaches is ``ROUTES`` on its run's project alone (``refusal``), every other path refused with 403 before any
route answers, and ``project_access`` finds no other project for it (``Principal.reaches``):

- GET /v1/kg/{project}/init, which ``hub kg init`` reads to find the run in flight;
- POST /v1/kg/{project}/init/{id}/questions and .../decision, which take the run's own kg_init run alone
  (``kg_questions``, ``kg_init``: ``acts_for_run``);
- POST /v1/kg/{project}/answers/{job}/replies, for the questions of its own batch alone (``kg_answers``);
- POST /v1/kg/{project}/tools/{tool} and GET /v1/kg/{project}/eval-set, read through the agents' sink whatever the
  request names (``sink``), as the agents read the graph on /mcp;
- POST /v1/kg/{project}/entity-map, the entity map a run of strategy entity-graph-agent made, checked through that
  sink too (``kg_entity_map``).

It is never a hub admin, starts no run (``run_access_rules.ACTING_RUN``), and each write made with it is a
run_access.write audit row naming the run, as for any run's token.
"""

from __future__ import annotations

import re
from typing import overload

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import KG_RUN

KINDS = ("review",)  # the kinds of a KG run
_PROJECT = r"(?P<project>[^/]+)"
ROUTES = (
    ("GET", re.compile(rf"/v1/kg/{_PROJECT}/init")),
    ("POST", re.compile(rf"/v1/kg/{_PROJECT}/init/[0-9]+/questions")),
    ("POST", re.compile(rf"/v1/kg/{_PROJECT}/init/[0-9]+/decision")),
    ("POST", re.compile(rf"/v1/kg/{_PROJECT}/answers/[0-9]+/replies")),
    ("POST", re.compile(rf"/v1/kg/{_PROJECT}/tools/[a-z_]+")),
    ("GET", re.compile(rf"/v1/kg/{_PROJECT}/eval-set")),
    ("POST", re.compile(rf"/v1/kg/{_PROJECT}/entity-map")),
)
REFUSED = (
    "a KG run's token reaches only the KG routes its run needs on project {project}: reading its kg_init runs, its graph "
    "and its eval set, sending the run's starter questions, decision, answers or entity map"
)


def host(run_id: int) -> str:
    """The host the token of KG run ``run_id`` is listed with (tokens.host)."""
    return f"{KG_RUN}:{run_id}"


def host_of(run_id):
    """``host`` of a column or expression of a run's id, in SQL."""
    return func.concat(f"{KG_RUN}:", run_id)


def lease_name(project: str) -> str:
    """The name of the lease of a KG run's token of project ``project``."""
    return f"{KG_RUN}:{project}"


async def is_kg_run(conn: AsyncConnection, run_id: int, kind: str) -> bool:
    """Whether run ``run_id`` of ``kind`` is a KG run: the review run of a kg_init run, the one that writes its starter
    questions, or the run of a batch of an answer job."""
    if kind not in KINDS:
        return False
    t, a = tables.kg_init_runs, tables.kg_answers
    found = select(
        or_(
            exists().where(or_(t.c.agent_run_id == run_id, t.c.questions_run_id == run_id)),
            exists().where(a.c.run_id == run_id),
        )
    )
    return bool(await conn.scalar(found))


def refusal(method: str, path: str, project: str) -> str | None:
    """Why the token of a KG run of ``project`` may not make the request ``method`` ``path``; None when it may."""
    for allowed, pattern in ROUTES:
        found = pattern.fullmatch(path)
        if found is not None and method == allowed and found["project"] == project:
            return None
    return REFUSED.format(project=project)


@overload
def sink(user, asked: str) -> str: ...
@overload
def sink(user, asked: str | None) -> str | None: ...
def sink(user, asked: str | None) -> str | None:
    """The sink a read of ``user`` goes through: the agents' sink for a KG run's token, whatever it asked; ``asked``
    for anyone else."""
    if getattr(user, "kg_project", None) is None:
        return asked
    from evo_agents.hub.kg_health_eval import AGENT_SINK  # it reads the evaluation: imported when used

    return AGENT_SINK


__all__ = ["KINDS", "ROUTES", "host", "host_of", "is_kg_run", "lease_name", "refusal", "sink"]
