"""The moves of a release cycle that the check, the landing, the watch of its CI and its owner's answer share
(``evo_agents.hub.releases`` describes the cycle, ``evo_agents.hub.release_ci`` why it stops).

``queue_run`` queues the next run of an open cycle, a cut or the fix of a red tag, dispatched as the cycle's owner,
pinned to no worker, with what is left of the cycle's cap as its cost cap (each agent session counted once, as the
status counts it); left under ``curator.MIN_RUN_USD``, it queues nothing and stops the cycle on its budget. Audited as
release.dispatch, with the owner as actor and no token.

``stop_cycle`` is the one way a cycle stops: open, it becomes stopped with the cause (``release_ci.STOP_CAUSES``) and
the reason, and the hub asks its owner what next (``ask_owner``): a decision of category release on the cycle's last
run, whose three options are to go on with one more cap of the schedule, to close the cycle, or to pause the schedule
(``release_answer`` does the one chosen), notified to the owner on their channels; and a thread of the hub's in the
project's triage chat (``triage.project_thread``) that says why, the tags with their workflow runs, the cost and the
decision. A stopped cycle moves no further until the owner answers, and no check opens another cycle while it is
there, unless a newer tag made outside it supersedes it (``release_check.supersede``). Asking happens in a savepoint: a thread or a decision that cannot be made leaves the cycle stopped, saying so in
the log.

``notify_release`` sends the owner a notice of kind release: a version published, or a CI the hub cannot read.
"""

from __future__ import annotations

import logging

from sqlalchemy import Text, cast, func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, releases, runs, tables
from evo_agents.hub.server import audit, triage
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.notifications import decision_link, notify, one_line
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.service.dispatch import insert_run

log = logging.getLogger(__name__)

DISPATCH = "release.dispatch"  # a cycle's run queued: "<project> cycle:<id> run:<id> mode=<mode>"
QUESTION_REASON_CHARS = 600
# Why the hub recommends closing a stopped cycle (``decision_asks``): it spends nothing and keeps the schedule.
CLOSE_REASON = (
    "Closing spends nothing more of the cap and keeps the schedule: its next daily check starts a new cycle when the "
    "newest tag is red or a packaging path changed. Go on only when the reason above is one more cap can fix."
)


def releases_link(project: str) -> str:
    """The web page of a project's daily release."""
    return f"/p/{project}/releases"


def cost_of(cycle_id: int):
    """What the runs of cycle ``cycle_id`` cost, each agent session once at its largest total, as a scalar query."""
    rr, r = tables.release_runs, tables.runs
    session = func.coalesce(r.c.session_id, literal("run:") + cast(r.c.id, Text))
    per_session = (
        select(func.max(usage_cost(r.c.usage)).label("cost"))
        .join_from(rr, r, r.c.id == rr.c.run_id)
        .where(rr.c.cycle_id == cycle_id)
        .group_by(session)
        .subquery()
    )
    return select(func.coalesce(func.sum(per_session.c.cost), 0)).scalar_subquery()


def _cycle(cycle_id: int):
    """Cycle ``cycle_id`` with its schedule's repo and cap, its project's name and its owner's login."""
    c, s, p, u = tables.release_cycles, tables.release_schedules, tables.projects, tables.users
    return (
        select(
            c,
            s.c.repo,
            s.c.tag_pattern,
            s.c.cycle_budget_usd.label("schedule_cap"),
            p.c.name.label("project"),
            u.c.login.label("owner"),
            cost_of(cycle_id).label("cost"),
        )
        .join_from(c, s, s.c.id == c.c.schedule_id)
        .join(p, p.c.id == c.c.project_id)
        .join(u, u.c.id == c.c.owner_id)
        .where(c.c.id == cycle_id)
    )


async def cycle_of(conn: AsyncConnection, cycle_id: int):
    return (await conn.execute(_cycle(cycle_id))).one()


async def queue_run(conn: AsyncConnection, cycle_id: int, mode: str, *, red_tag: str | None = None) -> int | None:
    """Queue the next run of open cycle ``cycle_id`` in ``mode``, a fix of ``red_tag`` (see the module); its id, or
    None when the cap is spent (the cycle stopped) or another release run of the project is active."""
    found = await cycle_of(conn, cycle_id)
    cap, spent = float(found.budget_usd), float(found.cost)
    budget = releases.run_budget(cap - spent)
    if budget["max_usd"] < curator.MIN_RUN_USD:
        reason = (
            f"its runs cost {curator.money(spent)} of its cap {curator.money(cap)}: what is left is under "
            f"{curator.money(curator.MIN_RUN_USD)}, so no {mode} run is queued"
        )
        await stop_cycle(conn, cycle_id, reason, "budget")
        return None
    if mode == "fix":
        title = releases.fix_title(found.repo, red_tag or "")
    else:
        title = releases.cut_title(found.repo, found.base_tag, found.target_ref, found.target_commit or "")
    values = releases.run_values(
        project_id=found.project_id, owner_id=found.owner_id, repo=found.repo, title=title, budget=budget
    )
    rr = tables.release_runs
    try:
        async with conn.begin_nested():
            run_id = await insert_run(conn, values)
            await conn.execute(insert(rr).values(run_id=run_id, cycle_id=cycle_id, mode=mode, tag=red_tag))
    except IntegrityError:  # runs_active_release_key: another release run of the project is active
        return None
    what = f"fixes {red_tag} forward with the next patch" if mode == "fix" else f"cuts {found.target_ref}"
    text = (
        f"Queued by cycle #{cycle_id} of the release of project {found.project}, as {found.owner}: it {what}, cost "
        f"cap {curator.money(budget['max_usd'])}, what is left of the cycle's {curator.money(cap)}."
    )
    await write_event(conn, run_id, {"text": text, "cycle_id": cycle_id, "release_mode": mode, **budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=found.owner_id,
        token_id=None,
        action=DISPATCH,
        target=f"{found.project} cycle:{cycle_id} run:{run_id} mode={mode}",
        project_id=found.project_id,
    )
    return run_id


# Stopping


async def stop_cycle(conn: AsyncConnection, cycle_id: int, reason: str, cause: str) -> bool:
    """Stop cycle ``cycle_id`` while it is open, keeping ``cause`` and ``reason``, and ask its owner what next;
    whether it stopped now."""
    c = tables.release_cycles
    statement = (
        update(c)
        .values(state="stopped", stop_reason=releases.clip_reason(reason), stop_cause=cause, updated_at=func.now())
        .where(c.c.id == cycle_id, c.c.state == "open")
        .returning(c.c.id)
    )
    stopped = (await conn.execute(statement)).scalar_one_or_none() is not None
    if not stopped:
        return False
    log.warning("release cycle stopped", extra={"cycle_id": cycle_id, "cause": cause})
    try:
        async with conn.begin_nested():
            await ask_owner(conn, cycle_id)
    except Exception as exc:  # the stop holds even when the hub cannot ask
        log.warning("release stop not asked", extra={"cycle_id": cycle_id, "error": f"{type(exc).__name__}: {exc}"})
    return True


def _tag_line(row) -> str:
    where = f" ({row.workflow_url})" if row.workflow_url else ""
    tests = (row.failures or {}).get("ids") or []
    failed = f", failed: {', '.join(tests[:5])}" + (f" and {len(tests) - 5} more" if len(tests) > 5 else "")
    problem = f"; {row.problem}" if row.problem else ""
    return f"- {row.tag} at {releases.short(row.commit)}: CI {row.ci_state}{where}{failed if tests else ''}{problem}"


async def _facts(conn: AsyncConnection, found) -> tuple[list[str], int]:
    """The lines that say where stopped cycle ``found`` stands, and its last run."""
    t, rr, r = tables.release_tags, tables.release_runs, tables.runs
    tags = (await conn.execute(select(t).where(t.c.cycle_id == found.id).order_by(t.c.id))).all()
    items = (
        await conn.execute(
            select(rr.c.run_id, rr.c.mode, rr.c.tag, r.c.state, rr.c.tagged, rr.c.land_state)
            .join_from(rr, r, r.c.id == rr.c.run_id)
            .where(rr.c.cycle_id == found.id)
            .order_by(rr.c.run_id)
        )
    ).all()
    lines = [
        f"Release cycle #{found.id} of {found.project} stopped ({found.stop_cause}): {found.stop_reason}",
        f"Cost: {curator.money(float(found.cost))} of the cycle's cap {curator.money(float(found.budget_usd))}.",
        "Tags it follows:" if tags else f"It made no tag since {found.base_tag or 'the first commit'}.",
        *(_tag_line(row) for row in tags),
        "Runs:",
        *(
            f"- #{item.run_id} {item.mode}{' of ' + item.tag if item.tag else ''}: {item.state}"
            + (f", tagged {item.tagged}" if item.tagged else f", {item.land_state}" if item.land_state else "")
            for item in items
        ),
    ]
    return lines, items[-1].run_id


def _options(cap: float) -> list[dict]:
    return [
        {"key": "continue", "label": f"Go on, with one more cap of {curator.money(cap)}"},
        {"key": "close", "label": "Close the cycle", "recommended": True},
        {"key": "pause", "label": "Pause the release schedule"},
    ]


async def ask_owner(conn: AsyncConnection, cycle_id: int) -> int:
    """Ask the owner of stopped cycle ``cycle_id`` what next (see the module); the decision's id."""
    found = await cycle_of(conn, cycle_id)
    lines, last_run = await _facts(conn, found)
    cap = float(found.schedule_cap)
    question = runs.clip(
        f"Release cycle #{cycle_id} of {found.project} stopped: "
        f"{one_line(found.stop_reason or '', QUESTION_REASON_CHARS)} Go on with one more cap of {curator.money(cap)}, "
        "close the cycle, or pause the release schedule?",
        runs.MAX_QUESTION_CHARS,
    )
    d = tables.decisions
    decision_id = (
        await conn.execute(
            insert(d)
            .values(
                run_id=last_run,
                project_id=found.project_id,
                category=runs.RELEASE_DECISION_CATEGORY,
                question=question,
                context=runs.clip("\n".join(lines), runs.MAX_DECISION_CONTEXT_BYTES),
                options=_options(cap),
                recommended_reason=CLOSE_REASON,
            )
            .returning(d.c.id)
        )
    ).scalar_one()
    await notify(
        conn,
        user_id=found.owner_id,
        kind="decision",
        project_id=found.project_id,
        run_id=last_run,
        decision_id=decision_id,
        title=f"Release cycle #{cycle_id} of {found.project} stopped: {one_line(found.stop_reason or '')}",
        body=f"{question}\n\n" + "\n".join(lines[1:]),
        link=decision_link(decision_id),
    )
    asked = (
        f"{found.owner} decides in decision #{decision_id} ({decision_link(decision_id)}): go on with one more cap of "
        f"{curator.money(cap)}, close the cycle, or pause the release schedule. Nothing goes on until then."
    )
    thread = await triage.project_thread(
        conn, found.project_id, found.project, found.owner_id, "\n".join([*lines, asked])
    )
    c = tables.release_cycles
    await conn.execute(
        update(c)
        .where(c.c.id == cycle_id)
        .values(
            decision_id=decision_id,
            triage_chat_id=thread.chat_id if thread else None,
            triage_root_id=thread.root_id if thread else None,
        )
    )
    log.info("release stop asked", extra={"cycle_id": cycle_id, "decision_id": decision_id})
    return decision_id


# Notices


async def notify_release(
    conn: AsyncConnection, found, *, event: str, title: str, body: str, run_id: int | None, details: dict
) -> int:
    """Send the owner of cycle ``found`` (``cycle_of``) a notice of kind release about ``event``."""
    return await notify(
        conn,
        user_id=found.owner_id,
        kind="notice",
        notice_kind="release",
        project_id=found.project_id,
        run_id=run_id,
        title=title,
        body=body,
        details={"event": event, "cycle_id": found.id, **details},
        link=releases_link(found.project),
    )
