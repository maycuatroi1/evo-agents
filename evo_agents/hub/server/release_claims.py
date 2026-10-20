"""A release run between its claim and its end (``evo_agents.hub.release_runs``): what its claim hands the worker,
the branch the worker reports it pushed, and what its end does to its cycle.

``release_claim`` is the claim's ``release`` (``runs.models.ReleaseSpec``): the cycle and mode, the schedule's repo,
the commit the worktree starts at (a cut's target commit; a fix's red tag's commit, the merge commit the hub tagged,
else the cycle's base commit when the red tag is its base tag) and the tags' prefix. ``release_prompt`` is its prompt,
built from what the cycle keeps (its range, ``span``), so a claim reads nothing of GitHub.

POST /v1/worker/runs/{id}/release/branch: the worker that holds a release run pushed release/<version> of the
schedule's repo; the hub keeps the version, the branch, the commit and the agent's Decision line on the run's row of
release_runs, and lands it once the run is done (``evo_agents.hub.server.release_land``). 404 for a run this worker
does not hold or that is no release run, 422 for another repo or a branch that is not release/<version>, 409 once the
hub landed it.

``release_run_moved`` is what a release run's move means for its cycle (``run_state.record_move``): one that fails, or
is cancelled while its schedule is not paused, stops the open cycle with the reason (a pause closes it instead); one
that ends done without a branch stops it too, its row refused; one done with its branch waits for the landing.
``stop_cycle`` is the one way a cycle stops.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import release_runs, releases, runs, tables
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import NOT_HELD, OBJECT_NAME, ReleaseSpec, Run, RunId
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}


class ReleasePushed(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    branch: str = Field(min_length=1, max_length=255, description="release/<version>")
    head: str = Field(pattern=OBJECT_NAME, description="the commit the branch has now")
    version: str = Field(pattern=releases.VERSION.pattern, description="the version the agent picked, X.Y.Z")
    decision: str | None = Field(None, max_length=releases.MAX_REASON_CHARS, description="the agent's Decision line")


class ReleaseBranch(BaseModel):
    run_id: int
    cycle_id: int
    repo: str
    branch: str
    head: str
    version: str


def _release_run(run_id: int):
    """Run ``run_id``'s row of release_runs with its cycle, schedule and project."""
    rr, c, s, p = tables.release_runs, tables.release_cycles, tables.release_schedules, tables.projects
    return (
        select(
            rr.c.run_id,
            rr.c.cycle_id,
            rr.c.mode,
            rr.c.tag.label("red_tag"),
            rr.c.land_state,
            c.c.state.label("cycle_state"),
            c.c.base_tag,
            c.c.base_commit,
            c.c.target_ref,
            c.c.target_commit,
            c.c.span,
            s.c.repo,
            s.c.tag_pattern,
            s.c.paused_at,
            p.c.name.label("project"),
        )
        .join_from(rr, c, c.c.id == rr.c.cycle_id)
        .join(s, s.c.id == c.c.schedule_id)
        .join(p, p.c.id == c.c.project_id)
        .where(rr.c.run_id == run_id)
    )


async def _found(conn: AsyncConnection, run_id: int):
    return (await conn.execute(_release_run(run_id))).one_or_none()


async def _fix_start(conn: AsyncConnection, found) -> str | None:
    """The commit a fix run of ``found`` starts at: the merge commit the hub tagged its red tag on, else the cycle's
    base commit when the red tag is the cycle's base tag."""
    rr = tables.release_runs
    query = select(rr.c.merge_commit).where(rr.c.cycle_id == found.cycle_id, rr.c.tagged == found.red_tag)
    tagged = (await conn.execute(query.limit(1))).scalar_one_or_none()
    if tagged:
        return tagged
    return found.base_commit if found.red_tag == found.base_tag else None


async def release_claim(conn: AsyncConnection, run_id: int) -> ReleaseSpec | None:
    """The ``release`` of the claim of release run ``run_id``; None for a run with no row of release_runs."""
    found = await _found(conn, run_id)
    if found is None:
        return None
    start = found.target_commit if found.mode == "cut" else await _fix_start(conn, found)
    return ReleaseSpec(
        cycle_id=found.cycle_id,
        mode=found.mode,
        repo=found.repo,
        start_commit=start or found.target_commit or "",
        base_tag=found.base_tag,
        tag_prefix=releases.tag_prefix(found.tag_pattern),
        red_tag=found.red_tag,
    )


async def release_prompt(conn: AsyncConnection, view: Run) -> str:
    """The prompt of release run ``view`` at its claim (``release_runs.cut_prompt``, ``release_runs.fix_prompt``)."""
    found = await _found(conn, view.id)
    if found is None:
        return f"Release run #{view.id} belongs to no cycle of the release: end the run without changing anything.\n"
    prefix = releases.tag_prefix(found.tag_pattern)
    common = {"project": found.project, "run_id": view.id, "cycle_id": found.cycle_id, "repo": found.repo}
    if found.mode == "fix":
        start = await _fix_start(conn, found) or found.target_commit or ""
        prompt = release_runs.fix_prompt(**common, red_tag=found.red_tag or "", start_commit=start, tag_prefix=prefix)
    else:
        prompt = release_runs.cut_prompt(
            **common,
            target_ref=found.target_ref,
            target_commit=found.target_commit or "",
            base_tag=found.base_tag,
            base_commit=found.base_commit,
            tag_prefix=prefix,
            span=found.span,
        )
    return runs.clip(prompt, runs.MAX_PROMPT_BYTES)


# The branch the worker pushed


async def _held_release(conn: AsyncConnection, user, run_id: int):
    """The row of release run ``run_id`` that this worker holds, locked; 404 otherwise."""
    worker_id = (await worker_of(conn, user))[0]
    r = tables.runs
    row = (
        await conn.execute(select(r.c.worker_id, r.c.state, r.c.kind).where(r.c.id == run_id).with_for_update())
    ).one_or_none()
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind != releases.RELEASE_KIND:
        raise HTTPException(404, f"run {run_id} is a {row.kind} run: only a release run reports a release branch")
    found = await _found(conn, run_id)
    if found is None:
        raise HTTPException(404, f"run {run_id} belongs to no cycle of the release")
    return found


@worker_router.post("/runs/{run_id}/release/branch", status_code=201, response_model=ReleaseBranch, responses=REFUSALS)
async def pushed(request: Request, run_id: RunId, body: ReleasePushed, user: CurrentUser) -> ReleaseBranch:
    """The worker pushed release/<version> of the release run's repo: kept on the run, for the hub to land once the
    run is done."""
    async with request.app.state.engine.begin() as conn:
        found = await _held_release(conn, user, run_id)
        if body.repo != found.repo:
            raise HTTPException(422, f"run {run_id} releases {found.repo}, not {body.repo}")
        if body.branch != release_runs.branch_of(body.version):
            raise HTTPException(
                422, f"run {run_id} pushes {release_runs.branch_of(body.version)} for {body.version}, not {body.branch}"
            )
        if found.land_state is not None:
            raise HTTPException(409, f"the hub landed run {run_id} already ({found.land_state})")
        decision = release_runs.one_line(body.decision, releases.MAX_REASON_CHARS) or None
        rr = tables.release_runs
        await conn.execute(
            update(rr)
            .where(rr.c.run_id == run_id)
            .values(version=body.version, branch=body.branch, head_commit=body.head, decision=decision)
        )
        text = (
            f"The worker pushed {body.branch} of {body.repo} at {releases.short(body.head)}: the hub opens its pull "
            "request, merges it and tags the merge commit once the run is done."
        )
        await write_event(conn, run_id, {"text": text, "release_version": body.version, "decision": decision})
    log.info("release branch pushed", extra={"run_id": run_id, "version": body.version, "head": body.head})
    return ReleaseBranch(
        run_id=run_id,
        cycle_id=found.cycle_id,
        repo=body.repo,
        branch=body.branch,
        head=body.head,
        version=body.version,
    )


# What a run's end does to its cycle


async def stop_cycle(conn: AsyncConnection, cycle_id: int, reason: str) -> bool:
    """Stop cycle ``cycle_id`` while it is open, keeping ``reason``; whether it stopped now."""
    c = tables.release_cycles
    statement = (
        update(c)
        .values(state="stopped", stop_reason=releases.clip_reason(reason), updated_at=func.now())
        .where(c.c.id == cycle_id, c.c.state == "open")
        .returning(c.c.id)
    )
    stopped = (await conn.execute(statement)).scalar_one_or_none() is not None
    if stopped:
        log.warning("release cycle stopped", extra={"cycle_id": cycle_id, "reason": releases.clip_reason(reason)})
    return stopped


async def release_run_moved(conn: AsyncConnection, run_id: int, new: str, *, reason: str) -> None:
    """What release run ``run_id``'s move to ``new`` means for its cycle (see the module)."""
    if new not in ("done", "failed", "cancelled"):
        return
    found = await _found(conn, run_id)
    if found is None:
        return
    if new == "cancelled":
        if found.paused_at is None:  # a pause closes the cycle itself
            await stop_cycle(conn, found.cycle_id, f"release run #{run_id} was cancelled: {reason}")
        return
    if new == "failed":
        await stop_cycle(conn, found.cycle_id, f"release run #{run_id} failed: {reason}")
        return
    rr = tables.release_runs
    branch = (await conn.execute(select(rr.c.branch).where(rr.c.run_id == run_id))).scalar_one()
    if branch is None:
        why = (
            f"release run #{run_id} ended done without pushing a branch {release_runs.BRANCH_PREFIX}<version>: "
            "nothing to land"
        )
        await conn.execute(update(rr).where(rr.c.run_id == run_id).values(land_state="refused", land_reason=why))
        await stop_cycle(conn, found.cycle_id, why)
        return
    await write_event(conn, run_id, {"text": f"The hub lands {branch} on its next pass of the release job."})
