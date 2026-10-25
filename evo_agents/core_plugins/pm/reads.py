"""Reading the facts of a project from the hub through the core plugin API: the tasks the daily report is made of
(those of the plugin tasks), and what each shows of the work on it: its people, events, comments, runs, the plan steps
it links, its blockers and the use cases of the plugin srs it links. pm owns no table of these and imports neither
tasks nor srs.

A task, a use case and a plan carry a label, and a reader sees them through the sink X-Evo-Sink names, else the
project's hub sink, as the routes of tasks show them (``hub.read_visibility``). What the reader may not see is still
read, with ``visible`` false, so the figures count it and the lists leave it out. The activity of a plan the reader
may not see counts for nothing: it keeps no task alive in their report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.pm import settings
from evo_agents.core_plugins.pm.facts import (
    DECISION,
    MANUAL,
    Blocker,
    Config,
    Event,
    Inputs,
    Member,
    Sprint,
    Task,
    UseCase,
)
from evo_agents.hub.server import core_plugin_api as hub

SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER
MAX_ROWS = 5000  # the most rows a read of the hub returns


@dataclass(frozen=True)
class Reader:
    """Who reads a project's report: the project, their role there (None without a grant) and how they see its
    labelled data."""

    project: str
    role: str | None
    sees: hub.Visibility

    def visible(self, label) -> bool:
        return self.sees.visible(label)


async def reader_of(
    conn: AsyncConnection, project: str, role: str | None, max_level: str | None, sink: str | None
) -> Reader:
    """The reader of ``project`` with a grant of ``role`` up to ``max_level`` (both None without one), through
    ``sink``, else the project's hub sink."""
    sees = await hub.read_visibility(conn, settings.handle(), project, max_level=max_level, sink=sink)
    return Reader(project, role, sees)


async def reader_for(conn: AsyncConnection, ref: hub.ProjectRef, sink: str | None) -> Reader:
    """The caller ``project_access`` found: a hub admin without a grant (no max level) holds none here."""
    role = ref.role if ref.max_level is not None else None
    return await reader_of(conn, ref.name, role, ref.max_level, sink)


def reader(access: Reader) -> None:
    """Reading needs a grant on the project (a hub admin without one gets this far only)."""
    if access.role is None:
        raise settings.PmError(403, f"reading the daily report of project {access.project} needs a grant on it")


def _people(rows) -> dict[int, dict[str, list[str]]]:
    found: dict[int, dict[str, list[str]]] = {}
    for row in rows:
        found.setdefault(row.task_id, {"assignee": [], "qa": []})[row.role].append(row.login)
    return found


def _events(rows) -> dict[int, list[Event]]:
    found: dict[int, list[Event]] = {}
    for row in rows:
        found.setdefault(row.task_id, []).append(Event(row.field, row.from_value, row.to_value, row.at))
    return found


async def _plan_steps(conn: AsyncConnection, access: Reader, ids: list[int], since: datetime) -> dict[int, datetime]:
    """task id -> the latest time the status of a plan step it links changed, in a plan the reader sees."""
    plugin = settings.handle()
    links = [
        link
        for link in await hub.read_task_links(conn, plugin, access.project, ids)
        if link.kind == "plan_step" and link.plan_id is not None
    ]
    if not links:
        return {}
    wanted = {link.plan_id for link in links}
    labels = {plan.plan_id: plan.label for plan in await hub.read_plans(conn, plugin, access.project)}
    seen = sorted(plan_id for plan_id in wanted if plan_id in labels and access.visible(labels[plan_id]))
    changed: dict[tuple[str, str], list[datetime]] = {}
    for change in await hub.read_plan_step_changes(conn, plugin, access.project, seen, since=since):
        changed.setdefault((change.plan_id, change.step_key), []).append(change.changed_at)
    found: dict[int, datetime] = {}
    for link in links:
        for at in changed.get((link.plan_id, link.step_id), ()):
            found[link.task_id] = max(found.get(link.task_id, at), at)
    return found


async def _blockers(conn: AsyncConnection, project: str, prefix: str, ids: list[int]) -> dict[int, list[Blocker]]:
    """task id -> its open blockers: those set by hand, then the open decisions of its runs, as tasks shows them."""
    plugin = settings.handle()
    found: dict[int, list[Blocker]] = {}
    for row in await hub.read_task_blockers(conn, plugin, project, ids):
        if row.waiting_login is not None:
            kind, on = "member", row.waiting_login
        elif row.waiting_task_number is not None:
            kind, on = "task", f"{prefix}-{row.waiting_task_number}"
        else:
            kind, on = "text", row.waiting_text
        found.setdefault(row.task_id, []).append(
            Blocker(MANUAL, row.reason, kind, on, row.since, row.set_by, None, None, row.id)
        )
    for row in await hub.read_task_decisions(conn, plugin, project, ids, states=("open",)):
        view = Blocker(
            DECISION, row.question, "member", row.dispatched_by, row.asked_at, None, row.decision_id, row.run_id
        )
        found.setdefault(row.task_id, []).append(view)
    return found


async def _use_cases(conn: AsyncConnection, access: Reader, ids: list[int]) -> dict[int, list[UseCase]]:
    """task id -> the use cases of the plugin srs it links to."""
    found: dict[int, list[UseCase]] = {}
    for row in await hub.read_task_srs_items(conn, settings.handle(), access.project, ids):
        if row.kind == "use_case" and row.retired_in is None and row.code:
            case = UseCase(row.code, row.title or "", access.visible(row.label))
            found.setdefault(row.task_id, []).append(case)
    return found


async def _members(conn: AsyncConnection, project: str) -> tuple[Member, ...]:
    grants = await hub.read_grants(conn, settings.handle(), project)
    return tuple(Member(grant.login, grant.role) for grant in grants)


async def _sprint(conn: AsyncConnection, project: str) -> Sprint | None:
    found = await hub.read_sprint_details(conn, settings.handle(), project, states=("active",))
    if not found:
        return None
    row = found[-1]  # the oldest active sprint
    return Sprint(row.name, row.created_at, row.starts_on, row.ends_on, row.activated_at)


def _earlier(config: Config, today: date) -> datetime:
    """The start of the working day ``stale_days`` + 1 working days before ``today``: what plan revisions are read from."""
    day = today
    for _ in range(config.stale_days + 1):
        day = config.calendar.previous(day)
    return config.calendar.start_of(day)


async def load(conn: AsyncConnection, access: Reader, config: Config, today: date) -> Inputs:
    """The facts of the project for the report of ``today``, as the reader sees them."""
    reader(access)
    plugin, project = settings.handle(), access.project
    prefix, groups = await settings.task_groups(conn, project)
    rows = await hub.read_task_details(conn, plugin, project, limit=MAX_ROWS)
    ids = [row.id for row in rows]
    people = _people(await hub.read_task_people(conn, plugin, project, ids, roles=("assignee", "qa")))
    events = _events(await hub.read_task_events(conn, plugin, project, ids))
    activity_of = {row.task_id: row for row in await hub.read_task_activity(conn, plugin, project, ids)}
    steps = await _plan_steps(conn, access, ids, _earlier(config, today))
    blockers = await _blockers(conn, project, prefix, ids)
    cases = await _use_cases(conn, access, ids) if await settings.srs_enabled(conn, project) else {}
    tasks = []
    for row in rows:
        held = people.get(row.id, {"assignee": [], "qa": []})
        moved = activity_of.get(row.id)
        activity = {
            "comment": moved.commented_at if moved else None,
            "run": moved.run_at if moved else None,
            "plan_step": steps.get(row.id),
        }
        tasks.append(
            Task(
                ref=f"{prefix}-{row.number}",
                number=row.number,
                title=row.title,
                status=row.status,
                group=groups.get(row.status, "todo"),
                created_at=row.created_at,
                due_on=row.due_on,
                sprint=row.sprint,
                description_md=row.description_md,
                outcome_md=row.outcome_md,
                frame=row.frame,
                visible=access.visible(row.label),
                assignees=tuple(held["assignee"]),
                qa=tuple(held["qa"]),
                events=tuple(events.get(row.id, ())),
                activity={source: at for source, at in activity.items() if at is not None},
                blockers=tuple(blockers.get(row.id, ())),
                use_cases=tuple(cases.get(row.id, ())),
            )
        )
    return Inputs(project, config, groups, tuple(tasks), await _sprint(conn, project), await _members(conn, project))


__all__ = ["SINK_HEADER", "Reader", "load", "reader", "reader_for", "reader_of"]
