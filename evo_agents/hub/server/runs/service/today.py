"""Today, what waits for the caller and what changed since they last looked (``evo_agents.hub.server.today`` is
the API and its rules): the items of the window, the summary sentence, and Today itself (``compute``), in the
caller's transaction or in one ``UnitOfWork`` of its own (``today``, ``today_seen``)."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, problems, runs
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.loop.view import measure
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.releases.models.cycles import releases_link
from evo_agents.hub.server.review_inbox import proposal_link
from evo_agents.hub.server.runs.models.today import (
    BODY_CHARS,
    DEFAULT_WINDOW,
    ITEM_KINDS,
    MAX_ITEMS,
    MAX_WINDOW,
    METRIC_DAYS,
    NO_MARK,
    OLD_MARK,
    SEEN,
    TITLE_CHARS,
    Found,
    Today,
    TodayDecision,
    TodayItem,
)
from evo_agents.hub.server.runs.repository import today as repository
from evo_agents.hub.server.runs.repository.overview import parameters
from evo_agents.hub.server.runs.service.overview import parks_at, scope
from evo_agents.hub.server.security import holds

log = logging.getLogger("evo_agents.hub.server.today")


# The items


def _line(text: str | None, limit: int) -> str | None:
    return NOTIFIER.one_line(text, limit) if text and text.strip() else None


def _release(row) -> Found:
    version = row.tag or "a new version"
    item = TodayItem(
        kind="release",
        title=NOTIFIER.one_line(f"Released {version} of {row.repo}", TITLE_CHARS),
        body=f"The daily release of {row.project} published {version}.",
        at=row.at,
        project=row.project,
        link=releases_link(row.project),
    )
    return Found(item)


def _run(row) -> Found:
    named = row.title or row.plan_id or "a run on no plan"
    if row.state == "parked":
        kind, title, body = "run_parked", f"Run #{row.id} parked", f"{named}: it waits for an answer."
    else:
        verb = "was lost" if row.state == "lost" else "failed"
        kind, title = "run_failed", f"Run #{row.id} {verb}"
        body = f"{named}: {row.error}" if row.error else named
    item = TodayItem(
        kind=kind,
        title=title,
        body=_line(body, BODY_CHARS),
        at=row.at,
        project=row.project,
        link=NOTIFIER.run_link(row.project, row.id),
    )
    return Found(item)


def _cost(row) -> Found:
    spent = float(row.cost or 0)
    runs_ended = int(row.runs or 0)
    item = TodayItem(
        kind="cost",
        title=f"Your runs cost {curator.money(spent)}",
        body=f"{_count(runs_ended, 'run')} you dispatched ended in {row.project}.",
        at=row.at,
        project=row.project,
        link=f"/p/{row.project}/runs",
    )
    return Found(item, cost_usd=spent)


REPORTS = {  # each kind of notice that only reports, one of it and some of it
    "push_default_branch": ("a push to a default branch", "{} pushes to a default branch"),
    "merge_default_branch": ("a merge into a default branch", "{} merges into a default branch"),
    "plan_finished": ("a plan finished", "{} plans finished"),
    runs.OUTPUT_NOTICE: ("files from a run", "{} notices of files from runs"),
}


def _report(row) -> Found:
    count = int(row.count)
    if count == 1:
        title, body = row.title, row.body
        link = row.link or f"/inbox?kind=notice&project={row.project}"
    else:
        some = REPORTS.get(row.notice_kind, ("a notice", "{} notices"))[1].format(count)
        title, body = f"{some[0].upper()}{some[1:]} in {row.project}", f"The newest: {row.title}"
        link = f"/inbox?kind=notice&project={row.project}"
    item = TodayItem(
        kind="report",
        title=NOTIFIER.one_line(title, TITLE_CHARS),
        body=_line(body, BODY_CHARS),
        at=row.at,
        project=row.project,
        link=link,
    )
    return Found(item)


def _first_done(revisions: list, since: datetime) -> tuple[set[str], dict[str, datetime]]:
    """Of a plan's ``revisions`` (oldest first): the steps done at the start of the window, and when each step done
    since was first done. A plan written first in the window starts from its first revision: the steps that one has
    done were done elsewhere, as when a plan is pushed from a file, and are none of the window's news."""
    before: set[str] | None = None
    first: dict[str, datetime] = {}
    for revision in revisions:
        done = {str(value) for value in revision.done or [] if isinstance(value, (str, int))}
        if revision.created_at <= since or before is None:
            before = done
            continue
        for key in done - before:
            first.setdefault(key, revision.created_at)
    return before or set(), first


def _steps_of(plan, revisions: list, since: datetime) -> list[Found]:
    before, first = _first_done(revisions, since)
    steps = plan.steps if isinstance(plan.steps, list) else []
    found = []
    for step in steps:
        if not isinstance(step, dict) or step.get("status") != "done" or not isinstance(step.get("id"), (str, int)):
            continue
        key = str(step["id"])
        if key in before or key not in first:
            continue
        title = step.get("title") if isinstance(step.get("title"), str) else None
        item = TodayItem(
            kind="step_done",
            title=NOTIFIER.one_line(title or f"Step {key}", TITLE_CHARS),
            body=_line(f"Step {key} of {plan.title or plan.plan_id} is done.", BODY_CHARS),
            at=first[key],
            project=plan.project,
            link=f"/p/{plan.project}/plans/{plan.plan_id}",
        )
        found.append(Found(item))
    return found


async def _steps(conn: AsyncConnection, bound: dict) -> list[Found]:
    """The steps done in the window, of the visible plans changed in it."""
    changed = await repository.changed_plans(conn, bound)
    if not changed:
        return []
    keys = [(plan.project_id, plan.plan_id) for plan in changed]
    by_plan = defaultdict(list)
    for revision in await repository.revisions(conn, keys, bound):
        by_plan[(revision.project_id, revision.plan_id)].append(revision)
    found = []
    for plan in changed:
        found += _steps_of(plan, by_plan[(plan.project_id, plan.plan_id)], bound["since"])
    return found


def _figure(value: float | None) -> str:
    return "no value" if value is None else f"{value:g}"


async def _problems(conn: AsyncConnection, bound: dict) -> list[Found]:
    """The problems the caller owns whose metric moved over METRIC_DAYS, measured last in the window."""
    now, found = bound["now"], []
    for row in await repository.owned_problems(conn, bound):
        metric = row.metric if isinstance(row.metric, dict) else None
        if metric is None:
            continue
        value = await measure(conn, row.project_id, metric, now, bounded=True)
        before = await measure(conn, row.project_id, metric, now - METRIC_DAYS, bounded=True)
        if value == before:
            continue
        last = await repository.last_measured(conn, row.project_id, metric, now)
        if last is None or last <= bound["since"]:
            continue
        code = problems.code(row.number)
        body = f"{problems.metric_text(metric)}: {_figure(before)} seven days ago, {_figure(value)} now."
        item = TodayItem(
            kind="problem",
            title=NOTIFIER.one_line(f"{code}: {row.statement}", TITLE_CHARS),
            body=_line(body, BODY_CHARS),
            at=last,
            project=row.project,
            link=f"/p/{row.project}/problems/{code}",
        )
        found.append(Found(item))
    return found


async def _proposals(conn: AsyncConnection, accesses: list[ProjectAccess], bound: dict) -> list[Found]:
    """The open proposals of the window in the projects the caller is an admin of, whose label their grant reaches."""
    admins = {access.project_id: access for access in accesses if holds(access.role, "admin")}
    if not admins:
        return []
    found = []
    for row in await repository.proposals(conn, list(admins), bound):
        access = admins[row.project_id]
        if not plan_service.sees(access, row.label, None):
            continue
        evidence = _count(row.evidence_count, "piece") + " of evidence"
        item = TodayItem(
            kind="proposal",
            title=NOTIFIER.one_line(row.title, TITLE_CHARS),
            body=f"A tier {row.tier} proposal of the Curator, with {evidence}, waits for an admin's answer.",
            at=row.created_at,
            project=access.name,
            link=proposal_link(row.id),
        )
        found.append(Found(item))
    return found


# The summary


def _count(number: int, word: str) -> str:
    return f"{number} {word}" + ("" if number == 1 else "s")


PHRASES = {  # each kind of item, one of it and some of it
    "release": ("a release", "{} releases"),
    "step_done": ("a plan step done", "{} plan steps done"),
    "run_failed": ("a failed run", "{} failed runs"),
    "run_parked": ("a parked run", "{} parked runs"),
    "problem": ("a problem whose metric moved", "{} problems whose metric moved"),
    "proposal": ("a proposal to answer", "{} proposals to answer"),
    "report": ("a report of your work", "{} reports of your work"),  # an item: a project and kind of notice
}


def _and(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def summary(waiting: int, found: list[Found], caught_up: bool, window: str) -> str:
    """One sentence from templates: how many questions wait for the caller, then what the items ``found`` are, by
    kind, ``window`` naming when they happened; "and more" when the items are not all there is."""
    if not waiting:
        asks = "Nothing waits for your answer"
    else:
        asks = f"{_count(waiting, 'question')} {'waits' if waiting == 1 else 'wait'} for your answer"
    if not found:
        return f"{asks}, and nothing changed {window}."
    counted = Counter(each.item.kind for each in found)
    parts = []
    for kind in ITEM_KINDS:
        if kind == "cost" and counted[kind]:
            parts.append(f"{curator.money(sum(each.cost_usd for each in found))} spent by your runs")
        elif counted[kind]:
            one, some = PHRASES[kind]
            parts.append(one if counted[kind] == 1 else some.format(counted[kind]))
    listed = _and(parts) + ("" if caught_up else ", and more")
    return f"{asks}. {window[0].upper()}{window[1:]}: {listed}."


# Today


def window_of(seen_at: datetime | None, now: datetime) -> tuple[datetime, str]:
    """(the start of the window, in words) for a member whose seen mark is ``seen_at``, at ``now`` (the module)."""
    if seen_at is None:
        return now - DEFAULT_WINDOW, NO_MARK
    if seen_at < now - MAX_WINDOW:
        return now - MAX_WINDOW, OLD_MARK
    return seen_at, SEEN


def _decision(row, wait: timedelta) -> TodayDecision:
    fields = dict(row._mapping)
    fields.pop("total")
    parks = parks_at(fields, wait)
    fields.pop("run_state")
    return TodayDecision(**fields, parks_at=parks, link=NOTIFIER.decision_link(fields["id"]))


async def _found(conn: AsyncConnection, accesses: list[ProjectAccess], bound: dict) -> list[Found]:
    """Every item of the window, the first in the order they happened, at most MAX_ITEMS + 1 of each source."""
    found = [_release(row) for row in await repository.source(conn, "release", bound)]
    found += await _steps(conn, bound)
    found += [_run(row) for row in await repository.source(conn, "run", bound)]
    found += await _problems(conn, bound)
    found += [_cost(row) for row in await repository.source(conn, "cost", bound)]
    found += await _proposals(conn, accesses, bound)
    found += [_report(row) for row in await repository.source(conn, "report", bound)]
    return sorted(found, key=lambda each: (each.item.at, ITEM_KINDS.index(each.item.kind), each.item.title))


async def compute(conn: AsyncConnection, user, wait: timedelta, *, now: datetime | None = None) -> Today:
    """Today of ``user`` (a Principal) at ``now`` (the database's now when None), on a hub whose decisions park after
    ``wait``, in the caller's transaction (the module)."""
    if now is None:
        now = await repository.now(conn)
    seen_at = await repository.seen_mark(conn, user.user_id)
    since, window = window_of(seen_at, now)
    accesses, pairs, granted, unplanned = await scope(conn, user)
    bound = {**parameters(user.user_id, pairs, granted, unplanned, since), "now": now}
    asked = await repository.needs_you(conn, bound)
    found = await _found(conn, accesses, bound)
    listed, caught_up = found[:MAX_ITEMS], len(found) <= MAX_ITEMS
    return Today(
        summary=summary(asked[0].total if asked else 0, listed, caught_up, window),
        since=since,
        seen_at=seen_at,
        seen_until=now if caught_up else listed[-1].item.at,
        needs_you=[_decision(row, wait) for row in asked],
        needs_you_count=asked[0].total if asked else 0,
        items=[each.item for each in listed],
        caught_up=caught_up,
    )


async def today(engine: AsyncEngine, user, wait: timedelta) -> Today:
    """Today of ``user`` now, in one transaction of its own."""
    async with UnitOfWork(engine) as conn:
        found = await compute(conn, user, wait)
    log.debug("today", extra={"login": user.login, "items": len(found.items), "needs_you": found.needs_you_count})
    return found


async def today_seen(engine: AsyncEngine, user_id: int, seen_at: datetime | None) -> datetime:
    """Move member ``user_id``'s seen mark forward to ``seen_at`` (``repository.mark_seen``); the mark now."""
    async with UnitOfWork(engine) as conn:
        return await repository.mark_seen(conn, user_id, seen_at)
