"""The check of a project's release schedule, and the job ``hub.release_schedules`` that makes it once a local day
(``evo_agents.hub.releases`` describes both).

``check`` is one check of one schedule, for the job (trigger ``schedule``, the local day ``releases.due_day`` names) or
for an admin's ``evo-agents hub release run`` (trigger ``manual``, the local day of now). It reads nothing of GitHub
when the schedule is paused, has a cycle open, or its owner no longer holds the admin role on the project; else it reads
the newest tag and the target's commit (``evo_agents.hub.server.release_github``) outside any transaction, and the CI of
the newest tag (``release_watch.red_tag``), then, under the schedule's row lock, looks at the gates again and records
the check. A stopped cycle keeps the check busy until its owner answers, unless the newest tag is newer than any the
cycle knows (its base tag and the tags it made) and no run of it is active: a release made outside the cycle superseded
it, so the check closes it, cancels the decision that waits on it and its notification, says so in the cycle's thread of
the triage chat, and goes on from that tag as if no cycle were there. A newest tag whose CI is red (its run of the
workflow completed, did not publish, and a job of it failed), whoever made it, opens a cycle that starts with a fix: the
cycle follows that tag with the failures read, and its first run, of kind release in mode fix, fixes it forward, whether
a packaging path changed or not. Otherwise, with a packaging path changed, it opens a cycle and queues its first run in
mode cut. Either run is dispatched as the owner, pinned to no worker, with what is left of the cycle's cap as its cost
cap, audited as release.dispatch with the owner as actor and no token. A CI the hub cannot read (the App's installation
grants no actions) counts as not red, and the check's reason says why. The scheduled check of a day happens once: a
check of that day that did not fail, or ERROR_TRIES that did, and the job passes the schedule over, and the unique index
on the day (``release_checks_day_key``) holds when two passes meet. No model is called by any of this.

``fire_release_schedules`` is the job's checks, run every minute by the hub's worker: each schedule whose local time
came in the last CATCH_UP_MINUTES gets the check of that day, in its own transactions, one failing leaving the others
their turn; ``release_pass`` is the whole job, those checks, then the landing of the release runs that are done
(``evo_agents.hub.server.release_land``), then the watch of the CI of the open cycles' tags
(``evo_agents.hub.server.release_watch``). ``cancel_queued`` and ``close_idle`` are what pausing does: the release runs
of the project still queued are cancelled, and an open cycle left with no active run closes, with the pause as its
reason.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, exists, func, insert, literal, null, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, releases, tables
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit
from evo_agents.hub.server.chat_runs import hub_reply
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.release_cycles import DISPATCH, queue_run
from evo_agents.hub.server.release_github import ReadFailed, Reading, read_release
from evo_agents.hub.server.release_land import land_releases, owner_is_admin
from evo_agents.hub.server.release_watch import Read, red_tag, watch_releases
from evo_agents.hub.server.run_state import move_run, notify_queued, write_event
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.tables import RUN_ACTIVE

log = logging.getLogger(__name__)

NO_APP = "this hub has no GitHub App (EVO_HUB_GITHUB_APP_ID, EVO_HUB_GITHUB_APP_PRIVATE_KEY): it reads no tag"


@dataclass(frozen=True)
class Checked:
    """A check recorded: its id and outcome, and the cycle and run it opened."""

    id: int
    outcome: str
    cycle_id: int | None = None
    run_id: int | None = None


def schedules(now: datetime | None = None):
    """Each release schedule with its project's name, its owner's and pauser's logins, and the local time in its zone
    at ``now`` (the database's now when None)."""
    s, p, u = tables.release_schedules, tables.projects, tables.users
    pauser = u.alias("pauser")
    moment = func.now() if now is None else literal(now)
    return (
        select(
            s,
            p.c.name.label("project"),
            u.c.login.label("owner"),
            pauser.c.login.label("paused_by_login"),
            func.timezone(s.c.timezone, moment).label("local_now"),
        )
        .join_from(s, p, p.c.id == s.c.project_id)
        .join(u, u.c.id == s.c.owner_id)
        .outerjoin(pauser, pauser.c.id == s.c.paused_by)
        .order_by(s.c.id)
    )


async def unfinished(conn: AsyncConnection, schedule_id: int):
    """The cycle of ``schedule_id`` that is open or stopped, (id, state), or None."""
    c = tables.release_cycles
    query = select(c.c.id, c.c.state).where(c.c.schedule_id == schedule_id, c.c.state.in_(releases.UNFINISHED))
    return (await conn.execute(query)).one_or_none()


async def _gate(conn: AsyncConnection, row, *, past_stopped: bool = False) -> tuple[str, str] | None:
    """(outcome, reason) of a check of ``row`` that reads nothing, or None when it may read GitHub; ``past_stopped``
    lets a stopped cycle through, for the read that may find it superseded."""
    if row.paused_at is not None:
        return "paused", f"the release schedule of {row.project} is paused, by {row.paused_by_login or 'the hub'}"
    cycle = await unfinished(conn, row.id)
    if cycle is not None and not (past_stopped and cycle.state == "stopped"):
        return "busy", f"cycle #{cycle.id} is {cycle.state}: no other cycle opens until it ends"
    if not await owner_is_admin(conn, row.owner_id, row.owner, row.project):
        return "owner", (
            f"{row.owner}, the owner of the schedule, holds no admin role on {row.project} any more: an admin sets the "
            "schedule again to own it"
        )
    return None


async def _github_repo(conn: AsyncConnection, row) -> tuple[str, str] | None:
    """(owner, name) on GitHub of the schedule's repo, as the project lists its origin; None off GitHub."""
    r = tables.project_repos
    query = select(r.c.origin).where(r.c.project_id == row.project_id, r.c.name == row.repo)
    origin = (await conn.execute(query)).scalar_one_or_none()
    return github_repo(origin) if origin else None


async def _day_done(conn: AsyncConnection, schedule_id: int, day: date) -> bool:
    """Whether the scheduled check of ``day`` is made: one that did not fail, or ERROR_TRIES that did."""
    k = tables.release_checks
    of_day = (k.c.schedule_id == schedule_id, k.c.trigger == "schedule", k.c.local_day == day)
    query = select(
        exists().where(*of_day, k.c.outcome != "error").label("made"),
        select(func.count()).select_from(k).where(*of_day, k.c.outcome == "error").scalar_subquery().label("failed"),
    )
    found = (await conn.execute(query)).one()
    return bool(found.made) or found.failed >= releases.ERROR_TRIES


async def _record(conn: AsyncConnection, row, how: dict, outcome: str, **values) -> Checked:
    """Record a check of ``row`` (``how``: trigger, local_day, requested_by) that ended ``outcome``."""
    if values.get("reason") is not None:
        values["reason"] = releases.clip_reason(values["reason"])
    k = tables.release_checks
    statement = insert(k).values(project_id=row.project_id, schedule_id=row.id, outcome=outcome, **how, **values)
    check_id = (await conn.execute(statement.returning(k.c.id))).scalar_one()
    log.info("release check", extra={"project": row.project, "check_id": check_id, "outcome": outcome, **how})
    return Checked(check_id, outcome, values.get("cycle_id"), values.get("run_id"))


def _read_values(reading: Reading) -> dict:
    return {
        "base_tag": reading.base_tag,
        "base_commit": reading.base_commit,
        "target_commit": reading.target_commit,
        "changed_paths": reading.changed,
    }


def _cut_run(row, reading: Reading, budget: dict) -> dict:
    """The columns of the cut run of a cycle of ``row`` from ``reading``, with ``budget``."""
    title = releases.cut_title(row.repo, reading.base_tag, row.target_ref, reading.target_commit)
    return releases.run_values(
        project_id=row.project_id, owner_id=row.owner_id, repo=row.repo, title=title, budget=budget
    )


async def _open(conn: AsyncConnection, row, day: date, reading: Reading) -> tuple[int, int] | None:
    """Open a cycle of ``row`` from ``reading`` and queue its cut run: (cycle id, run id); None when another cycle or
    release run of the project got in first."""
    c, rr = tables.release_cycles, tables.release_runs
    cap = float(row.cycle_budget_usd)
    budget = releases.run_budget(cap)  # a new cycle has spent nothing
    if budget["max_usd"] < curator.MIN_RUN_USD:
        return None
    cycle = {"project_id": row.project_id, "schedule_id": row.id, "start": "cut", "owner_id": row.owner_id}
    cycle |= {"local_day": day, "target_ref": row.target_ref, "budget_usd": cap, **_read_values(reading)}
    cycle["span"] = reading.span  # the range the claim of its cut run builds the prompt from
    try:
        async with conn.begin_nested():
            cycle_id = (await conn.execute(insert(c).values(**cycle).returning(c.c.id))).scalar_one()
            run_id = await insert_run(conn, _cut_run(row, reading, budget))
            await conn.execute(insert(rr).values(run_id=run_id, cycle_id=cycle_id, mode="cut"))
    except IntegrityError:  # release_cycles_unfinished_key or runs_active_release_key
        return None
    text = (
        f"Queued by the release schedule of project {row.project} for {day.isoformat()}, as {row.owner}: cycle "
        f"#{cycle_id} cuts {row.target_ref} at {releases.short(reading.target_commit)} since {reading.base_tag} "
        f"({', '.join(reading.changed)} changed), cost cap {curator.money(budget['max_usd'])} of the cycle's "
        f"{curator.money(cap)}."
    )
    await write_event(conn, run_id, {"text": text, "cycle_id": cycle_id, "release_mode": "cut", **budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=row.owner_id,
        token_id=None,
        action=DISPATCH,
        target=f"{row.project} cycle:{cycle_id} run:{run_id} mode=cut",
        project_id=row.project_id,
    )
    return cycle_id, run_id


class _Busy(Exception):
    """Another release run of the project got in first: the cycle just made is rolled back."""


def _red_values(read: Reading, ci: Read) -> dict:
    """The row of release_tags of the red newest tag a cycle opens on."""
    run = ci.run
    return {
        "tag": read.base_tag,
        "commit": read.base_commit,
        "ci_state": "failed",
        "workflow_run_id": run.id if run else None,
        "workflow_url": run.url if run else None,
        "run_attempt": run.attempt if run else None,
        "failures": ci.failures,
        "checked_at": func.now(),
    }


async def _open_fix(conn: AsyncConnection, row, day: date, read: Reading, ci: Read) -> tuple[int, int] | None:
    """Open a cycle of ``row`` that starts with the fix of the red newest tag of ``read``: (cycle id, run id); None
    when another cycle or release run of the project got in first."""
    c, t = tables.release_cycles, tables.release_tags
    cycle = {"project_id": row.project_id, "schedule_id": row.id, "start": "fix", "owner_id": row.owner_id}
    cycle |= {"local_day": day, "target_ref": row.target_ref, "budget_usd": float(row.cycle_budget_usd)}
    cycle |= {**_read_values(read), "span": null() if read.span is None else read.span}  # SQL NULL, not JSON null
    try:
        async with conn.begin_nested():
            cycle_id = (await conn.execute(insert(c).values(**cycle).returning(c.c.id))).scalar_one()
            await conn.execute(insert(t).values(cycle_id=cycle_id, **_red_values(read, ci)))
            run_id = await queue_run(conn, cycle_id, "fix", red_tag=read.base_tag)
            if run_id is None:
                raise _Busy
    except (IntegrityError, _Busy):
        return None
    return cycle_id, run_id


async def _ci(app: GitHubApp | None, where: tuple[str, str] | None, row, read: Reading | str) -> Read | None:
    """What the check of ``row`` reads of the CI of the newest tag, once it read the tag."""
    if app is None or where is None or isinstance(read, str):
        return None
    return await red_tag(app, where, row.workflow, read.base_tag, read.base_commit)


def _unread(ci: Read | None, read: Reading) -> str:
    """What a check's reason adds when it could not read the CI of the newest tag."""
    if ci is None or not ci.problem:
        return ""
    return f"; the hub could not read the CI of {read.base_tag}: {ci.problem}"


async def _read(app: GitHubApp | None, where: tuple[str, str] | None, row) -> Reading | str:
    """What the check of ``row`` reads of GitHub, or why it could not."""
    if app is None:
        return NO_APP
    if where is None:
        return f"repo {row.repo} of project {row.project} has no origin on GitHub: register it with one"
    try:
        return await read_release(app, *where, row)
    except ReadFailed as exc:
        return str(exc)


async def _decide_red(conn: AsyncConnection, row, how: dict, read: Reading, ci: Read) -> Checked:
    """Record the check of ``row`` whose newest tag is red: a cycle opened on its fix, or busy."""
    opened = await _open_fix(conn, row, how["local_day"], read, ci)
    if opened is None:
        cycle = await unfinished(conn, row.id)
        why = f"cycle #{cycle.id} is {cycle.state}" if cycle else "another release run of the project is active"
        return await _record(conn, row, how, "busy", reason=f"{why}: no other cycle opens", **_read_values(read))
    cycle_id, run_id = opened
    where = f" ({ci.run.url})" if ci.run and ci.run.url else ""
    tests = ", ".join((ci.failures or {}).get("ids", [])[:5])
    why = (
        f"{read.base_tag} is red: the run of {row.workflow} for it{where} failed ({tests}); a fix run fixes it forward"
    )
    return await _record(conn, row, how, "opened", reason=why, cycle_id=cycle_id, run_id=run_id, **_read_values(read))


async def _decide(conn: AsyncConnection, row, how: dict, read: Reading | str, ci: Read | None = None) -> Checked:
    """Record the check of ``row`` once GitHub was read, under the schedule's row lock the caller holds."""
    if not isinstance(read, str) and row.paused_at is None:
        await supersede(conn, row, read.base_tag)
    gated = await _gate(conn, row)
    if gated is not None:
        return await _record(conn, row, how, gated[0], reason=gated[1])
    if isinstance(read, str):
        return await _record(conn, row, how, "error", reason=read)
    if ci is not None and ci.red:
        return await _decide_red(conn, row, how, read, ci)
    if not read.changed:
        since = (
            f"{row.target_ref} at {releases.short(read.target_commit)} changes no packaging path since {read.base_tag}"
        )
        return await _record(conn, row, how, "unchanged", reason=since + _unread(ci, read), **_read_values(read))
    opened = await _open(conn, row, how["local_day"], read)
    if opened is None:
        cycle = await unfinished(conn, row.id)
        why = f"cycle #{cycle.id} is {cycle.state}" if cycle else "another release run of the project is active"
        return await _record(conn, row, how, "busy", reason=f"{why}: no other cycle opens", **_read_values(read))
    cycle_id, run_id = opened
    why = f"{', '.join(read.changed)} changed since {read.base_tag}" + _unread(ci, read)
    return await _record(conn, row, how, "opened", reason=why, cycle_id=cycle_id, run_id=run_id, **_read_values(read))


async def check(
    engine: AsyncEngine,
    app: GitHubApp | None,
    schedule_id: int,
    *,
    trigger: str,
    local_day: date | None = None,
    requested_by: int | None = None,
    now: datetime | None = None,
) -> Checked | None:
    """One check of schedule ``schedule_id`` (see the module); None when the schedule is gone, or the scheduled check
    of ``local_day`` is made already."""
    s = tables.release_schedules
    query = schedules(now).where(s.c.id == schedule_id)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(query)).one_or_none()
            if row is None or (trigger == "schedule" and await _day_done(conn, schedule_id, local_day)):
                return None
            how = {"trigger": trigger, "local_day": local_day or row.local_now.date(), "requested_by": requested_by}
            gated = await _gate(conn, row, past_stopped=True)
            if gated is not None:
                return await _record(conn, row, how, gated[0], reason=gated[1])
            where = await _github_repo(conn, row)
        read = await _read(app, where, row)  # outside any transaction: GitHub may take seconds
        ci = await _ci(app, where, row, read)
        async with engine.begin() as conn:
            await conn.execute(select(s.c.id).where(s.c.id == schedule_id).with_for_update())
            row = (await conn.execute(query)).one_or_none()
            if row is None or (trigger == "schedule" and await _day_done(conn, schedule_id, how["local_day"])):
                return None
            return await _decide(conn, row, how, read, ci)
    except IntegrityError:  # release_checks_day_key: another pass recorded the day's check first
        return None


async def fire_release_schedules(engine: AsyncEngine, app: GitHubApp | None, *, now: datetime | None = None) -> dict:
    """One pass of ``hub.release_schedules`` at ``now`` (the database's now when None): the check of each schedule
    whose day is due; how many ended each outcome."""
    async with engine.connect() as conn:
        rows = (await conn.execute(schedules(now))).all()
    outcomes: Counter = Counter()
    for row in rows:
        day = releases.due_day(row.local_now, row.check_at)
        if day is None:
            continue
        try:
            found = await check(engine, app, row.id, trigger="schedule", local_day=day, now=now)
        except Exception:  # one schedule failing leaves the others their turn
            log.exception("a release check failed", extra={"project": row.project, "schedule_id": row.id})
            outcomes["failed"] += 1
            continue
        outcomes[found.outcome if found else "done"] += 1
    return dict(sorted(outcomes.items()))


async def release_pass(engine: AsyncEngine, app: GitHubApp | None, *, now: datetime | None = None) -> dict:
    """One pass of ``hub.release_schedules``: the checks that are due, the landing of the release runs done, then
    the watch of the open cycles' CI, which reads the clock (``now`` is the checks'); how many of each ended each way,
    the landings under ``landed_<how>`` and the watch's under ``watched_<how>``."""
    found = await fire_release_schedules(engine, app, now=now)
    landed = await land_releases(engine, app)
    watched = await watch_releases(engine, app)
    return {
        **found,
        **{f"landed_{how}": count for how, count in landed.items()},
        **{f"watched_{how}": count for how, count in watched.items()},
    }


# Superseding


async def supersede(conn: AsyncConnection, row, newest: str) -> int | None:
    """Close the stopped cycle of schedule ``row`` that tag ``newest`` supersedes (see the module), cancel its decision
    and the decision's notifications, and say so in its thread; the cycle's id, or None when there is none to close."""
    cycle = await unfinished(conn, row.id)
    newer = releases.tag_version(newest, row.tag_pattern)
    if cycle is None or cycle.state != "stopped" or newer is None:
        return None
    c, t, rr, r = tables.release_cycles, tables.release_tags, tables.release_runs, tables.runs
    found = (await conn.execute(select(c).where(c.c.id == cycle.id, c.c.state == "stopped").with_for_update())).one()
    made = (await conn.execute(select(t.c.tag).where(t.c.cycle_id == found.id))).scalars().all()
    known = [v for v in (releases.tag_version(tag, row.tag_pattern) for tag in (found.base_tag, *made) if tag) if v]
    active = exists().where(rr.c.cycle_id == found.id, r.c.id == rr.c.run_id, r.c.state.in_(RUN_ACTIVE))
    if (known and newer <= max(known)) or (await conn.execute(select(active))).scalar_one():
        return None
    why = f"{newest} was tagged outside cycle #{found.id}, which it supersedes"
    reason = releases.clip_reason(f"{found.stop_reason or 'stopped'}; {why}")
    closed = {"state": "closed", "stop_reason": reason, "ended_at": func.now(), "updated_at": func.now()}
    await conn.execute(update(c).where(c.c.id == found.id).values(**closed))
    if found.decision_id is not None:
        d, n = tables.decisions, tables.notifications
        await conn.execute(update(d).values(state="cancelled").where(d.c.id == found.decision_id, d.c.state == "open"))
        unread = (n.c.decision_id == found.decision_id, n.c.read_at.is_(None))
        await conn.execute(update(n).values(read_at=func.now()).where(*unread))
    if found.triage_chat_id and found.triage_root_id:
        cancelled = f" and cancelled decision #{found.decision_id}" if found.decision_id is not None else ""
        said = f"{why}: the hub closed the cycle{cancelled}; the release goes on from {newest}."
        await hub_reply(conn, found.triage_chat_id, found.triage_root_id, said)
    log.info("release cycle superseded", extra={"cycle_id": found.id, "tag": newest, "decision_id": found.decision_id})
    return found.id


# Pausing


async def cancel_queued(conn: AsyncConnection, project_id: int, reason: str) -> list[int]:
    """Cancel the release runs of the project still queued; their ids."""
    r = tables.runs
    query = (
        select(r.c.id)
        .where(r.c.project_id == project_id, r.c.kind == releases.RELEASE_KIND, r.c.state == "queued")
        .order_by(r.c.id)
        .with_for_update(skip_locked=True)
    )
    cancelled = []
    for run_id in (await conn.execute(query)).scalars().all():
        await move_run(conn, run_id, "queued", "cancelled", "owner", reason=reason)
        cancelled.append(run_id)
    return cancelled


async def close_idle(conn: AsyncConnection, schedule_id: int, reason: str) -> int | None:
    """Close the open cycle of ``schedule_id`` that has no run active; its id, or None."""
    c, rr, r = tables.release_cycles, tables.release_runs, tables.runs
    active = exists().where(rr.c.cycle_id == c.c.id, r.c.id == rr.c.run_id, r.c.state.in_(RUN_ACTIVE))
    statement = (
        update(c)
        .values(state="closed", stop_reason=releases.clip_reason(reason), ended_at=func.now(), updated_at=func.now())
        .where(and_(c.c.schedule_id == schedule_id, c.c.state == "open", ~active))
        .returning(c.c.id)
    )
    return (await conn.execute(statement)).scalar_one_or_none()
