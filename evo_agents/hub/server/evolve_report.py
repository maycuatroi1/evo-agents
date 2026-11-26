"""The end of an evolution of the Curator (POST /v1/worker/runs/{id}/evolve/report, ``evo_agents.hub.server.evolve``).

An evolution of target git ends with the branch its run pushed: when the evolution names an accepted proposal whose
change is still planned, on that change's repo and branch, the change goes on as a Builder's would
(``change_shift.builder_ended``): it waits for its pull request (GitHub) or its Judge (GitLab), and the hub's Curator
change flow takes it from there. Without one the branch stays as it is and the evolution says why; the hub makes no
change without an accepted proposal.

An evolution of target agent_config ends with its best candidate, which becomes the agent profile's next version
(``system_curation.apply_evolved``) only when it wins on the holdout split, or regresses on none of its tasks, and the
profile still holds what the evolution started from. The worker reports those holdout scores in ``holdout``; without
them nothing is pinned, and the evolution says why.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import replay, tables
from evo_agents.hub.server import system_curation
from evo_agents.hub.server.changes.service.moves import set_change
from evo_agents.hub.server.evolve_models import ReportIn
from evo_agents.hub.server.evolve_runs import reason_text, set_row
from evo_agents.hub.server.projects import access_of
from evo_agents.hub.server.security import Principal

HOLDOUT = replay.SPLITS[1]


async def _attach(conn: AsyncConnection, row, run_id: int, body: ReportIn) -> tuple[int | None, str]:
    """(the change the git evolution's branch goes on as, what happened) or (None, why it waits)."""
    if not body.branch or not body.head_sha:
        return None, "the run pushed no branch: nothing goes on"
    pushed = f"branch {body.branch} at {body.head_sha[:12]}"
    if row.proposal_id is None:
        return None, f"{pushed} waits: the evolution names no accepted proposal whose planned change takes it"
    c = tables.curator_changes
    query = select(c).where(c.c.proposal_id == row.proposal_id, c.c.project_id == row.project_id).with_for_update()
    change = (await conn.execute(query)).one_or_none()
    if change is None:
        return None, f"{pushed} waits: proposal #{row.proposal_id} has no change of the Curator"
    if change.state != "planned":
        return None, f"{pushed} waits: change #{change.id} of proposal #{row.proposal_id} is {change.state}"
    if change.repo != row.repo or change.branch != body.branch:
        return None, f"{pushed} waits: change #{change.id} takes branch {change.branch} of {change.repo}"
    state = "pr_pending" if change.forge == "github" else "judge_pending"
    await set_change(conn, change.id, state=state, builder_run_id=run_id, head_sha=body.head_sha, reason=None)
    what = (
        "the hub opens its pull request" if state == "pr_pending" else "its merge request is open; its Judge comes next"
    )
    return change.id, f"{pushed} is change #{change.id} of proposal #{row.proposal_id}: {what}"


async def _holdout_refs(conn: AsyncConnection, project_id: int, holdout: dict) -> None:
    """422 unless each ref of ``holdout`` is a replay task of the project's holdout split."""
    t = tables.replay_tasks
    ids = [int(ref.split(":", 1)[1]) for ref in holdout if ref.startswith("replay:")]
    query = select(t.c.id).where(t.c.project_id == project_id, t.c.split == HOLDOUT, t.c.id.in_(ids))
    known = {f"replay:{task_id}" for task_id in (await conn.scalars(query)).all()}
    unknown = sorted(set(holdout) - known)
    if unknown:
        raise HTTPException(422, f"{', '.join(unknown[:5])}: no replay task of the project's holdout split")


async def _components(conn: AsyncConnection, row, *, origin: str | None = None, candidate_id: int | None = None):
    ec = tables.curator_evolve_candidates
    query = select(ec.c.components).where(ec.c.evolve_id == row.id)
    query = query.where(ec.c.origin == origin) if origin else query.where(ec.c.id == candidate_id)
    return (await conn.execute(query.order_by(ec.c.id).limit(1))).scalar_one_or_none()


async def _pin(conn: AsyncConnection, user: Principal, row, best: dict, body: ReportIn) -> str:
    """Pin the agent_config evolution's best candidate when it holds on the holdout split; what happened, or why not."""
    holdout = {ref: pair.model_dump() for ref, pair in (body.holdout or {}).items()}
    await _holdout_refs(conn, row.project_id, holdout)
    held_up, why = system_curation.holdout_verdict(holdout)
    if not held_up:
        return why
    seed = await _components(conn, row, origin="seed")
    if seed is None:
        return "the evolution has no seed candidate to compare the profile with: the profile keeps its version"
    p = tables.projects
    name = await conn.scalar(select(p.c.name).where(p.c.id == row.project_id))
    access = None if name is None else await access_of(conn, user.user_id, name)
    if access is None:
        return "the evolution's project is gone: the profile keeps its version"
    return (await system_curation.apply_evolved(conn, access, user, row, seed, best, holdout))[1]


async def ended(conn: AsyncConnection, user: Principal, row, run_id: int, body: ReportIn) -> str:
    """End evolution ``row`` (locked, running) as its run reports; a line for the run's events."""
    if body.state == "failed":
        why = body.reason or "its run gave no reason"
        await set_row(conn, row.id, state="failed", evolve_key=None, reason=reason_text(why))
        return f"Evolution #{row.id} failed: {why}."
    best = await _components(conn, row, candidate_id=body.best_candidate_id)
    if best is None:
        raise HTTPException(422, f"candidate #{body.best_candidate_id} is no candidate of evolution #{row.id}")
    change_id = None
    if row.target == "git":
        change_id, why = await _attach(conn, row, run_id, body)
    else:
        why = await _pin(conn, user, row, best, body)
    values = {"state": "done", "evolve_key": None, "best_candidate_id": body.best_candidate_id, "change_id": change_id}
    await set_row(conn, row.id, **values, reason=reason_text(why))
    return f"Evolution #{row.id} is done, best candidate #{body.best_candidate_id}: {why}."
