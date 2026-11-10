"""The night shift's side of the Curator's changes (``evo_agents.hub.server.changes`` describes the design): it
queues the judge run of a change waiting for one and the Builder of a planned change, and moves a change on when a run
of it ends."""

from __future__ import annotations

import logging

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, judge, runs, tables
from evo_agents.hub.plans import step_key
from evo_agents.hub.server import audit, merge_gate
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.change_views import change_of_plan, set_change
from evo_agents.hub.server.protection import (
    reason_line,
    ruleset_wait,
)
from evo_agents.hub.server.run_state import RunStep, notify_queued, write_event
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.runs.service.views import lock_plan, plan_activity

log = logging.getLogger(__name__)

BUILD = "curator.build"  # the night shift queued a change's Builder: "<project>/<plan> run:<id> change:<id>"
JUDGE = "curator.judge"  # the night shift queued a change's judge run: "<project>/<plan> run:<id> change:<id> ..."


# The night shift


async def _owner_secret(conn: AsyncConnection, owner_id: int, project_id: int, name: str | None) -> bool:
    """Whether the owner has a live git secret ``name`` bound to the project."""
    if not name:
        return False
    s, b = tables.secrets, tables.secret_bindings
    bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id)
    query = select(
        exists().where(
            s.c.owner_id == owner_id,
            s.c.name == name,
            s.c.kind == "git",
            s.c.deleted_at.is_(None),
            (s.c.expires_at.is_(None)) | (s.c.expires_at > func.now()),
            bound,
        )
    )
    return bool((await conn.execute(query)).scalar_one())


def _run_values(
    due, gate, *, kind: str, plan_id: str, revision: int, title: str | None, runtime: str, model, repos
) -> dict:
    budget = gate.budget
    return {
        "kind": kind,
        "project_id": due.project_id,
        "plan_id": plan_id,
        "title": title,
        "plan_revision": revision,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        **gate.duty.columns(),
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": model,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "repos": repos,
        "schedule_id": due.id,
        "schedule_night": gate.night,
        "budget": budget,
    }


def _caps(budget: dict) -> str:
    return (
        f"cost cap {curator.money(budget['max_usd'])}, {budget['max_turns']} turns, "
        f"{budget['max_seconds'] // 60} minutes of agent time"
    )


async def _waits(conn: AsyncConnection, change, why: str) -> str:
    """Keep ``why`` as the reason of planned ``change``, which the night shift passed over, written only when its
    reason says something else; the line the job's log names it by."""
    text = reason_line(why)
    if change.reason != text:
        c = tables.curator_changes
        await conn.execute(
            update(c)
            .values(reason=text, updated_at=func.now())
            .where(c.c.id == change.id, c.c.state == "planned", c.c.reason.is_distinct_from(text))
        )
    return f"{change.plan_id}: {why}"


async def _git_secret_wait(conn: AsyncConnection, due, change) -> str | None:
    """Why the Builder of ``change``, on GitLab, waits for the owner's git secret; None once the charter names one the
    owner has, live and bound to the project."""
    name = due.body.get("git_secret")
    if await _owner_secret(conn, due.owner_id, due.project_id, name):
        return None
    what = f"repo {change.repo} is on GitLab, where a Builder pushes with the owner's git secret the charter names"
    if not name:
        return f"{what}, and the charter names none (git_secret)"
    return f"{what}, and {due.owner} has no live git secret {name} bound to project {due.project}"


def unready_steps(body) -> list[tuple[str, str | None]]:
    """(the key, why it is not ready to run or None) of each step of plan ``body``."""
    steps = body.get("steps") if isinstance(body, dict) else None
    steps = steps if isinstance(steps, list) else []
    return [(step_key(step, index), runs.unready_reason(body, step)) for index, step in enumerate(steps)]


async def _run_wait(conn: AsyncConnection, project_id: int, change, held) -> str | None:
    """Why the Builder of ``change`` waits for its plan ``held``: a run of the plan or a judge run of the change still
    active, or no step ready to run; None when the night shift may queue it. Takes the plan's lock."""
    await lock_plan(conn, project_id, change.plan_id)
    activity = await plan_activity(conn, project_id, change.plan_id)
    r = tables.runs
    judging = (
        await conn.execute(
            select(
                exists().where(
                    r.c.project_id == project_id,
                    r.c.plan_id == change.plan_id,
                    r.c.kind == "judge",
                    r.c.state.in_(runs.ACTIVE_STATES),
                )
            )
        )
    ).scalar_one()
    if activity.plan_run is not None or activity.steps or judging:
        return f"a run of its plan {change.plan_id} is still active: the night shift builds it once that run ends"
    unready = unready_steps(held.body)
    if any(why is None for _, why in unready):
        return None
    shown = "; ".join(f"step {key}: {why}" for key, why in unready[:3]) or "it has no step"
    last = f"; its last Builder was run #{change.builder_run_id}" if change.builder_run_id else ""
    return f"its plan {change.plan_id} has no step ready to run ({shown}){last}"


async def _build_wait(conn: AsyncConnection, due, gate, change, runtime: str, *, curator_app: bool):
    """(the plan held, None) when the night shift may queue the Builder of planned ``change`` now; else (None, why)."""
    access = gate.access
    held = await plan_routes._held(conn, access, change.plan_id)
    if held is None or not access.visible(held.label, plan_routes._sink(access, None)):
        return (
            None,
            f"its plan {change.plan_id} is not on the hub, or {due.owner}, the night shift's owner, may not read it",
        )
    why = None
    if change.forge == "github":
        why = await ruleset_wait(conn, due.project_id, change.repo, curator_app=curator_app)
    elif change.forge == "gitlab":
        why = await _git_secret_wait(conn, due, change)
    why = why or await _run_wait(conn, due.project_id, change, held)
    problems = [] if why else gate.duty.unfit(due.project, "plan", [change.repo], runtime)
    if problems:
        why = f"{gate.duty.where} on duty cannot build it: {'; '.join(problems)}"
    return (None, why) if why else (held, None)


async def _builder_queued(conn: AsyncConnection, due, gate, change, run_id: int) -> None:
    await set_change(conn, change.id, builder_run_id=run_id, reason=None)
    text = (
        f"Queued by the night shift of project {due.project} as the Builder of the Curator's change #{change.id} "
        f"(proposal #{change.proposal_id}, tier {change.tier}), as {due.owner} on {gate.duty.where}, on "
        f"{change.branch} of {change.repo} alone: {_caps(gate.budget)}."
    )
    await write_event(conn, run_id, {"text": text, "change_id": change.id, **gate.budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=BUILD,
        target=f"{due.project}/{change.plan_id} run:{run_id} change:{change.id}",
        project_id=due.project_id,
    )
    log.info("curator builder queued", extra={"project": due.project, "run_id": run_id, "change_id": change.id})


async def queue_builder(conn: AsyncConnection, due, gate, *, curator_app: bool, only: int | None = None) -> int | None:
    """Queue the Builder of the oldest planned change of the project the night shift may build now, or of change
    ``only`` alone (``build_now``); its run's id, or None (see ``changes``). Each planned change it passes
    over keeps why as its reason (``_waits``). ``curator_app``: whether this hub has the Curator's App. Called under the
    schedule's row lock."""
    c = tables.curator_changes
    query = select(c).where(c.c.project_id == due.project_id, c.c.state == "planned")
    planned = (await conn.execute((query.where(c.c.id == only) if only else query).order_by(c.c.id))).all()
    if not planned:
        return None
    builder = due.body.get("builder") or {}
    runtime = builder.get("runtime") or curator.DEFAULT_RUNTIME
    skipped = []
    for change in planned:
        held, why = await _build_wait(conn, due, gate, change, runtime, curator_app=curator_app)
        if held is not None:
            values = _run_values(
                due,
                gate,
                kind="plan",
                plan_id=change.plan_id,
                revision=held.revision,
                title=runs.step_title(held.body),
                runtime=runtime,
                model=builder.get("model"),
                repos=[{"repo": change.repo, "branch": change.branch}],
            )
            try:
                run_id = await insert_run(conn, values)
            except IntegrityError:  # another run of the plan got in first
                why = f"a run of its plan {change.plan_id} got in first: the night shift builds it once that run ends"
            else:
                await _builder_queued(conn, due, gate, change, run_id)
                return run_id
        assert why is not None  # _build_wait gives the plan or why it waits
        skipped.append(await _waits(conn, change, why))
    log.info("no curator change to build", extra={"project": due.project, "why": "; ".join(skipped)})
    return None


async def queue_judge(conn: AsyncConnection, due, gate) -> int | None:
    """Queue the judge run of the oldest change of the project that waits for one; its id, or None. The Judge runs on
    Codex when the project's policy clears Codex for the change's label and the worker on duty has it, else on Claude
    Code with a model other than the Builder's (``judge.judge_runtime``); a runtime of ``gate.avoid`` is not on duty.
    Called under the schedule's row lock."""
    c, p = tables.curator_changes, tables.proposals
    waiting = (
        await conn.execute(
            select(c, p.c.label.label("proposal_label"), p.c.title.label("proposal_title"))
            .join_from(c, p, p.c.id == c.c.proposal_id)
            .where(c.c.project_id == due.project_id, c.c.state == "judge_pending")
            .order_by(c.c.id)
            .limit(1)
        )
    ).one_or_none()
    if waiting is None:
        return None
    access, duty, charter = gate.access, gate.duty, due.body
    held = await plan_routes._held(conn, access, waiting.plan_id)
    if held is None:
        await set_change(conn, waiting.id, state="open", reason="its plan is no longer on the hub")
        return None
    await lock_plan(conn, due.project_id, waiting.plan_id)
    activity = await plan_activity(conn, due.project_id, waiting.plan_id)
    if activity.plan_run is not None or activity.steps:
        return None  # a Builder of the change still runs: never both at once
    usable = {name for name in runs.RUNTIMES if duty.runtime(name) is not None and name not in gate.avoid}
    cleared = judge.codex_cleared(access.rules, waiting.proposal_label)
    runtime, model, why = judge.judge_runtime(
        charter.get("judge") or {}, charter.get("builder") or {}, codex_allowed=cleared, worker_runtimes=usable
    )
    problems = duty.unfit(due.project, "judge", [waiting.repo], runtime)
    if runtime in gate.avoid:  # its dependency waits its backoff, and no other runtime is on duty
        problems = [f"{runtime} is held off: it failed of an infra cause"]
    if problems:
        log.info("no judge run yet", extra={"project": due.project, "why": "; ".join(problems)})
        return None
    values = _run_values(
        due,
        gate,
        kind="judge",
        plan_id=waiting.plan_id,
        revision=held.revision,
        title=f"Judge of the Curator's change #{waiting.id}: {waiting.proposal_title}"[:200],
        runtime=runtime,
        model=model,
        repos=[{"repo": waiting.repo, "branch": waiting.branch}],
    )
    values["max_attempts"] = 1  # a judge run that ends without a verdict is tried again by the change, not the reaper
    try:
        run_id = await insert_run(conn, values)
    except IntegrityError:  # another judge run of the project is active
        return None
    await set_change(conn, waiting.id, state="judging", judge_run_id=run_id, reason=None)
    head = waiting.head_sha[:12] if waiting.head_sha else f"the tip of {waiting.branch}"
    text = (
        f"Queued by the night shift of project {due.project} as the Judge of the Curator's change #{waiting.id} "
        f"(proposal #{waiting.proposal_id}, tier {waiting.tier}) at {head}, on {runtime}"
        f"{f' ({model})' if model else ''}: {why}. It reads the proposal, the diff, the plan's verify and the "
        f"project's hidden checks, never the Builder's transcript: {_caps(gate.budget)}."
    )
    await write_event(conn, run_id, {"text": text, "change_id": waiting.id, **gate.budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=JUDGE,
        target=f"{due.project}/{waiting.plan_id} run:{run_id} change:{waiting.id} runtime={runtime}",
        project_id=due.project_id,
    )
    log.info("curator judge queued", extra={"project": due.project, "run_id": run_id, "change_id": waiting.id})
    return run_id


# When a run of a change ends


def _all_done(body) -> bool:
    steps = body.get("steps") if isinstance(body, dict) else None
    return (
        isinstance(steps, list)
        and bool(steps)
        and all(isinstance(step, dict) and step.get("status") == "done" for step in steps)
    )


async def builder_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> None:
    """What the end of plan run ``found`` does to its change, when its plan is the Curator's: done with every step
    of the plan done, the change waits for its pull request (GitHub) or its Judge (GitLab); otherwise it stays
    planned, with why."""
    if new not in runs.TERMINAL_STATES or found.project_id is None:
        return
    change = await change_of_plan(conn, found.project_id, found.plan_id, lock=True)
    if change is None or change.state != "planned" or old == "parked":
        return
    pl = tables.plans
    body = (
        await conn.execute(select(pl.c.body).where(pl.c.project_id == found.project_id, pl.c.plan_id == found.plan_id))
    ).scalar_one_or_none()
    if new == "done" and _all_done(body):
        state = "pr_pending" if change.forge == "github" else "judge_pending"
        await set_change(conn, change.id, state=state, builder_run_id=found.run_id, reason=None)
        what = (
            "the hub opens its pull request"
            if state == "pr_pending"
            else "its merge request is open; its Judge comes next"
        )
        await write_event(
            conn, found.run_id, {"text": f"The Curator's change #{change.id} is built: {what}.", "change_id": change.id}
        )
        return
    why = found.error or reason if new != "done" else "its plan has steps not done"
    await set_change(
        conn,
        change.id,
        builder_run_id=found.run_id,
        reason=reason_line(f"Builder run #{found.run_id} ended {new}: {why}"),
    )
    if new == "failed":
        await ledger_lines.build_failed(conn, change, found.run_id, new, why)


async def judge_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> None:
    """What the end of judge run ``found`` does: a run that ends without the verdict of its change queues another
    (the change waits for a Judge again), JUDGE_ATTEMPTS in all, then leaves the change open; a failure sends the
    owner the notice run_failed."""
    if new not in runs.TERMINAL_STATES or found.project_id is None:
        return
    from evo_agents.hub.server.notifications import notify, run_link

    if new == "failed":
        error = found.error or reason
        await notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=found.project_id,
            run_id=found.run_id,
            title=await merge_gate.judge_run_title(conn, found.run_id, found.plan_id),
            body=error,
            details={"run_kind": "judge", "plan_id": found.plan_id, "error": error},
            link=run_link(found.project, found.run_id),
        )
    change = await change_of_plan(conn, found.project_id, found.plan_id, lock=True)
    if change is None or change.state != "judging" or change.judge_run_id != found.run_id:
        return
    attempts = change.judge_attempts + 1
    why = found.error or reason
    if attempts >= judge.JUDGE_ATTEMPTS:
        await set_change(
            conn,
            change.id,
            state="open",
            judge_attempts=attempts,
            reason=reason_line(f"{attempts} judge runs ended without a verdict, the last {new}: {why}"),
        )
        return
    await set_change(
        conn,
        change.id,
        state="judge_pending",
        judge_attempts=attempts,
        reason=reason_line(f"judge run #{found.run_id} ended {new} without a verdict ({why}): another one comes"),
    )
