"""What the job ``curator.changes`` reads again of the changes it no longer moves on itself
(``evo_agents.hub.server.changes.service.job`` describes the design): every WATCH_EVERY, a pull request left open for its owner,
and the branch of a planned change whose plan has no step ready; merged or closed by hand, the change settles so."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import judge
from evo_agents.hub.server.changes.repository import watch as store
from evo_agents.hub.server.changes.repository.views import change_of_plan
from evo_agents.hub.server.changes.service.moves import set_change
from evo_agents.hub.server.changes.service.shift import unready_steps
from evo_agents.hub.server.forge.client.github_app import GitHubApp
from evo_agents.hub.server.forge.models.forges import github_target
from evo_agents.hub.server.forge.models.github import GitHubRefused, GitHubUnavailable, Target

log = logging.getLogger(__name__)

WATCH_EVERY = timedelta(hours=1)  # how often the job reads a pull request left open for its owner
MAX_WATCH = 20  # pull requests left open one pass of the job reads


async def _left_open(engine: AsyncEngine) -> list:
    """The changes left open for their owner with a pull request on GitHub, not read for WATCH_EVERY."""
    async with engine.begin() as conn:
        return await store.left_open(conn, WATCH_EVERY, MAX_WATCH)


def _github_time(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")) if text else None
    except ValueError:
        return None


async def _watch(engine: AsyncEngine, app: GitHubApp, change) -> str:
    """Read a pull request left open for its owner: merged by hand, the change is merged (and its figures will be
    counted again); closed, it is closed; else it is read again after WATCH_EVERY."""
    from evo_agents.hub.server.merge.service import pulls

    target = github_target(change.origin)
    try:
        if target is None:
            raise GitHubRefused(f"repo {change.repo} has no origin on GitHub")
        owner, name = target.owner, target.name

        async def read(token: str) -> dict:
            return await pulls.pull(app, token, owner, name, change.pr_number)

        found = await pulls.with_token(app, owner, name, "read", read)
    except GitHubRefused as exc:  # read again after WATCH_EVERY, not at every pass
        async with engine.begin() as conn:
            await set_change(conn, change.id)
        log.warning("a pull request left open could not be read", extra={"change_id": change.id, "why": str(exc)})
        return "refused"
    async with engine.begin() as conn:
        locked = await change_of_plan(conn, change.project_id, change.plan_id, lock=True)
        if locked is None or locked.state != "open":
            return "moved"
        settled = await _settle(conn, locked, found)
        if settled is None:
            await set_change(conn, change.id)  # read again after WATCH_EVERY
    return settled or "still_open"


async def _settle(conn: AsyncConnection, change, found: dict) -> str | None:
    """What pull request ``found`` of ``change`` (a row of curator_changes, locked), as GitHub reads it whole, makes of
    the change: merged by hand, ``merged`` (when, by whom, at which commit); closed without a merge, ``closed``; None
    while it is open. A change that named no pull request yet names this one."""
    values = {}
    if change.pr_number is None and type(found.get("number")) is int:
        base, head = found.get("base") or {}, (found.get("head") or {}).get("sha")
        values.update(pr_number=found["number"], pr_url=found.get("html_url"), base_branch=base.get("ref"))
        if change.head_sha is None and judge.is_sha(head):
            values["head_sha"] = head
    if found.get("merged") is True:
        login = (found.get("merged_by") or {}).get("login")
        sha = found.get("merge_commit_sha")
        extra = {
            "actor": "user",
            "actor_id": await store.member_of(conn, login),
            "login": login,
            "before_sha": (found.get("base") or {}).get("sha"),
        }
        await set_change(
            conn,
            change.id,
            **values,
            state="merged",
            merged_at=_github_time(found.get("merged_at")) or func.now(),
            merge_sha=sha if judge.is_sha(sha) else None,
            reason=None,
            ledger_extra=extra,
        )
        return "merged_by_hand"
    if found.get("state") == "closed":
        await set_change(
            conn, change.id, **values, state="closed", reason="its pull request was closed without a merge"
        )
        return "closed"
    return None


async def settle_by_branch(engine: AsyncEngine, app: GitHubApp, change, target: Target | None) -> str | None:
    """Look on GitHub for a pull request of ``change``'s branch, in any state, when the hub cannot open one or its plan
    has no step left: one merged, else the newest when it is closed, settles the change as ``_settle`` does (its branch
    merged, or its pull request closed, by hand). The outcome, or None when no pull request settles it or GitHub
    refuses."""
    from evo_agents.hub.server.merge.service import pulls

    if target is None or not change.branch:
        return None

    async def read(token: str) -> dict | None:
        listed = await pulls.branch_pulls(app, token, target.owner, target.name, change.branch)
        chosen = next((item for item in listed if item.get("merged_at")), listed[0] if listed else None)
        if chosen is None or chosen.get("state") != "closed" or type(chosen.get("number")) is not int:
            return None
        return await pulls.pull(app, token, target.owner, target.name, chosen["number"])

    try:
        found = await pulls.with_token(app, target.owner, target.name, "read", read)
    except GitHubRefused as exc:
        log.warning(
            "the pull requests of a change's branch could not be read", extra={"change_id": change.id, "why": str(exc)}
        )
        return None
    if found is None:
        return None
    async with engine.begin() as conn:
        locked = await change_of_plan(conn, change.project_id, change.plan_id, lock=True)
        if locked is None or locked.state != change.state:
            return None
        return await _settle(conn, locked, found)


async def _stalled(engine: AsyncEngine) -> list:
    """The planned changes on GitHub whose plan has no step ready to run, not read for WATCH_EVERY, MAX_WATCH of
    them: a branch merged by hand leaves its change so."""
    async with engine.begin() as conn:
        rows = await store.planned_on_github(conn, WATCH_EVERY)
    return [row for row in rows if all(why is not None for _, why in unready_steps(row.plan_body))][:MAX_WATCH]


async def _watch_planned(engine: AsyncEngine, app: GitHubApp, change) -> str:
    """Read the pull requests of the branch of a planned ``change`` whose plan has no step ready (``settle_by_branch``);
    unsettled, it is read again after WATCH_EVERY."""
    settled = await settle_by_branch(engine, app, change, github_target(change.origin))
    if settled is None:
        async with engine.begin() as conn:
            await store.touch_planned(conn, change.id)
    return settled or "still_planned"


async def watch_pass(engine: AsyncEngine, app: GitHubApp, outcomes: Counter) -> None:
    """Read again the pull requests left open for their owner and the branches of the planned changes whose plan has
    no step ready, counting in ``outcomes`` each change they settle; GitHub failing leaves a change to the next pass."""
    for change in await _left_open(engine):
        try:
            found = await _watch(engine, app, change)
            if found in ("merged_by_hand", "closed"):
                outcomes[found] += 1
        except GitHubUnavailable as exc:
            why = {"change_id": change.id, "why": str(exc)}
            log.warning("a pull request left open waits for GitHub", extra=why)
        except Exception:  # one change failing leaves the others their turn
            log.exception("reading a pull request left open failed", extra={"change_id": change.id})
    for change in await _stalled(engine):
        try:
            found = await _watch_planned(engine, app, change)
            if found in ("merged_by_hand", "closed"):
                outcomes[found] += 1
        except GitHubUnavailable as exc:
            log.warning("a planned change's branch waits for GitHub", extra={"change_id": change.id, "why": str(exc)})
        except Exception:  # one change failing leaves the others their turn
            log.exception("reading a planned change's branch failed", extra={"change_id": change.id})
