"""Reading decisions: who may read one (``GRANTED`` lists a project's, ``READERS`` read one), the list of a project's
decisions a page at a time (``page``) and every view of one (``decision_view``), with the outputs it carries, where
its message stands on each channel (``channels``) and what a wait waits for (``waiting_on``).

A decision is read by the readers of its run, as ``views.readable_run`` reads it: the readers of its plan
(``views.visible_plans``), and for an author run that has put no plan, those who read the project's runs on no plan
(``views.sees_unplanned``); a local run's decisions are its owner's alone.
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server import waiting_on
from evo_agents.hub.server.base.errors import NotFound
from evo_agents.hub.server.base.scope import Noun, ProjectScope, Scope
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.decisions import repository
from evo_agents.hub.server.decisions.models import Decision, DecisionList, DecisionOption, WakeOn
from evo_agents.hub.server.decisions.service import channels
from evo_agents.hub.server.notifications.service.member_channels import effective_label
from evo_agents.hub.server.output_views import by_decision as outputs_by_decision
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.runs.service.views import sees_unplanned, visible_plans
from evo_agents.hub.server.security import Principal


class Granted(ProjectScope):
    """A caller with access to the project, as ``project_access`` gives it (404 without), no role asked: the list
    shows each caller the decisions of the runs it reads, none to a hub admin without a grant."""

    def refusal(self, user: Principal, access: ProjectAccess) -> str | None:
        return None


# A decision is read with the plans of its run: a refusal names the plans, as the routes of plans do.
NOUN = Noun("a decision", "the plans")
GRANTED = Granted("reader", NOUN)
READERS = ProjectScope("reader", NOUN)


def _decision(row) -> Decision:
    fields = dict(row._mapping)
    shown = [
        DecisionOption(
            key=option["key"],
            label=option["label"],
            description=option.get("description"),
            recommended=option.get("recommended") is True,
            waits=option.get("waits") is True,
        )
        for option in fields.pop("options")
    ]
    recommended = next((option.key for option in shown if option.recommended), None)
    wake_on = fields.pop("wake_on")
    shown_wake = None if wake_on is None else WakeOn(kind=wake_on["kind"], contains=wake_on.get("contains") or {})
    fields["evidence"] = fields["evidence"] or []
    given, default_label, levels = fields.pop("label_given"), fields.pop("default_label"), fields.pop("levels")
    label = effective_label(given, default_label, levels)
    return Decision(
        **fields,
        options=shown,
        recommended=recommended,
        wake_on=shown_wake,
        label=label,
        label_given=given is not None,
    )


async def decision_view(conn: AsyncConnection, decision_id: int, plans: Collection[str] | None = None) -> Decision:
    """Decision ``decision_id`` as every view shows it; ``plans`` are those its reader reads (``waiting_on.fill``)."""
    (view,) = await with_outputs(conn, [_decision(await repository.one(conn, decision_id))], plans)
    return view


async def with_outputs(
    conn: AsyncConnection, decisions: list[Decision], plans: Collection[str] | None = None
) -> list[Decision]:
    """``decisions``, each with the outputs it carries, where its message stands on each channel
    (``channels``), and a wait with what it waits for (``waiting_on.fill``, with the plans the reader reads)."""
    await waiting_on.fill(conn, decisions, plans)
    await channels.fill(conn, decisions)
    by_project: dict[str, list[int]] = {}
    for decision in decisions:
        by_project.setdefault(decision.project, []).append(decision.id)
    for project, ids in by_project.items():
        found = await outputs_by_decision(conn, project, ids)
        for decision in decisions:
            if decision.project == project:
                decision.outputs = found.get(decision.id, [])
    return decisions


def _no_decision(project: str, decision_id: int) -> NotFound:
    return NotFound(f"project {project} has no decision {decision_id}: see GET /v1/projects/{project}/decisions")


async def visible(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, decision_id: int, sink: str | None
) -> None:
    """404, as for none, unless decision ``decision_id`` of the project is there and its run readable to ``user``:
    its plan visible, or for an author run that has put no plan, the project's runs on no plan
    (``views.readable_run``)."""
    found = await repository.run_of(conn, access.project_id, decision_id)
    if found is None or (found.kind == "local" and found.dispatched_by != user.user_id):  # a local run is its owner's
        raise _no_decision(access.name, decision_id)
    if found[0] is None:
        seen = sees_unplanned(access, sink)
    else:
        seen = found[0] in await visible_plans(conn, access, sink)
    if not seen:
        raise _no_decision(access.name, decision_id)


async def readable_decision(
    conn: AsyncConnection, user: Principal, project: str, decision_id: int, sink: str | None
) -> ProjectAccess:
    """The caller's access to ``project`` when it may read decision ``decision_id`` of it: a grant on the project (404
    without, 403 for a hub admin without one, ``READERS``) and the decision's run readable to it (``visible``)."""
    access = await READERS.check(conn, user, project)
    await visible(conn, user, access, decision_id, sink)
    return access


async def shown(scope: Scope, decision_id: int, sink: str | None) -> Decision:
    """Decision ``decision_id`` of the scope's project, once the caller may read it."""
    await visible(scope.conn, scope.user, scope.access, decision_id, sink)
    return await decision_view(scope.conn, decision_id, await visible_plans(scope.conn, scope.access, sink))


async def page(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    states: list[str],
    run_id: int | None,
    plan_id: str | None,
    limit: int,
    offset: int,
    sink: str | None,
) -> DecisionList:
    """The project's decisions, newest first, of the runs ``user`` may read: of any of ``states`` (any when empty),
    of run ``run_id`` and plan ``plan_id`` when given, ``limit`` of them after the first ``offset``."""
    plans = await visible_plans(conn, access, sink)
    unplanned = sees_unplanned(access, sink)
    states = list(dict.fromkeys(states))
    where = repository.listed(access.project_id, plans, unplanned, states, run_id, plan_id, user.user_id)
    rows = await repository.page(conn, where, limit, offset)
    found = await with_outputs(conn, [_decision(row) for row in rows], plans)
    total = await repository.count(conn, where)
    return DecisionList(decisions=found, total=total, limit=limit, offset=offset)


async def listing(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    *,
    states: list[str],
    run_id: int | None = None,
    plan_id: str | None = None,
    limit: int,
    offset: int = 0,
    sink: str | None = None,
) -> DecisionList:
    """``page`` in a transaction of its own, for a caller outside the routes (the MCP tool ``decision_list``)."""
    async with UnitOfWork(engine) as conn:
        access = await GRANTED.check(conn, user, project)
        return await page(conn, user, access, states, run_id, plan_id, limit, offset, sink)
