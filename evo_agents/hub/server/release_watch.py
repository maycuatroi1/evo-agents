"""How the hub follows the CI of the tags of an open release cycle, a pass of the job ``hub.release_schedules`` after
its checks and its landings (``evo_agents.hub.release_ci`` describes what it reads and decides).

Each pass, for each open cycle: first the tags of the repo under the schedule's prefix (git's ls-refs, as the check
reads them), and a tag of a version greater than every tag the cycle follows and its base tag, which a person pushed,
becomes a tag the cycle follows. Then the newest tag it follows, while its CI is pending or running: the run of the
workflow for it and, once that completed, its jobs and the logs of those that failed (``release_actions``), read
outside any transaction; then, under the cycle's row lock, with the cycle still open:

- passed (success, and the job publish succeeded): the cycle is published, and its owner gets a notice of kind release
  with the version, the link of the workflow run and the version's page on PyPI;
- failed of the infrastructure alone, and run again fewer than INFRA_RERUNS times: its failed jobs run again, and the
  hub waits for that new attempt;
- failed otherwise: the failures are kept on the tag; the cycle stops when a test id failed at the tag it followed
  before too (same_test), else a fix run of the tag is queued with what is left of the cap (``release_cycles.queue_run``,
  which stops the cycle on its budget instead);
- no run of the workflow NO_RUN_MINUTES after the hub began to follow the tag, or a run going MAX_RUN_HOURS: the cycle
  stops (timeout); a run that ended cancelled, or red with no job failed, stops it too (ci).

An installation that does not grant actions keeps the CI unread: the tag says why (``actions_permission``), the owner
is told once, and the deadlines still count. A red tag a fix run was not queued for (another release run was active)
gets it on a later pass. ``red_tag`` is what the check of the schedule reads of its newest tag the same way, to open a
cycle that starts with the fix of a red tag.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import release_ci, releases, tables
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import pulls
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.release_actions import (
    NoActions,
    WorkflowRun,
    failures,
    rerun_failed,
    run_jobs,
    tag_run,
    with_actions,
)
from evo_agents.hub.server.release_cycles import cycle_of, notify_release, queue_run, stop_cycle
from evo_agents.hub.server.release_github import ReadFailed, read_refs
from evo_agents.hub.tables import RUN_ACTIVE

log = logging.getLogger(__name__)

TAGS = "refs/tags/"


@dataclass(frozen=True)
class Read:
    """What the hub read of the CI of one tag: the run of the workflow, its jobs once it completed, and the failures
    of a red one; or what kept it from reading (``blocked``: the installation grants no actions)."""

    run: WorkflowRun | None = None
    jobs: list[dict] = field(default_factory=list)
    failures: dict | None = None
    problem: str | None = None
    blocked: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.run is not None
            and self.run.completed
            and release_ci.published({"conclusion": self.run.conclusion}, self.jobs)
        )

    @property
    def red(self) -> bool:
        """Completed, not published, with a job that failed."""
        return self.failures is not None and bool(self.failures.get("jobs"))


def _open_cycles():
    c, s, p, pr = tables.release_cycles, tables.release_schedules, tables.projects, tables.project_repos
    return (
        select(c.c.id, c.c.base_tag, s.c.workflow, s.c.tag_pattern, s.c.repo, pr.c.origin, p.c.name.label("project"))
        .join_from(c, s, s.c.id == c.c.schedule_id)
        .join(p, p.c.id == c.c.project_id)
        .outerjoin(pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == s.c.repo))
        .where(c.c.state == "open")
        .order_by(c.c.id)
    )


def _version(tag: str, prefix: str):
    return releases.tag_version(tag, prefix + "*")


async def _tags(conn: AsyncConnection, cycle_id: int, prefix: str) -> list:
    """The tags cycle ``cycle_id`` follows, oldest version first."""
    t = tables.release_tags
    rows = (await conn.execute(select(t).where(t.c.cycle_id == cycle_id))).all()
    return sorted(rows, key=lambda row: (_version(row.tag, prefix) or (-1, -1, -1), row.id))


async def read_ci(
    app: GitHubApp,
    where: tuple[str, str],
    *,
    workflow: str,
    tag: str,
    commit: str,
    run_id: int | None = None,
    after_attempt: int | None = None,
) -> Read:
    """What the hub reads of the CI of ``tag`` at ``commit`` (see the module); a run whose attempt is not past
    ``after_attempt`` is read as one going on."""
    owner, name = where

    async def read(token: str) -> Read:
        found = await tag_run(app, token, owner, name, workflow=workflow, tag=tag, commit=commit, run_id=run_id)
        if found is None or not found.completed or (after_attempt and (found.attempt or 0) <= after_attempt):
            return Read(found)
        jobs = await run_jobs(app, token, owner, name, found.id)
        if release_ci.published({"conclusion": found.conclusion}, jobs):
            return Read(found, jobs)
        return Read(found, jobs, await failures(app, token, owner, name, jobs))

    try:
        return await with_actions(app, owner, name, "ci", read)
    except NoActions as exc:
        return Read(problem=str(exc), blocked=True)
    except (GitHubRefused, GitHubUnavailable) as exc:
        return Read(problem=str(exc))


async def red_tag(app: GitHubApp, where: tuple[str, str], workflow: str, tag: str, commit: str) -> Read:
    """What the check reads of the CI of the newest tag (``read_ci``)."""
    return await read_ci(app, where, workflow=workflow, tag=tag, commit=commit)


# Following the tags a person pushed


async def _follow(engine: AsyncEngine, app: GitHubApp, where: tuple[str, str], row) -> bool:
    """Follow the newest tag of the repo under the prefix when its version is greater than every tag the cycle follows
    and its base tag; whether it did."""
    owner, name = where
    prefix = releases.tag_prefix(row.tag_pattern)
    try:
        refs = await pulls.with_token(
            app, owner, name, "tree", lambda token: read_refs(app, token, owner, name, [TAGS + prefix])
        )
    except (ReadFailed, GitHubRefused, GitHubUnavailable) as exc:
        log.info("release tags not read", extra={"cycle_id": row.id, "why": str(exc)})
        return False
    found = releases.latest_tag({ref.removeprefix(TAGS): sha for ref, sha in refs.items()}, row.tag_pattern)
    if found is None:
        return False
    async with engine.begin() as conn:
        followed = await _tags(conn, row.id, prefix)
        known = [_version(tag.tag, prefix) for tag in followed] + [_version(row.base_tag or "", prefix)]
        floor = max((item for item in known if item is not None), default=None)
        if floor is not None and _version(found[0], prefix) <= floor:
            return False
        t = tables.release_tags
        made = upsert(t).values(cycle_id=row.id, tag=found[0], commit=found[1]).on_conflict_do_nothing()
        added = (await conn.execute(made.returning(t.c.id))).scalar_one_or_none() is not None
    if added:
        log.info("release tag followed", extra={"cycle_id": row.id, "tag": found[0]})
    return added


# Deciding


def _moment(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    except ValueError:
        return None


def _overdue(row, tag, read: Read, now: datetime) -> str | None:
    """Why the CI of ``tag`` is overdue now, or None."""
    run = read.run
    if run is None:
        if now - tag.watched_since <= timedelta(minutes=release_ci.NO_RUN_MINUTES):
            return None
        why = f": {read.problem}" if read.problem else ""
        return (
            f"no run of {row.workflow} for {tag.tag} {release_ci.NO_RUN_MINUTES} minutes after the hub began to "
            f"follow it{why}"
        )
    started = max(_moment(run.started_at) or tag.watched_since, tag.watched_since)
    if now - started <= timedelta(hours=release_ci.MAX_RUN_HOURS):
        return None
    return (
        f"the run of {row.workflow} for {tag.tag} ({run.url or run.id}) is still going {release_ci.MAX_RUN_HOURS} "
        "hours after it started"
    )


async def _locked(conn: AsyncConnection, cycle_id: int, tag_id: int):
    """The cycle's row locked, and the tag's row once the cycle is open still; None otherwise."""
    c, t = tables.release_cycles, tables.release_tags
    state = (await conn.execute(select(c.c.state).where(c.c.id == cycle_id).with_for_update())).scalar_one()
    if state != "open":
        return None
    return (await conn.execute(select(t).where(t.c.id == tag_id))).one()


def _run_values(read: Read) -> dict:
    run = read.run
    if run is None:
        return {}
    return {
        "workflow_run_id": run.id,
        "workflow_url": run.url,
        "run_attempt": run.attempt,
        "started_at": _moment(run.started_at),
    }


async def _save(conn: AsyncConnection, tag_id: int, **values) -> None:
    t = tables.release_tags
    await conn.execute(update(t).where(t.c.id == tag_id).values(checked_at=func.now(), **values))


async def _published(conn: AsyncConnection, row, tag, read: Read) -> str:
    c = tables.release_cycles
    await _save(conn, tag.id, ci_state="passed", problem=None, **_run_values(read))
    await conn.execute(
        update(c).where(c.c.id == row.id).values(state="published", ended_at=func.now(), updated_at=func.now())
    )
    found = await cycle_of(conn, row.id)
    version = release_ci.tag_version_text(tag.tag, releases.tag_prefix(row.tag_pattern))
    url, pypi = read.run.url if read.run else None, release_ci.pypi_url(version)
    await notify_release(
        conn,
        found,
        event="published",
        title=f"Released {version} of {row.repo}: {tag.tag} passed its CI and is on PyPI",
        body=f"Cycle #{row.id} of the release of {row.project} published {version}.\nWorkflow run: {url}\nPyPI: {pypi}",
        run_id=tag.run_id,
        details={"tag": tag.tag, "version": version, "workflow_url": url, "pypi_url": pypi},
    )
    log.info("release published", extra={"cycle_id": row.id, "tag": tag.tag})
    return "published"


async def _blocked(conn: AsyncConnection, row, tag, read: Read) -> None:
    """Keep why the CI of ``tag`` is unread; tell the owner the first time the installation grants no actions."""
    first = read.blocked and not (tag.problem or "").startswith(release_ci.ACTIONS_PERMISSION)
    await _save(conn, tag.id, problem=releases.clip_reason(read.problem or "unread"))
    if first:
        found = await cycle_of(conn, row.id)
        await notify_release(
            conn,
            found,
            event=release_ci.ACTIONS_PERMISSION,
            title=f"The hub cannot read the CI of {tag.tag}: the App lacks actions",
            body=read.problem or "",
            run_id=tag.run_id,
            details={"tag": tag.tag, "problem": release_ci.ACTIONS_PERMISSION},
        )


async def _red(conn: AsyncConnection, row, tag) -> str:
    """What a red ``tag`` of open cycle ``row`` (its failures kept) leads to: a stop for a test that failed at the tag
    before too, else its fix run; waiting while another release run of the project is active."""
    prefix = releases.tag_prefix(row.tag_pattern)
    followed = await _tags(conn, row.id, prefix)
    before = [item for item in followed if (_version(item.tag, prefix) or ()) < (_version(tag.tag, prefix) or ())]
    if before and before[-1].ci_state == "failed":
        same = release_ci.same_tests((before[-1].failures or {}).get("ids"), (tag.failures or {}).get("ids"))
        if same:
            shown = ", ".join(same[:5]) + (f" and {len(same) - 5} more" if len(same) > 5 else "")
            reason = f"the same test failed at {before[-1].tag} and {tag.tag}, two tags in a row: {shown}"
            await stop_cycle(conn, row.id, reason, "same_test")
            return "stopped"
    rr, r = tables.release_runs, tables.runs
    active = exists().where(rr.c.cycle_id == row.id, r.c.id == rr.c.run_id, r.c.state.in_(RUN_ACTIVE))
    asked = exists().where(rr.c.cycle_id == row.id, rr.c.mode == "fix", rr.c.tag == tag.tag)
    busy, fixing = (await conn.execute(select(active, asked))).one()
    if busy or fixing:
        return "waiting"
    if await queue_run(conn, row.id, "fix", red_tag=tag.tag) is not None:
        return "fix"
    state = (
        await conn.execute(select(tables.release_cycles.c.state).where(tables.release_cycles.c.id == row.id))
    ).scalar_one()
    return "stopped" if state == "stopped" else "waiting"


async def _rerun(engine: AsyncEngine, app: GitHubApp, where: tuple[str, str], row, tag, read: Read) -> str | None:
    """Run the failed jobs of ``tag``'s run again, for a failure of the infrastructure alone; None when GitHub
    refused, and the failure goes on as any other."""
    try:
        await rerun_failed(app, *where, read.run.id)
    except (GitHubRefused, GitHubUnavailable) as exc:
        log.warning("release jobs not run again", extra={"cycle_id": row.id, "tag": tag.tag, "why": str(exc)})
        return None
    async with engine.begin() as conn:
        if await _locked(conn, row.id, tag.id) is None:
            return "waiting"
        await _save(
            conn,
            tag.id,
            ci_state="running",
            reruns=tag.reruns + 1,
            rerun_of_attempt=read.run.attempt or 1,
            watched_since=func.now(),
            failures=read.failures,
            problem=None,
            **_run_values(read),
        )
    log.info("release jobs run again", extra={"cycle_id": row.id, "tag": tag.tag, "run": read.run.id})
    return "rerun"


async def _decide(conn: AsyncConnection, row, tag, read: Read, now: datetime) -> str:
    """Under the cycle's lock: what ``read`` of open cycle ``row``'s newest ``tag`` leads to (see the module)."""
    if read.passed:
        return await _published(conn, row, tag, read)
    if read.problem:
        await _blocked(conn, row, tag, read)
    elif read.failures is None:
        await _save(conn, tag.id, ci_state="running" if read.run else "pending", problem=None, **_run_values(read))
    if read.failures is None:
        overdue = _overdue(row, tag, read, now)
        if overdue is None:
            return "waiting"
        await _save(conn, tag.id, ci_state="timeout")
        await stop_cycle(conn, row.id, overdue, "timeout")
        return "stopped"
    await _save(conn, tag.id, ci_state="failed", failures=read.failures, problem=None, **_run_values(read))
    if not read.red:
        ended = f"the run of {row.workflow} for {tag.tag} ({read.run.url or read.run.id}) ended {read.run.conclusion}"
        if read.run.conclusion == "success":
            ended += f" but its job {release_ci.PUBLISH_JOB} did not succeed"
        await stop_cycle(conn, row.id, f"{ended}, and no job of it failed: no fix forward answers that", "ci")
        return "stopped"
    t = tables.release_tags
    return await _red(conn, row, (await conn.execute(select(t).where(t.c.id == tag.id))).one())


async def watch(engine: AsyncEngine, app: GitHubApp | None, row, *, now: datetime | None = None) -> str:
    """One pass of the watch of open cycle ``row`` (see the module): published, stopped, fix, rerun, followed or
    waiting."""
    where = github_repo(row.origin) if row.origin else None
    if app is None or where is None:
        return "waiting"
    followed = await _follow(engine, app, where, row)
    async with engine.connect() as conn:
        tags = await _tags(conn, row.id, releases.tag_prefix(row.tag_pattern))
    if not tags:
        return "followed" if followed else "waiting"
    tag = tags[-1]
    if tag.ci_state == "failed":  # a fix not queued yet: another release run was active
        async with engine.begin() as conn:
            locked = await _locked(conn, row.id, tag.id)
            return "waiting" if locked is None else await _red(conn, row, locked)
    if tag.ci_state not in release_ci.WATCHED:
        return "followed" if followed else "waiting"
    read = await read_ci(
        app,
        where,
        workflow=row.workflow,
        tag=tag.tag,
        commit=tag.commit,
        run_id=tag.workflow_run_id,
        after_attempt=tag.rerun_of_attempt,
    )
    if read.red and read.failures.get("infra") and tag.reruns < release_ci.INFRA_RERUNS:
        rerun = await _rerun(engine, app, where, row, tag, read)
        if rerun is not None:
            return rerun
    async with engine.begin() as conn:
        locked = await _locked(conn, row.id, tag.id)
        if locked is None:
            return "waiting"
        found = await _decide(conn, row, locked, read, now or datetime.now(UTC))
    return "followed" if followed and found == "waiting" else found


async def watch_releases(engine: AsyncEngine, app: GitHubApp | None, *, now: datetime | None = None) -> dict:
    """One pass of the watch of every open cycle, one failing leaving the others their turn; how many ended each way
    but waiting."""
    async with engine.connect() as conn:
        rows = (await conn.execute(_open_cycles())).all()
    outcomes: Counter = Counter()
    for row in rows:
        try:
            found = await watch(engine, app, row, now=now)
        except Exception:
            log.exception("a release watch failed", extra={"cycle_id": row.id})
            outcomes["failed"] += 1
            continue
        if found != "waiting":
            outcomes[found] += 1
    return dict(sorted(outcomes.items()))
