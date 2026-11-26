"""What the end of a run says to its owner: the triage thread a failed run opens (``triage``) and the notices
``run_failed`` and ``plan_finished`` (``notifications.notify``), which ``run_state.record_move`` asks for.

A plan run that ends ``failed`` sends the notice ``run_failed``, and one that ends ``done`` with every step of its plan
done the notice ``plan_finished``; one that ends ``done`` because a new run resumes it sends nothing. A run of one step
or an author run sends ``run_failed`` when its worker failed it with a cause of ``runs.PREFLIGHT_CAUSES`` (a repo the
project lists no origin for, a credential or a program missing), which only the owner can mend. When the failure opened
a triage thread, the notice links to the thread; the notices sent are the same. With no thread (the project's triage
off, the chats feature off, a thread that could not be opened) the notice is the one a failure always had.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import notifications, triage
from evo_agents.hub.server.run_state import RunStep, reported_steps


async def run_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> int | None:
    """Open the thread of run ``found`` when it failed, then send its owner the notice its end calls for; the
    notification's id, or None."""
    thread = await triage.on_failed(conn, found) if new == "failed" else None
    link = thread.link if thread is not None else None
    if found.kind == "plan":
        return await notify_plan_run_end(conn, found, old, new, reason=reason, link=link)
    return await notify_preflight_failure(conn, found, new, link=link)


async def notify_preflight_failure(
    conn: AsyncConnection, found: RunStep, new: str, *, link: str | None = None
) -> int | None:
    """Send the owner of run ``found``, a run of one step or an author run, the notice ``run_failed`` when it failed
    with a cause of runs.PREFLIGHT_CAUSES (a repo without origin, a credential or a program missing), which only the
    owner can mend; a plan, review or judge run sends it for every failure elsewhere. It links to ``link``, the triage
    thread of the run, when given, else to the run's page. The notification's id, or None."""
    if new != "failed" or found.failure_cause not in runs.PREFLIGHT_CAUSES:
        return None
    error = found.error or found.failure_cause
    what = f"Run #{found.run_id} of step {found.step_key} of {found.plan_id}"
    if found.kind == "author":
        what = f"Author run #{found.run_id}"
    return await notifications.notify(
        conn,
        user_id=found.dispatcher_id,
        kind="notice",
        notice_kind="run_failed",
        project_id=found.project_id,
        run_id=found.run_id,
        title=f"{what} failed ({found.failure_cause})",
        body=error,
        details={
            "run_kind": found.kind,
            "plan_id": found.plan_id,
            "step_key": found.step_key,
            "failure_cause": found.failure_cause,
            "error": error,
        },
        link=link or notifications.run_link(found.project, found.run_id),
    )


def _every_step_done(body) -> bool:
    steps = body.get("steps") if isinstance(body, dict) else None
    if not isinstance(steps, list) or not steps:
        return False
    return all(isinstance(step, dict) and step.get("status") == "done" for step in steps)


async def notify_plan_run_end(
    conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str, link: str | None = None
) -> int | None:
    """Send the owner of plan run ``found`` the notice its end calls for: ``run_failed`` when it failed (its last
    attempt lost included, which fails), ``plan_finished`` when it ended done with every step of its plan done. A run
    done because a new run resumes it, and any other move, send nothing. The notification's id, or None."""
    link = link or notifications.run_link(found.project, found.run_id)
    if new == "failed":
        error = found.error or reason
        return await notifications.notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=found.project_id,
            run_id=found.run_id,
            title=f"Plan run #{found.run_id} of {found.plan_id} failed",
            body=error,
            details={"plan_id": found.plan_id, "error": error},
            link=link,
        )
    if new != "done" or old == "parked":
        return None
    pl = tables.plans
    body = (
        await conn.execute(select(pl.c.body).where(pl.c.project_id == found.project_id, pl.c.plan_id == found.plan_id))
    ).scalar_one_or_none()
    if body is None or not _every_step_done(body):
        return None
    steps = await reported_steps(conn, found.run_id)
    done = f"steps {', '.join(steps)}" if steps else "no step of its own"
    return await notifications.notify(
        conn,
        user_id=found.dispatcher_id,
        kind="notice",
        notice_kind="plan_finished",
        project_id=found.project_id,
        run_id=found.run_id,
        title=f"Plan {found.plan_id} finished",
        body=f"Plan run #{found.run_id} ended with every step of the plan done; it did {done}.",
        details={"plan_id": found.plan_id, "steps": steps},
        link=link,
    )
