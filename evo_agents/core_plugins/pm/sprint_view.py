"""The active sprint in the daily report: how much of the scope it opened with is done, what was added and dropped
since, how much of its time has gone, and the progress of each use case it works on.

The scope at opening is rebuilt from the history: a task was in the sprint when it existed at the opening and the
last ``sprint`` row of its history up to then names the sprint (with no row up to then, the first row after it says
which sprint the task came from; with none at all, the task's sprint now). The sprint opened when it turned active
(``tasks_sprints.activated_at``), else at the start of its ``starts_on`` day in the project's time zone, else when it
was created. Cancelled tasks leave the denominator.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from evo_agents.core_plugins.pm.facts import CANCELLED, DONE, Inputs, Sprint, Task
from evo_agents.core_plugins.pm.workdays import Calendar


def opened_at(sprint: Sprint, calendar: Calendar) -> datetime:
    if sprint.activated_at is not None:
        return sprint.activated_at
    if sprint.starts_on is not None:
        return calendar.start_of(sprint.starts_on)
    return sprint.created_at


def in_sprint_at(task: Task, name: str, moment: datetime) -> bool:
    """Whether ``task`` was in sprint ``name`` at ``moment``."""
    if task.created_at > moment:
        return False
    moves = sorted((e for e in task.events if e.field == "sprint"), key=lambda e: e.at)
    before = [e for e in moves if e.at <= moment]
    if before:
        return before[-1].to_value == name
    if moves:
        return moves[0].from_value == name
    return task.sprint == name


def _percent(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _progress(tasks: list[Task]) -> tuple[int, int]:
    """(done, counted): the tasks done, and the tasks that are not cancelled."""
    counted = [task for task in tasks if task.group != CANCELLED]
    return sum(task.group == DONE for task in counted), len(counted)


def _time(sprint: Sprint, calendar: Calendar, start: date, today: date) -> tuple[int | None, int | None]:
    """(working days in the sprint, working days gone before ``today``)."""
    if sprint.ends_on is None:
        return None, None
    total = calendar.span(start, sprint.ends_on)
    gone = calendar.span(start, min(today - timedelta(days=1), sprint.ends_on))
    return total, gone


def _cases(tasks: list[Task]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for task in tasks:
        if task.group == CANCELLED:
            continue
        for case in task.use_cases:
            entry = found.setdefault(case.code, {"code": case.code, "title": case.title, "done": 0, "total": 0})
            entry["done"] += task.group == DONE
            entry["total"] += 1
            entry["visible"] = case.visible
    return found


def sprint(data: Inputs, today: date, hidden: dict) -> dict | None:
    """The sprint section; None when the project has no active sprint."""
    current = data.sprint
    if current is None:
        return None
    calendar = data.config.calendar
    moment = opened_at(current, calendar)
    scope = [task for task in data.tasks if in_sprint_at(task, current.name, moment)]
    now = [task for task in data.tasks if task.sprint == current.name]
    opening, held = {t.number for t in scope}, {t.number for t in now}
    done, counted = _progress(scope)
    start = calendar.local(moment)
    total, gone = _time(current, calendar, start, today)
    cases = list(_cases(now).values())
    hidden["sprint"] = hidden.get("sprint", 0) + sum(not case["visible"] for case in cases)
    seen = [{key: value for key, value in case.items() if key != "visible"} for case in cases if case["visible"]]
    return {
        "name": current.name,
        "starts_on": current.starts_on,
        "ends_on": current.ends_on,
        "opened_at": moment,
        "scope": len(scope),
        "done_percent": _percent(done, counted),
        "added": len(held - opening),
        "removed": len(opening - held),
        "working_days": total,
        "working_days_gone": gone,
        "time_percent": _percent(gone, total or 0),
        "use_cases": seen,
    }


__all__ = ["in_sprint_at", "opened_at", "sprint"]
