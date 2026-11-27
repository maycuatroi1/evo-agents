"""A release run between its claim and its end (``evo_agents.hub.release_runs``): what its claim hands the worker,
the branch the worker reports it pushed, and what its end does to its cycle.

``release_claim`` is the claim's ``release`` (``runs.models.ReleaseSpec``): the cycle and mode, the schedule's repo,
the commit the worktree starts at (a cut's target commit; a fix's red tag's commit as the cycle follows it in
release_tags, else the merge commit the hub tagged, else the cycle's base commit when the red tag is its base tag) and
the tags' prefix. ``release_prompt`` is its prompt, built from what the cycle keeps (its range, ``span``, and for a fix
what the hub read of the red tag's CI, ``release_ci.failures_text``), so a claim reads nothing of GitHub.

POST /v1/worker/runs/{id}/release/branch: the worker that holds a release run pushed release/<version> of the
schedule's repo; the hub keeps the version, the branch, the commit and the agent's Decision line on the run's row of
release_runs, and lands it once the run is done (``evo_agents.hub.server.releases.service.land``). 404 for a run this worker
does not hold or that is no release run, 422 for another repo or a branch that is not release/<version>, 409 once the
hub landed it.

``release_run_moved`` is what a release run's move means for its cycle (``run_state.record_move``): one that fails, or
is cancelled while its schedule is not paused, stops the open cycle with the reason (a pause closes it instead); one
that ends done without a branch stops it too, its row refused; one done with its branch waits for the landing.
``service.cycles.stop_cycle`` is the one way a cycle stops, and asks the owner what next.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import release_ci, release_runs, releases, runs
from evo_agents.hub.server.releases.models.claims import ReleaseBranch, ReleasePushed
from evo_agents.hub.server.releases.repository import runs as runs_repo
from evo_agents.hub.server.releases.repository import tags as tags_repo
from evo_agents.hub.server.releases.service.cycles import stop_cycle
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import NOT_HELD, ReleaseSpec, Run
from evo_agents.hub.server.runs.service.claims import worker_of

log = logging.getLogger(__name__)


async def _fix_start(conn: AsyncConnection, found) -> str | None:
    """The commit a fix run of ``found`` starts at: its red tag's commit as the cycle follows it, else the merge
    commit the hub tagged it on, else the cycle's base commit when the red tag is the cycle's base tag."""
    followed = await tags_repo.tag_of(conn, found.cycle_id, found.red_tag)
    if followed is not None:
        return followed.commit
    tagged = await runs_repo.tagged_merge(conn, found.cycle_id, found.red_tag)
    if tagged:
        return tagged
    return found.base_commit if found.red_tag == found.base_tag else None


async def release_claim(conn: AsyncConnection, run_id: int) -> ReleaseSpec | None:
    """The ``release`` of the claim of release run ``run_id``; None for a run with no row of release_runs."""
    found = await runs_repo.release_run(conn, run_id)
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
    found = await runs_repo.release_run(conn, view.id)
    if found is None:
        return f"Release run #{view.id} belongs to no cycle of the release: end the run without changing anything.\n"
    prefix = releases.tag_prefix(found.tag_pattern)
    common = {"project": found.project, "run_id": view.id, "cycle_id": found.cycle_id, "repo": found.repo}
    if found.mode == "fix":
        start = await _fix_start(conn, found) or found.target_commit or ""
        red = await tags_repo.tag_of(conn, found.cycle_id, found.red_tag)
        failures = release_ci.failures_text(found.red_tag or "", red.workflow_url, red.failures) if red else None
        prompt = release_runs.fix_prompt(
            **common, red_tag=found.red_tag or "", start_commit=start, tag_prefix=prefix, failures=failures
        )
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


async def held_release(conn: AsyncConnection, user, run_id: int):
    """The row of release run ``run_id`` that this worker holds, locked; 404 otherwise."""
    worker_id = (await worker_of(conn, user))[0]
    row = await runs_repo.locked_run(conn, run_id)
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind != releases.RELEASE_KIND:
        raise HTTPException(404, f"run {run_id} is a {row.kind} run: only a release run reports a release branch")
    found = await runs_repo.release_run(conn, run_id)
    if found is None:
        raise HTTPException(404, f"run {run_id} belongs to no cycle of the release")
    return found


async def push(engine: AsyncEngine, user, run_id: int, body: ReleasePushed) -> ReleaseBranch:
    """Keep the branch ``body`` the worker of release run ``run_id`` pushed (see the module)."""
    async with engine.begin() as conn:
        found = await held_release(conn, user, run_id)
        if body.repo != found.repo:
            raise HTTPException(422, f"run {run_id} releases {found.repo}, not {body.repo}")
        if body.branch != release_runs.branch_of(body.version):
            raise HTTPException(
                422, f"run {run_id} pushes {release_runs.branch_of(body.version)} for {body.version}, not {body.branch}"
            )
        if found.land_state is not None:
            raise HTTPException(409, f"the hub landed run {run_id} already ({found.land_state})")
        decision = release_runs.one_line(body.decision, releases.MAX_REASON_CHARS) or None
        await runs_repo.save_release_run(
            conn, run_id, version=body.version, branch=body.branch, head_commit=body.head, decision=decision
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


async def release_run_moved(conn: AsyncConnection, run_id: int, new: str, *, reason: str) -> None:
    """What release run ``run_id``'s move to ``new`` means for its cycle (see the module)."""
    if new not in ("done", "failed", "cancelled"):
        return
    found = await runs_repo.release_run(conn, run_id)
    if found is None:
        return
    if new == "cancelled":
        if found.paused_at is None:  # a pause closes the cycle itself
            await stop_cycle(conn, found.cycle_id, f"release run #{run_id} was cancelled: {reason}", "run_cancelled")
        return
    if new == "failed":
        await stop_cycle(conn, found.cycle_id, f"release run #{run_id} failed: {reason}", "run_failed")
        return
    branch = await runs_repo.branch_of(conn, run_id)
    if branch is None:
        why = (
            f"release run #{run_id} ended done without pushing a branch {release_runs.BRANCH_PREFIX}<version>: "
            "nothing to land"
        )
        await runs_repo.save_release_run(conn, run_id, land_state="refused", land_reason=why)
        await stop_cycle(conn, found.cycle_id, why, "refused")
        return
    await write_event(conn, run_id, {"text": f"The hub lands {branch} on its next pass of the release job."})
