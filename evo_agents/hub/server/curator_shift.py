"""The job of the night shift: ``fire_schedules`` (the job hub.fire_schedules) and the gate it and the other jobs of the
night queue their runs through (``gate_of``). ``evo_agents.hub.server.curator`` holds the charter, the schedules and
pausing them; its docstring says what the job does."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from typing import cast

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, func, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, hub_settings
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.curator import NIGHT_SHIFT
from evo_agents.hub.server.infra_breaker import infra_of
from evo_agents.hub.server.night_cost import next_run_budget, night_figures
from evo_agents.hub.server.night_duty import Duty, on_duty
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.service.dispatch import insert_run, plan_run_repos
from evo_agents.hub.server.runs.service.views import lock_plan, plan_activity
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.shift_hours import cancel_queued, changes_waiting

log = logging.getLogger(__name__)

DISPATCH = "curator.dispatch"  # a schedule queued a plan run: "<project>/<plan> run:<id> schedule:<id> night=<date>"


def due_schedules(now: datetime | None):
    """Each night_shift schedule of a live project the Curator is on for, with its project, its owner's login, the
    newest revision of its charter, and the local time in the charter's zone at ``now`` (the database's now when None)."""
    s, c, p, u = tables.schedules, tables.charters, tables.projects, tables.users
    newest = (
        select(c.c.project_id, func.max(c.c.revision).label("revision")).group_by(c.c.project_id).subquery("newest")
    )
    moment = func.now() if now is None else literal(now)
    return (
        select(
            s.c.id,
            s.c.project_id,
            p.c.name.label("project"),
            s.c.owner_id,
            u.c.login.label("owner"),
            c.c.revision,
            c.c.body,
            func.timezone(c.c.body["window"]["timezone"].astext, moment).label("local_now"),
            moment.label("now"),
        )
        .join_from(s, p, p.c.id == s.c.project_id)
        .join(u, u.c.id == s.c.owner_id)
        .join(newest, newest.c.project_id == s.c.project_id)
        .join(c, and_(c.c.project_id == newest.c.project_id, c.c.revision == newest.c.revision))
        .where(
            s.c.kind == NIGHT_SHIFT,
            p.c.archived_at.is_(None),  # an archived project has no night shift (project_archive)
            cast(ColumnElement[bool], hub_settings.feature_on("curator", s.c.project_id)),
        )
        .order_by(s.c.id)
    )


async def owner_access(conn: AsyncConnection, owner_id: int, login: str, project: str) -> ProjectAccess | None:
    """The access of the schedule's owner to the project, as a request of theirs would get it; None without one."""
    member = Principal(user_id=owner_id, login=login, admin=False, token_id=0, kind=MACHINE, token_hash="")
    try:
        return await project_access(conn, member, project)
    except HTTPException:
        return None


def _ready(body: dict) -> bool:
    steps = body.get("steps")
    return isinstance(steps, list) and any(runs.unready_reason(body, step) is None for step in steps)


async def _next_plan(conn: AsyncConnection, access: ProjectAccess, charter: dict, duty: Duty):
    """(plan held, its repos) of the first plan of the charter's night_plans the owner may read, with a ready step,
    no active run, and repos a worker on duty has; the plan's dispatch lock is taken. (None, why) when there is none."""
    runtime = charter["builder"]["runtime"]
    skipped = []
    for plan_id in charter["night_plans"]:
        held, through = await plan_routes._held(conn, access, plan_id), plan_routes._sink(access, None)
        if held is None or through is None or not access.visible(held.label, through):
            skipped.append(f"{plan_id}: not on the hub")
            continue
        await lock_plan(conn, access.project_id, plan_id)
        activity = await plan_activity(conn, access.project_id, plan_id)
        if activity.plan_run is not None or activity.steps:
            skipped.append(f"{plan_id}: has an active run")
            continue
        if not _ready(held.body):
            skipped.append(f"{plan_id}: no ready step")
            continue
        try:
            repos = plan_run_repos(held)
        except HTTPException as exc:
            skipped.append(f"{plan_id}: {exc.detail}")
            continue
        problems = duty.unfit(access.name, "plan", [entry["repo"] for entry in repos], runtime)
        if problems:
            skipped.append(f"{plan_id}: {'; '.join(problems)}")
            continue
        return (held, repos), None
    return None, "; ".join(skipped) or "the charter names no plan"


async def _queue(conn: AsyncConnection, due, night: date, duty: Duty, plan, budget: dict) -> int:
    """Queue the plan run of ``plan`` (held, repos) for schedule ``due`` and ``night``, placed on ``duty``, with
    ``budget``; its id. The insert's IntegrityError goes on to the caller, whose transaction stays usable."""
    held, repos = plan
    builder = due.body["builder"]
    values = {
        "kind": "plan",
        "project_id": due.project_id,
        "plan_id": held.plan_id,
        "title": runs.step_title(held.body),
        "plan_revision": held.revision,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        **duty.columns(),
        "requested_runtime": builder["runtime"],
        "runtime": builder["runtime"],
        "model": builder.get("model"),
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "repos": repos,
        "schedule_id": due.id,
        "schedule_night": night,
        "budget": budget,
    }
    run_id = await insert_run(conn, values)
    caps = (
        f"cost cap {curator.money(budget['max_usd'])}, {budget['max_turns']} turns, "
        f"{budget['max_seconds'] // 60} minutes of agent time"
    )
    text = (
        f"Queued by the night shift of project {due.project} (charter revision {due.revision}) for the night of "
        f"{night.isoformat()}, as {due.owner} on {duty.where}: {caps}."
    )
    await write_event(conn, run_id, {"text": text, "schedule_id": due.id, "night": night.isoformat(), **budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=DISPATCH,
        target=f"{due.project}/{held.plan_id} run:{run_id} schedule:{due.id} night={night.isoformat()}",
        project_id=due.project_id,
    )
    return run_id


async def writer_access(conn: AsyncConnection, due) -> ProjectAccess | None:
    access = await owner_access(conn, due.owner_id, due.owner, due.project)
    if access is None or not has_role(access.role, "writer"):
        log.warning("night shift skipped: its owner holds no writer grant", extra={"project": due.project})
        return None
    return access


async def gate_of(conn: AsyncConnection, due, *, figures_first=False, any_hour=False, beside=False, on_demand=False):
    """(word, gate) for schedule ``due`` now, in the caller's transaction, under the schedule's row lock it takes: a
    ``collect.Gate`` when the night shift may queue a run now, else None and the word for the job's summary. Paused,
    the schedule's queued runs are cancelled; outside its window, those of the night alone (``shift_hours``), and with
    ``any_hour`` (the job hub.fire_schedules) the gate is for the Curator's changes, ``inside`` false; ``beside``
    (``build_now``) opens it though another run of the schedule is in flight. With
    ``figures_first`` (the job curator.collect), the night's figures are counted as soon as the window is open, before
    a run of the schedule held now ends. With ``on_demand`` (a KG optimize run asked for, ``kg_optimize``) the window
    is not asked and nothing is cancelled."""
    from evo_agents.hub.server.collect import Gate, ensure_figures  # it queues runs through this module
    from evo_agents.hub.server.outcomes import check_circuit  # it pauses through this module

    s = tables.schedules
    locked = select(s.c.paused_at, s.c.updated_at).where(s.c.id == due.id).with_for_update(skip_locked=True)
    found = (await conn.execute(locked)).one_or_none()
    if found is None:
        return "locked", None
    window = due.body["window"]
    inside, night = curator.window_state(due.local_now, window["start"], window["end"])
    if found.paused_at is None and await check_circuit(conn, due, night, found.updated_at):
        return "circuit", None
    if found.paused_at is not None:
        cancelled = await cancel_queued(conn, [due.id], f"the night shift of {due.project} is paused")
        return ("cancelled" if cancelled else "paused"), None
    if not inside and not on_demand:
        why = f"the night shift of {due.project} is outside its window"
        cancelled = await cancel_queued(conn, [due.id], why, changes_too=False)
        if (cancelled and not beside) or not any_hour or not await changes_waiting(conn, due.project_id):
            return ("cancelled" if cancelled else "outside"), None
    access = None
    if figures_first:
        access = await writer_access(conn, due)
        if access is None:
            return "owner", None
        async with conn.begin_nested():
            await ensure_figures(conn, due, night, access)
    figures = await night_figures(conn, due.id, night)
    if figures.active_run_id is not None and not beside:
        return "busy", None
    if figures.runs >= due.body["max_runs_per_night"]:
        return "max_runs", None
    budget = next_run_budget(due.body, figures)
    if budget is None:
        return "spent", None
    access = access or await writer_access(conn, due)
    if access is None:
        return "owner", None
    duty, why = await on_duty(conn, due)
    if duty is None:
        log.info("night shift waits for its pool", extra={"project": due.project, "why": why})
        return "pool", None
    return "", Gate(night=night, figures=figures, budget=budget, access=access, duty=duty, inside=inside)


async def project_gate(conn: AsyncConnection, project_id: int):
    """(the project's night_shift schedule, the word, the gate) for a Builder a member asks for now: ``gate_of`` at
    any hour and beside the schedule's other runs; (None, "none", None) for a project without one."""
    due = (await conn.execute(due_schedules(None).where(tables.schedules.c.project_id == project_id))).one_or_none()
    if due is None:
        return None, "none", None
    word, gate = await gate_of(conn, due, any_hour=True, beside=True)
    return due, word, gate


async def _fire(conn: AsyncConnection, due, *, curator_app: bool) -> str:
    """What schedule ``due`` does now, in the caller's transaction; a word for the job's summary. A dependency failing
    of an infra cause holds off the kinds of run that use it alone (``infra_breaker``): ``infra`` when that left
    nothing to queue."""
    from evo_agents.hub.server.change_shift import queue_builder, queue_judge  # it queues runs through this module
    from evo_agents.hub.server.collect import queue_review  # it queues runs through this module
    from evo_agents.hub.server.history import queue_history  # it queues runs through this module
    from evo_agents.hub.server.probe_runs import queue_probe  # it queues runs through this module
    from evo_agents.hub.server.replays import queue_replay  # it queues runs through this module

    word, gate = await gate_of(conn, due, any_hour=True)
    if gate is None:
        return word
    infra = await infra_of(conn, due, due.now)
    if infra.hub is not None:  # every kind of run uses the hub
        return "infra"
    gate = replace(gate, avoid=infra.avoid)
    body, held = due.body, bool(infra.avoid)
    if gate.inside and not infra.holds(body, "review") and await queue_review(conn, due, gate) is not None:
        return "review"
    if gate.inside and not infra.holds(body, "history") and await queue_history(conn, due, gate) is not None:
        return "history"
    if await queue_judge(conn, due, gate) is not None:
        return "judge"
    if not infra.holds(body, "plan") and await queue_builder(conn, due, gate, curator_app=curator_app) is not None:
        return "builder"
    if not infra.holds(body, "replay") and await queue_replay(conn, due, gate) is not None:
        return "replay"
    if not gate.inside:  # the night's plans wait for the window
        return "infra" if held else "outside"
    night, duty, budget = gate.night, gate.duty, gate.budget
    plan, why = (None, "held off") if infra.holds(body, "plan") else await _next_plan(conn, gate.access, body, duty)
    if plan is None:
        # the probes queue after every other kind of the night
        if not infra.holds(body, "probe") and await queue_probe(conn, due, gate) is not None:
            return "probe"
        if held:
            log.info(
                "night shift holds off a failing dependency",
                extra={"project": due.project, "avoid": sorted(gate.avoid)},
            )
            return "infra"
        log.info("night shift has no plan to run", extra={"project": due.project, "why": why})
        return "no_plan"
    try:
        run_id = await _queue(conn, due, night, duty, plan, budget)
    except IntegrityError:  # a dispatch of the plan got in first, though the plan's lock is held: try next minute
        return "no_plan"
    log.info(
        "night shift queued a run",
        extra={"project": due.project, "plan_id": plan[0].plan_id, "run_id": run_id, "night": night.isoformat()},
    )
    return "queued"


async def fire_schedules(engine: AsyncEngine, *, curator_app: bool, now: datetime | None = None) -> dict:
    """One pass of ``hub.fire_schedules`` over each night_shift schedule, in a transaction of its own, at ``now`` (the
    database's now when None), ``curator_app`` whether this hub has the Curator's App; how many ended each outcome."""
    async with engine.begin() as conn:
        due = (await conn.execute(due_schedules(now))).all()
    outcomes: Counter = Counter()
    for row in due:
        try:
            async with engine.begin() as conn:
                outcomes[await _fire(conn, row, curator_app=curator_app)] += 1
        except Exception:  # one schedule failing leaves the others their turn
            log.exception("a schedule failed", extra={"project": row.project, "schedule_id": row.id})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))
