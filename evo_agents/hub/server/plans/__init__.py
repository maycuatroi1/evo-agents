"""Plans of a project: the hub holds them, and git keeps read-only copies (``evo_agents.hub.mirror``).

A plan is a row of ``plans`` (its area, label, body as jsonb, revision and digest) with one row of
``plan_revisions`` per revision, only ever appended. Every write takes the plan's row lock, checks the body against
plan.schema.json and ``plan_semantics`` (repos against the repos registered for the project; a schema error is a
422, a semantic finding a warning in the answer), refuses with a 422 a verify new in the write that cannot be one shell
command line on a step not done (``evo_agents.hub.plans.unrunnable_verify``), refuses with a 422 a problem the write
names anew that the project does not have (``service.missing_problems``), writes the plan and its revision, and adds one audit
row naming the plan and the revision, never its content, all in one transaction, with the event ``plan.step`` of each
step whose status the write sets or changes (``evo_agents.hub.plan_step_status``). A PUT that writes a plan or a
revision whose body names no problem says so in its warnings, and stores it all the same.

- PUT creates a plan, or replaces it when ``if_revision`` is the revision the hub holds; anything else is a 409
  ``revision_conflict`` whose ``current`` is the plan as the hub holds it. A body equal to the one held (same digest
  and label) changes nothing and answers 200, so pushing the same files again is idempotent. An author run's agent
  writes through the same code (``write_plan``) with PUT /v1/worker/runs/{id}/plan, as the run's dispatcher, and the
  revision records the run (``run_id``, which the revisions show).
- PATCH sets keys of one item of a section, by index or (for steps) by step id, as ``evo harness step`` does, and
  needs ``if_revision`` too. A client that gets the 409 reads the plan again and retries on the new revision.
- POST .../complete moves the plan to the completed area, only when every step is done or descoped, or with
  ``mark_steps_done`` after marking each other step done with today's date.
- A PUT or PATCH of a plan the hub holds (an author run's and the hub's own writes of a run's step too) that leaves
  an active plan with steps, every one done or descoped, completes it in the same transaction, with the revision and
  audit row of POST .../complete; one that leaves a step open on a completed plan puts it back in the active area,
  with those of POST .../reopen. An abandoned or superseded plan stays where it is (``plans.settled_area``).
- POST .../abandon moves an active or completed plan to the completed area as abandoned (or superseded by another
  plan), with a date and a reason, leaving each step's status as it was; POST .../reopen puts a completed plan,
  abandoned, superseded or not, back in the active area.

Reads go through a sink of the project (X-Evo-Sink, by default its hub sink) and follow ``ProjectAccess.visible``;
a plan the caller cannot see answers the same 404 as one that does not exist. Reading needs a grant on the project:
a hub admin without one is answered 403, as by the knowledge graph. GET .../diff compares two revisions line by line
as their git copies read (``evo_agents.hub.plan_diff``), and GET .../handoff builds the handoff of the plan for a new
agent session within a token budget (``evo_agents.hub.handoff``). Writes need the writer role, and
the plan's label must stay below the clearance of the project's hub sink (``ProjectAccess.push_label``).

The package has four layers, which lint-imports keeps in this order (contract hub-plans-layers in pyproject.toml):
``routes`` hands the request to ``service``, which opens its transaction (``UnitOfWork``), checks the caller's role
(``PlanScope``, a ``ProjectScope``), checks and decides, and reads and writes through ``repository`` (``PlanStore``);
``models`` holds the request and answer bodies. Other modules import ``service`` and ``models``; the MCP tools and the
bridge of a plugin's page call the route handlers as functions. ``Conflict``, ``Written`` and ``REFUSALS`` are here,
in the package itself, because the OpenAPI document names the two models by their module.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans.models import Plan, Problem


class Conflict(ErrorBody):
    current: dict | None = Field(None, description="the plan as the hub holds it, when the caller may see it")


class Written(Plan):
    created: bool
    changed: bool = Field(description="false when the hub already held exactly this; no revision was added")
    warnings: list[Problem] = Field(description="what plan_semantics found; the plan was stored anyway")


READ_REFUSALS: dict[int | str, dict[str, Any]] = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": Conflict},
    413: {"model": ErrorBody},
    422: {"model": ErrorBody},
}
