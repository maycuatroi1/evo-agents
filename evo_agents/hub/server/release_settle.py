"""What the hub does with a release branch a refused landing left behind (``evo_agents.hub.release_runs``), so that a
fix run of the same red tag, which picks the same version, never finds release/<version> taken on origin: it would
push onto it, GitHub would refuse a push that is no fast-forward, and the run would fail ``push_conflict``.

``settle_releases`` is one pass, made by the job ``hub.release_schedules`` before the landing: each release run that
the hub refused (or failed to land), that made no tag and whose cycle is open or stopped, is looked at on GitHub once
a pass, with one token of the App for the repo alone (``pulls.with_token``, use ``merge``):

- **A person merged its pull request** into the default branch, whose head is the commit the run pushed
  (``release_land.pull_problem``): the hub adopts the merge, as ``release_land.land`` would have: the lightweight tag
  ``<prefix><version>`` on the merge commit (or the tag found there already), the run kept ``tagged`` with its merge
  commit, a row of release_tags for the cycle to follow, audited as ``release.tag``. A person who merges a refused pull
  request decides what the hub would not: the test-hacking guard of the hub's own merge is not applied again. A tag
  on another commit is left alone, and a cycle stopped stays so: its owner's ``continue`` follows the tag's CI.
- **Nobody merged it** and the cycle is open again (its owner chose ``continue``): its open pull requests are closed
  and its branch deleted, when the branch still holds the commit the run pushed, so the new fix run pushes its own
  release/<version> to a free name. A stopped cycle keeps its branch and pull request for the person to read; a
  branch that moved is never touched. The hub never forces a push, never touches the default branch and never makes a
  tag but on a merge commit of a pull request of the run's own branch.

The owner's ``continue`` queues no run while such a branch holds the cycle (``release_cycles.held_by``): a person may
merge the refused pull request as they answer, and a fix run queued then pushes the same release/<version> and fails
push_conflict (run #12580). Once the branch is free (cleared, or found gone), this pass makes the cycle's next move
(``release_cycles.go_on``); a branch that a person moved stops the cycle again with what to do (``_after``).

A pass that GitHub failed leaves the run for the next one; one run failing leaves the others their turn.
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import releases, tables
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import autonomy_gates, pulls
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.release_cycles import cycle_of, go_on, held_by, stop_cycle
from evo_agents.hub.server.release_land import (
    Landing,
    Refusal,
    default_branch,
    ensure_tag,
    landing_rows,
    mark_tagged,
    owner_is_admin,
    pull_problem,
    run_event,
    save_run,
)
from evo_agents.hub.tables import RUN_ACTIVE

log = logging.getLogger(__name__)


def _left():
    """The release runs a landing left behind: refused or failed, no tag, of a cycle open or stopped, whose branch no
    other run has taken since: one that waits to land it or landed it."""
    rr, other = tables.release_runs, tables.release_runs.alias("other")
    c = tables.release_cycles
    taken = exists().where(
        other.c.branch == rr.c.branch,
        other.c.run_id != rr.c.run_id,
        other.c.land_state.is_(None) | (other.c.land_state == "tagged"),
    )
    return landing_rows(
        rr.c.land_state.in_(("refused", "failed")), rr.c.tagged.is_(None), c.c.state.in_(("open", "stopped")), ~taken
    )


async def _adopt(engine: AsyncEngine, app: GitHubApp, token: str, where: tuple[str, str], row: Landing, merged: dict):
    """Tag the merge commit of ``merged``, the pull request a person merged, and keep it; ``tagged``."""
    merge = merged["merge_commit_sha"]
    await save_run(engine, row.run_id, pull_number=merged["number"])
    await ensure_tag(app, token, where, row.tag, merge)
    await mark_tagged(engine, row, merge, by_hand=True)
    return "adopted"


async def _clear(engine: AsyncEngine, app: GitHubApp, token: str, where: tuple[str, str], row: Landing, found: list):
    """Close the open pull requests of the run's branch and delete the branch, while it holds the commit the run
    pushed; ``cleared`` when something was done, else ``clean``."""
    try:
        head = (await pulls.branch_head(app, token, *where, row.branch))["sha"]
    except pulls.Refused as exc:
        if exc.status == 404:
            return "clean"
        raise
    if head != row.head_commit:
        return "kept"
    default = await default_branch(app, token, where)
    closed = []
    for item in found:
        if item.get("state") == "open" and pull_problem(item, row, default) is None:
            await pulls.update_pull(app, token, *where, item["number"], state="closed")
            closed.append(item["number"])
    deleted = await pulls.delete_branch(app, token, *where, row.branch)
    if not closed and not deleted:
        return "clean"
    text = (
        f"The hub cleared {row.branch} of {row.repo} (the commit {releases.short(row.head_commit)} release run "
        f"#{row.run_id} pushed, refused): "
        + (f"pull request {', '.join(f'#{n}' for n in closed)} closed, " if closed else "")
        + "branch deleted, so a fix run of the same version pushes its own."
    )
    async with engine.begin() as conn:
        await run_event(conn, row.run_id, {"text": text, "release_branch": "cleared"})
    log.info("release branch cleared", extra={"run_id": row.run_id, "branch": row.branch, "closed": closed})
    return "cleared"


async def _settle(engine: AsyncEngine, app: GitHubApp, token: str, where: tuple[str, str], row: Landing) -> str:
    default = await default_branch(app, token, where)
    found = await pulls.branch_pulls(app, token, *where, row.branch)
    for item in found:
        if item.get("merged_at") is None or pull_problem(item, row, default) is not None:
            continue
        merged = await pulls.pull(app, token, *where, item["number"])
        if merged.get("merged") is True and isinstance(merged.get("merge_commit_sha"), str):
            what = f"tag {row.tag} of {row.repo} on the merge commit of {row.branch}, merged by a person"
            async with engine.begin() as conn:
                said = await autonomy_gates.check(
                    conn, row.run_id, "K6", key=f"release {row.tag}", what=what, repo=row.repo
                )
            return await _adopt(engine, app, token, where, row, merged) if said.act else "held"
    return await _clear(engine, app, token, where, row, found) if row.cycle_state == "open" else "kept"


async def settle(engine: AsyncEngine, app: GitHubApp | None, row: Landing) -> str:
    """Settle the branch of release run ``row`` (see the module): adopted, cleared, clean, kept, held (the principal
    is asked first), or skipped when the hub cannot reach GitHub for it."""
    where = github_repo(row.origin) if row.origin else None
    if app is None or where is None:
        return "skipped"
    async with engine.connect() as conn:
        if not await owner_is_admin(conn, row.owner_id, row.owner, row.project):
            return "skipped"
    try:
        found = await pulls.with_token(app, *where, "merge", lambda token: _settle(engine, app, token, where, row))
    except (Refusal, GitHubUnavailable, GitHubRefused) as exc:
        log.warning("release branch not settled", extra={"run_id": row.run_id, "why": str(exc)})
        return "skipped"
    if found in ("cleared", "clean", "kept") and row.cycle_state == "open":
        await _after(engine, row, found)
    return found


async def _after(engine: AsyncEngine, row: Landing, found: str) -> None:
    """Once ``row``'s branch is settled (``found``) and still holds its open cycle's next move (``held_by``): that move
    made when the branch is free, or the cycle stopped when a person moved the branch."""
    c, rr, r = tables.release_cycles, tables.release_runs, tables.runs
    async with engine.begin() as conn:
        state = (await conn.execute(select(c.c.state).where(c.c.id == row.cycle_id).with_for_update())).scalar_one()
        if state != "open":
            return
        cycle = await cycle_of(conn, row.cycle_id)
        left = await held_by(conn, cycle)
        if left is None or left.run_id != row.run_id:
            return
        if found == "kept":
            reason = (
                f"{row.branch} of {row.repo} moved since release run #{row.run_id} pushed it, so the next run cannot "
                "push it: merge its pull request (the hub tags the merge) or delete the branch, then continue"
            )
            await stop_cycle(conn, row.cycle_id, reason, "refused")
            return
        active = exists().where(rr.c.cycle_id == row.cycle_id, r.c.id == rr.c.run_id, r.c.state.in_(RUN_ACTIVE))
        if (await conn.execute(select(active))).scalar_one():
            return
        said = await go_on(conn, cycle)
        await run_event(conn, row.run_id, {"text": f"{row.branch} of {row.repo} is free again: {said}."})
    log.info("release cycle goes on after settling", extra={"run_id": row.run_id, "said": said})


async def settle_releases(engine: AsyncEngine, app: GitHubApp | None) -> dict:
    """One pass of settling: each release run a landing left behind gets ``settle``, one failing leaving the others
    their turn; how many ended each way."""
    async with engine.connect() as conn:
        rows = [Landing(**row._mapping) for row in (await conn.execute(_left())).all()]
    outcomes: Counter = Counter()
    for row in rows:
        try:
            outcomes[await settle(engine, app, row)] += 1
        except Exception:
            log.exception("a release settling failed", extra={"run_id": row.run_id})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))
