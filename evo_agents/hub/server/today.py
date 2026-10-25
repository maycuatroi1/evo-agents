"""Today, what waits for the caller and what changed since they last looked (plan hub-mobile-phase-0, criterion a7):
GET /v1/me/today and POST /v1/me/today/seen, with a web session or a machine token. ``docs/notifications.md`` is the
design; the name keeps clear of the Curator's morning brief (``brief``).

GET /v1/me/today is worked out at each call, over every project the caller holds a grant on, through the checks of
GET /v1/me/overview (``overview.scope``): a decision, run or plan of a plan whose label the caller's grant does not
reach is neither listed nor counted. It answers:

- ``needs_you``: the open decisions asked of the caller (``decisions.asked_person``), the oldest first, at most
  MAX_NEEDS_YOU, each with when its run parks for want of an answer (``overview.parks_at``); ``needs_you_count``
  counts them all;
- ``items``: at most MAX_ITEMS things that changed in the window, the first ones in the order they happened, each with
  its kind (ITEM_KINDS), a title, a body, its moment, its project and a link:

  - ``release``: a release cycle of a project that was published (``release_watch``);
  - ``step_done``: a step of a visible plan that is done now and was not in the revision the hub held at the start of
    the window (for a plan first written in the window, in its first revision), at the first revision since that has
    it done; a step's ``done_at`` is a date, too coarse for a mark that moves within a day;
  - ``run_failed`` and ``run_parked``: a run the caller dispatched that failed or was lost, or parked for want of an
    answer; the night shift's runs (``runs.CURATOR_KINDS``) are left to the Curator's morning brief;
  - ``problem``: an open problem the caller owns whose metric, measured as the Loop measures a goal's
    (``loop.view.measure``), differs from METRIC_DAYS ago, at the newest measurement of its key in the window;
  - ``cost``: what the runs the caller dispatched that ended in the window cost, one item a project, each agent
    session once at its largest total (``night_cost.usage_cost``);
  - ``proposal``: an open proposal of the Curator recorded in the window, in a project the caller is an admin of, and
    so may answer, whose label their grant reaches;

- ``caught_up``: whether ``items`` holds everything that changed in the window; ``seen_until``, the mark to post once
  every item listed was shown: the moment of the answer when it is caught up, else the moment of the last item;
- ``summary``: one sentence made from English templates (``summary``), no model.

The window starts at the caller's seen mark (``today_marks.seen_at``). Without a mark it is the last DEFAULT_WINDOW,
and a mark older than MAX_WINDOW counts from MAX_WINDOW ago. POST /v1/me/today/seen moves the mark forward to its
``seen_at`` (now when left out, never past now): a mark already later stays where it is. ``today_digest`` sends the
summary push of the same answer.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Text, and_, bindparam, case, cast, func, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, problems, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.db import one_of
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.decisions import asked_person
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.loop.view import measure
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.notifications import decision_link, one_line, run_link
from evo_agents.hub.server.overview import (
    UNPLANNED,
    VISIBLE,
    decision_visible,
    of_visible,
    parameters,
    parks_at,
    scope,
    shown_run,
)
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.release_cycles import releases_link
from evo_agents.hub.server.review_inbox import proposal_link
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

MAX_ITEMS = 8
MAX_NEEDS_YOU = 20
MAX_PROPOSALS = 200  # the open proposals of the window one answer reads, before their labels are checked
DEFAULT_WINDOW = timedelta(hours=24)  # the window of a member who has no seen mark yet
MAX_WINDOW = timedelta(days=7)  # a seen mark older than this counts from this long ago
METRIC_DAYS = timedelta(days=7)  # a problem's metric now is compared with its value this long ago
ITEM_KINDS = ("release", "step_done", "run_failed", "run_parked", "problem", "cost", "proposal")
FAILED = ("failed", "lost")  # a run of these ends is a run_failed item
TITLE_CHARS = 160
BODY_CHARS = 300
DONE_IDS = '$.steps[*] ? (@.status == "done").id'  # the ids of a plan body's steps that are done, as a JSON path
SEEN, NO_MARK, OLD_MARK = "since you last looked", "in the last 24 hours", "in the last 7 days"  # the window, in words

router = APIRouter(prefix="/v1/me", tags=["today"], responses={401: {"model": ErrorBody}})


# Models


class TodayDecision(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the run the decision belongs to")
    plan_id: str | None = Field(description="the run's plan when its agent asked; null for a run on none")
    plan_title: str | None
    step_key: str | None = Field(description="the step it is about, when the agent named one")
    category: Literal[runs.SHOWN_DECISION_CATEGORIES]
    question: str
    asked_at: datetime
    parks_at: datetime | None = Field(
        description="when the run parks for want of an answer: EVO_HUB_DECISION_WAIT_SECONDS after it started "
        "waiting, or when it parked; null while its agent still works, and for a local run"
    )
    link: str = Field(description="the web page that shows it and its answer form")


class TodayItem(BaseModel):
    kind: Literal[ITEM_KINDS] = Field(
        description="release: a release published; step_done: a plan step done; run_failed and run_parked: a run you "
        "dispatched failed or parked; problem: the metric of a problem you own moved; cost: what your runs cost; "
        "proposal: a proposal of the Curator you may answer"
    )
    title: str
    body: str | None
    at: datetime = Field(description="when it happened")
    project: str
    link: str = Field(description="the web page it is on")


class Today(BaseModel):
    summary: str = Field(description="one sentence of what waits and what changed, from a template")
    since: datetime = Field(description="the start of the window the items are counted from")
    seen_at: datetime | None = Field(description="your seen mark; null before your first POST /v1/me/today/seen")
    seen_until: datetime = Field(
        description="the seen mark to post once every item listed was shown: the moment of this answer when it is "
        "caught up, else the moment of the last item"
    )
    needs_you: list[TodayDecision] = Field(
        max_length=MAX_NEEDS_YOU, description=f"at most {MAX_NEEDS_YOU} open decisions asked of you, the oldest first"
    )
    needs_you_count: int = Field(description="all the open decisions asked of you")
    items: list[TodayItem] = Field(
        max_length=MAX_ITEMS,
        description=f"at most {MAX_ITEMS} things that changed since the window's start, the first in the order they "
        "happened",
    )
    caught_up: bool = Field(description="whether the items are everything that changed in the window")


class TodaySeenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seen_at: AwareDatetime | None = Field(
        None, description="what you saw up to, as seen_until gave it; now when left out; a moment past now is now"
    )


class TodaySeen(BaseModel):
    seen_at: datetime = Field(description="your seen mark now, which only moves forward")


@dataclass(frozen=True)
class Found:
    """An item, with what its cost item spent (0 for any other)."""

    item: TodayItem
    cost_usd: float = 0.0


# Queries


def _needs_you(visible, user_id, unplanned):
    """The first MAX_NEEDS_YOU open decisions asked of ``user_id`` that the caller reads (``decision_visible``), the
    oldest first, with TodayDecision's columns, what its parks_at is made of, and how many there are in all."""
    d, r, projects, plans = tables.decisions, tables.runs, tables.projects, tables.plans
    return (
        select(
            d.c.id,
            projects.c.name.label("project"),
            d.c.run_id,
            r.c.state.label("run_state"),
            r.c.kind.label("run_kind"),
            d.c.plan_id,
            plans.c.body["title"].astext.label("plan_title"),
            d.c.step_key,
            d.c.category,
            d.c.question,
            d.c.asked_at,
            r.c.waiting_since,
            r.c.parked_at,
            func.count().over().label("total"),
        )
        .select_from(
            d.join(r, r.c.id == d.c.run_id)
            .join(projects, projects.c.id == d.c.project_id)
            .outerjoin(plans, and_(plans.c.project_id == d.c.project_id, plans.c.plan_id == d.c.plan_id))
        )
        .where(d.c.state == "open", asked_person() == user_id, decision_visible(visible, unplanned, user_id))
        .order_by(d.c.asked_at, d.c.id)
        .limit(MAX_NEEDS_YOU)
    )


def _in_window(moment):
    """``moment`` falls in the window: after :since, and not after :now."""
    return and_(moment > bindparam("since"), moment <= bindparam("now"))


def _releases():
    """The release cycles of the projects :granted that were published in the window, with the tag that passed."""
    c, s, t, projects = tables.release_cycles, tables.release_schedules, tables.release_tags, tables.projects
    tag = (
        select(t.c.tag)
        .where(t.c.cycle_id == c.c.id, t.c.ci_state == "passed")
        .order_by(t.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(c.c.id, projects.c.name.label("project"), s.c.repo, tag.label("tag"), c.c.ended_at.label("at"))
        .join_from(c, s, s.c.id == c.c.schedule_id)
        .join(projects, projects.c.id == c.c.project_id)
        .where(one_of(c.c.project_id, name="granted"), c.c.state == "published", _in_window(c.c.ended_at))
        .order_by(c.c.ended_at, c.c.id)
        .limit(MAX_ITEMS + 1)
    )


def _runs(visible):
    """The runs of :user_id the caller sees that failed, were lost or parked in the window, but the night shift's."""
    r, projects = tables.runs, tables.projects
    user_id = bindparam("user_id")
    at = case((r.c.state == "parked", r.c.parked_at), else_=r.c.finished_at)
    failed = and_(one_of(r.c.state, FAILED), _in_window(r.c.finished_at))
    parked = and_(r.c.state == "parked", _in_window(r.c.parked_at))
    return (
        select(
            r.c.id,
            projects.c.name.label("project"),
            r.c.state,
            r.c.title,
            r.c.plan_id,
            r.c.error,
            at.label("at"),
        )
        .join_from(r, visible, of_visible(visible, r), isouter=True)
        .join(projects, projects.c.id == r.c.project_id)
        .where(
            shown_run(visible, user_id),
            r.c.dispatched_by == user_id,
            ~one_of(r.c.kind, runs.CURATOR_KINDS),
            or_(failed, parked),
        )
        .order_by(at, r.c.id)
        .limit(MAX_ITEMS + 1)
    )


def _costs(visible):
    """What the runs of :user_id the caller sees that ended in the window cost, a project at a time (each agent
    session once, at its largest total), with how many ended and when the last did; projects of no cost left out."""
    r, projects = tables.runs, tables.projects
    user_id = bindparam("user_id")
    session = func.coalesce(r.c.session_id, literal("run:") + cast(r.c.id, Text))
    per_session = (
        select(
            r.c.project_id,
            func.max(usage_cost(r.c.usage)).label("cost"),
            func.max(r.c.finished_at).label("at"),
            func.count().label("runs"),
        )
        .join_from(r, visible, of_visible(visible, r), isouter=True)
        .where(shown_run(visible, user_id), r.c.dispatched_by == user_id, _in_window(r.c.finished_at))
        .group_by(r.c.project_id, session)
        .subquery()
    )
    total = func.sum(per_session.c.cost)
    return (
        select(
            projects.c.name.label("project"),
            total.label("cost"),
            func.max(per_session.c.at).label("at"),
            func.sum(per_session.c.runs).label("runs"),
        )
        .join_from(per_session, projects, projects.c.id == per_session.c.project_id)
        .group_by(projects.c.name)
        .having(total > 0)
        .order_by(func.max(per_session.c.at))
        .limit(MAX_ITEMS + 1)
    )


def _changed_plans(visible):
    """The visible plans changed in the window: their project, title and steps."""
    p, projects = tables.plans, tables.projects
    return (
        select(
            p.c.project_id,
            projects.c.name.label("project"),
            p.c.plan_id,
            p.c.body["title"].astext.label("title"),
            p.c.body["steps"].label("steps"),
        )
        .join_from(p, visible, of_visible(visible, p))
        .join(projects, projects.c.id == p.c.project_id)
        .where(_in_window(p.c.updated_at))
    )


def _revisions(keys: list[tuple[int, str]]):
    """The revisions of the plans ``keys`` ((project_id, plan_id) each) the window needs, by plan and revision: the
    last one at or before :since, then each one after it, each with the ids of its steps that are done."""
    r = tables.plan_revisions
    earlier = r.alias("earlier")
    base = (
        select(func.max(earlier.c.revision))
        .where(
            earlier.c.project_id == r.c.project_id,
            earlier.c.plan_id == r.c.plan_id,
            earlier.c.created_at <= bindparam("since"),
        )
        .scalar_subquery()
    )
    done = func.jsonb_path_query_array(r.c.body, cast(literal(DONE_IDS), JSONPATH))
    return (
        select(r.c.project_id, r.c.plan_id, r.c.revision, r.c.created_at, done.label("done"))
        .where(
            tuple_(r.c.project_id, r.c.plan_id).in_(keys),
            or_(_in_window(r.c.created_at), r.c.revision == base),
        )
        .order_by(r.c.project_id, r.c.plan_id, r.c.revision)
    )


def _owned_problems():
    """The open problems of the projects :granted that :user_id owns and that name a metric, at their newest
    revision."""
    p, rv, projects = tables.problems, tables.problem_revisions, tables.projects
    return (
        select(p.c.project_id, projects.c.name.label("project"), p.c.number, rv.c.statement, rv.c.metric)
        .join_from(p, rv, and_(rv.c.problem_id == p.c.id, rv.c.revision == p.c.revision))
        .join(projects, projects.c.id == p.c.project_id)
        .where(
            one_of(p.c.project_id, name="granted"),
            rv.c.owner_id == bindparam("user_id"),
            rv.c.status == problems.OPEN,
            rv.c.metric.is_not(None),
        )
        .order_by(projects.c.name, p.c.number)
    )


def _last_measured(project_id: int, metric: dict, now: datetime):
    """When the key of ``metric`` was last measured up to ``now``, of its tenant when it names one."""
    k, s = tables.signal_keys, tables.signals
    key = select(k.c.id).where(k.c.project_id == project_id, k.c.name == metric.get("key")).scalar_subquery()
    where = [s.c.key_id == key, s.c.value.is_not(None), s.c.measured_at <= now]
    if metric.get("tenant"):
        where.append(s.c.tenant == metric["tenant"])
    return select(func.max(s.c.measured_at)).where(*where)


# Built once with bind parameters, as the overview's: each takes ``overview.parameters`` and :now.
_NEEDS_YOU = _needs_you(VISIBLE, bindparam("user_id"), UNPLANNED)
_RELEASES = _releases()
_RUNS = _runs(VISIBLE)
_COSTS = _costs(VISIBLE)
_CHANGED_PLANS = _changed_plans(VISIBLE)
_OWNED_PROBLEMS = _owned_problems()
_NOW = select(func.now())


# The items


def _line(text: str | None, limit: int) -> str | None:
    return one_line(text, limit) if text and text.strip() else None


def _release(row) -> Found:
    version = row.tag or "a new version"
    item = TodayItem(
        kind="release",
        title=one_line(f"Released {version} of {row.repo}", TITLE_CHARS),
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
        link=run_link(row.project, row.id),
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
            title=one_line(title or f"Step {key}", TITLE_CHARS),
            body=_line(f"Step {key} of {plan.title or plan.plan_id} is done.", BODY_CHARS),
            at=first[key],
            project=plan.project,
            link=f"/p/{plan.project}/plans/{plan.plan_id}",
        )
        found.append(Found(item))
    return found


async def _steps(conn: AsyncConnection, bound: dict) -> list[Found]:
    """The steps done in the window, of the visible plans changed in it."""
    changed = (await conn.execute(_CHANGED_PLANS, bound)).all()
    if not changed:
        return []
    keys = [(plan.project_id, plan.plan_id) for plan in changed]
    by_plan = defaultdict(list)
    for revision in (await conn.execute(_revisions(keys), {"since": bound["since"], "now": bound["now"]})).all():
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
    for row in (await conn.execute(_OWNED_PROBLEMS, bound)).all():
        metric = row.metric if isinstance(row.metric, dict) else None
        if metric is None:
            continue
        value = await measure(conn, row.project_id, metric, now, bounded=True)
        before = await measure(conn, row.project_id, metric, now - METRIC_DAYS, bounded=True)
        if value == before:
            continue
        last = (await conn.execute(_last_measured(row.project_id, metric, now))).scalar_one_or_none()
        if last is None or last <= bound["since"]:
            continue
        code = problems.code(row.number)
        body = f"{problems.metric_text(metric)}: {_figure(before)} seven days ago, {_figure(value)} now."
        item = TodayItem(
            kind="problem",
            title=one_line(f"{code}: {row.statement}", TITLE_CHARS),
            body=_line(body, BODY_CHARS),
            at=last,
            project=row.project,
            link=f"/p/{row.project}/problems/{code}",
        )
        found.append(Found(item))
    return found


async def _proposals(conn: AsyncConnection, accesses: list[ProjectAccess], bound: dict) -> list[Found]:
    """The open proposals of the window in the projects the caller is an admin of, whose label their grant reaches."""
    admins = {access.project_id: access for access in accesses if has_role(access.role, "admin")}
    if not admins:
        return []
    p = tables.proposals
    query = (
        select(p.c.id, p.c.project_id, p.c.title, p.c.tier, p.c.evidence_count, p.c.created_at, p.c.label)
        .where(one_of(p.c.project_id, list(admins)), p.c.state == "open", _in_window(p.c.created_at))
        .order_by(p.c.created_at, p.c.id)
        .limit(MAX_PROPOSALS)
    )
    found = []
    for row in (await conn.execute(query, {"since": bound["since"], "now": bound["now"]})).all():
        access = admins[row.project_id]
        if not access.visible(row.label, plan_routes._sink(access, None)):
            continue
        evidence = _count(row.evidence_count, "piece") + " of evidence"
        item = TodayItem(
            kind="proposal",
            title=one_line(row.title, TITLE_CHARS),
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


async def seen_mark(conn: AsyncConnection, user_id: int) -> datetime | None:
    t = tables.today_marks
    return (await conn.execute(select(t.c.seen_at).where(t.c.user_id == user_id))).scalar_one_or_none()


def _decision(row, wait: timedelta) -> TodayDecision:
    fields = dict(row._mapping)
    fields.pop("total")
    parks = parks_at(fields, wait)
    fields.pop("run_state")
    return TodayDecision(**fields, parks_at=parks, link=decision_link(fields["id"]))


async def _found(conn: AsyncConnection, accesses: list[ProjectAccess], bound: dict) -> list[Found]:
    """Every item of the window, the first in the order they happened, at most MAX_ITEMS + 1 of each source."""
    found = [_release(row) for row in (await conn.execute(_RELEASES, bound)).all()]
    found += await _steps(conn, bound)
    found += [_run(row) for row in (await conn.execute(_RUNS, bound)).all()]
    found += await _problems(conn, bound)
    found += [_cost(row) for row in (await conn.execute(_COSTS, bound)).all()]
    found += await _proposals(conn, accesses, bound)
    return sorted(found, key=lambda each: (each.item.at, ITEM_KINDS.index(each.item.kind), each.item.title))


async def compute(conn: AsyncConnection, user, wait: timedelta, *, now: datetime | None = None) -> Today:
    """Today of ``user`` (a Principal) at ``now`` (the database's now when None), on a hub whose decisions park after
    ``wait``, in the caller's transaction (the module)."""
    if now is None:
        now = (await conn.execute(_NOW)).scalar_one()
    seen_at = await seen_mark(conn, user.user_id)
    since, window = window_of(seen_at, now)
    accesses, pairs, granted, unplanned = await scope(conn, user)
    bound = {**parameters(user.user_id, pairs, granted, unplanned, since), "now": now}
    asked = (await conn.execute(_NEEDS_YOU, bound)).all()
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


async def mark_seen(conn: AsyncConnection, user_id: int, seen_at: datetime | None) -> datetime:
    """Move member ``user_id``'s seen mark forward to ``seen_at`` (now when None, never past now); the mark now."""
    t = tables.today_marks
    at = func.now() if seen_at is None else func.least(literal(seen_at, DateTime(timezone=True)), func.now())
    moved = pg_insert(t).values(user_id=user_id, seen_at=at)
    moved = moved.on_conflict_do_update(
        constraint="today_marks_pkey",
        set_={"seen_at": func.greatest(t.c.seen_at, moved.excluded.seen_at), "updated_at": func.now()},
    )
    return (await conn.execute(moved.returning(t.c.seen_at))).scalar_one()


@router.get("/today", response_model=Today)
async def today(request: Request, user: CurrentUser) -> Today:
    """What waits for you, and what changed since you last looked, over the projects you hold a grant on."""
    wait = timedelta(seconds=request.app.state.config.decision_wait_seconds)
    async with request.app.state.engine.begin() as conn:
        found = await compute(conn, user, wait)
    log.debug("today", extra={"login": user.login, "items": len(found.items), "needs_you": found.needs_you_count})
    return found


@router.post("/today/seen", response_model=TodaySeen, responses={422: {"model": ErrorBody}})
async def today_seen(request: Request, user: CurrentUser, body: TodaySeenIn | None = None) -> TodaySeen:
    """Move your seen mark forward to what you saw: Today counts what changes after it."""
    async with request.app.state.engine.begin() as conn:
        moved = await mark_seen(conn, user.user_id, body.seen_at if body is not None else None)
    return TodaySeen(seen_at=moved)
