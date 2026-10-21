"""The daily report of a project, and one member's view of it: pure functions of the facts (``facts``) and a date.

"Yesterday" is the working day before the date, and the window runs from the start of that day to the start of the
date, in the project's time zone, so a Monday's report holds the weekend. Every count of days is in working days
(``workdays``). A task the reader may not see adds to the counts of ``hidden`` and to the figures of the sprint, and
appears in no list.
"""

from __future__ import annotations

from datetime import date, datetime

from evo_agents.core_plugins.pm import sprint_view
from evo_agents.core_plugins.pm.facts import (
    CANCELLED,
    DOING,
    DONE,
    REVIEW,
    TODO,
    Blocker,
    Inputs,
    Task,
)
from evo_agents.core_plugins.pm.outcome import outcome_of
from evo_agents.hub.access import has_role

SECTIONS = ("done_use_cases", "done_tasks", "sprint", "late", "review", "stale", "blocked", "escalate")
HISTORY, CREATED = "history", "created"


class Daily:
    """The sections of one date's report. ``hidden`` counts what each section left out for the reader."""

    def __init__(self, data: Inputs, today: date, sections: tuple[str, ...] = SECTIONS):
        self.data, self.today = data, today
        self.cal = data.config.calendar
        self.previous = self.cal.previous(today)
        self.window = (self.cal.start_of(self.previous), self.cal.start_of(today))
        self.hidden = {name: 0 for name in sections}

    def shown(self, section: str, pairs: list[tuple[bool, dict]]) -> list[dict]:
        """The entries of ``pairs`` the reader sees (the first of each pair); the others count in ``hidden``."""
        self.hidden[section] = self.hidden.get(section, 0) + sum(not seen for seen, _ in pairs)
        return [entry for seen, entry in pairs if seen]

    def _entered(self, task: Task, group: str) -> datetime | None:
        """When the task last moved to a status of ``group``."""
        groups = self.data.groups
        times = [e.at for e in task.events if e.field == "status" and groups.get(e.to_value or "") == group]
        return max(times) if times else None

    # What was done

    def _done_at(self, task: Task) -> datetime | None:
        """When the task moved to done inside the window and is done still; None otherwise."""
        if task.group != DONE:
            return None
        groups, start, end = self.data.groups, *self.window
        moves = [
            e.at
            for e in task.events
            if e.field == "status"
            and start <= e.at < end
            and groups.get(e.to_value or "") == DONE
            and groups.get(e.from_value or "") != DONE
        ]
        return max(moves) if moves else None

    def done_tasks(self) -> list[dict]:
        pairs = []
        for task in self.data.tasks:
            at = self._done_at(task)
            if at is not None:
                pairs.append((task.visible, {**_who(task), "done_at": at}))
        return self.shown("done_tasks", pairs)

    def _subject(self, tasks: list[Task]) -> Task | None:
        """The task whose state is the use case's: the latest frame task, else the task opened last that is not
        cancelled."""
        frames = [task for task in tasks if task.frame]
        if frames:
            return max(frames, key=lambda task: task.number)
        live = [task for task in tasks if task.group != CANCELLED]
        return max(live, key=lambda task: (task.created_at, task.number)) if live else None

    def done_use_cases(self) -> list[dict]:
        by_code: dict[str, list[Task]] = {}
        for task in self.data.tasks:
            for case in task.use_cases:
                by_code.setdefault(case.code, []).append(task)
        pairs = []
        for code, tasks in sorted(by_code.items()):
            subject = self._subject(tasks)
            at = self._done_at(subject) if subject else None
            if subject is None or at is None:
                continue
            case = next(c for c in subject.use_cases if c.code == code)
            entry = {"code": code, "title": case.title, "task": subject.ref, "done_at": at}
            entry["outcome"] = outcome_of(subject.description_md)
            pairs.append((subject.visible and case.visible, entry))
        return self.shown("done_use_cases", pairs)

    # What needs attention

    def late(self, tasks: list[Task] | None = None) -> list[dict]:
        cal, config, pairs = self.cal, self.data.config, []
        for task in self.data.tasks if tasks is None else tasks:
            if not task.open or task.due_on is None:
                continue
            if task.due_on < self.today:
                late = cal.elapsed(task.due_on, self.today)
                pairs.append((task.visible, self._due(task, "overdue", late, None)))
            elif task.group == TODO and cal.span(self.today, task.due_on) <= config.at_risk_days:
                left = cal.span(self.today, task.due_on)
                pairs.append((task.visible, self._due(task, "at_risk", None, left)))
        return self.shown("late", pairs)

    def _due(self, task: Task, kind: str, late: int | None, left: int | None) -> dict:
        return {**_who(task), "due_on": task.due_on, "kind": kind, "days_late": late, "days_left": left}

    def review(self, tasks: list[Task] | None = None) -> list[dict]:
        pairs = []
        for task in self.data.tasks if tasks is None else tasks:
            if task.group != REVIEW:
                continue
            since = self._entered(task, REVIEW) or task.created_at
            age = self.cal.elapsed(self.cal.local(since), self.today)
            pairs.append((task.visible, {**_who(task), "qa": list(task.qa), "since": since, "age_days": age}))
        return self.shown("review", pairs)

    def _last_activity(self, task: Task) -> tuple[datetime, str]:
        found = [(e.at, HISTORY) for e in task.events]
        found += [(at, source) for source, at in task.activity.items()]
        return max(found, key=lambda item: item[0]) if found else (task.created_at, CREATED)

    def stale(self) -> list[dict]:
        pairs = []
        for task in self.data.tasks:
            if task.group != DOING:
                continue
            at, source = self._last_activity(task)
            idle = self.cal.elapsed(self.cal.local(at), self.today)
            if idle > self.data.config.stale_days:
                pairs.append(
                    (task.visible, {**_who(task), "last_activity_at": at, "source": source, "idle_days": idle})
                )
        return self.shown("stale", pairs)

    def _blocker(self, task: Task, blocker: Blocker) -> dict:
        age = self.cal.elapsed(self.cal.local(blocker.since), self.today)
        return {
            "ref": task.ref,
            "title": task.title,
            "kind": blocker.kind,
            "reason": blocker.reason,
            "waiting_kind": blocker.waiting_kind,
            "waiting_on": blocker.waiting_on,
            "since": blocker.since,
            "set_by": blocker.set_by,
            "age_days": age,
            "decision_id": blocker.decision_id,
            "run_id": blocker.run_id,
        }

    def _blockers(self, tasks: list[Task] | None = None) -> list[tuple[bool, dict]]:
        return [
            (t.visible, self._blocker(t, b))
            for t in self.data.tasks
            if tasks is None or t in tasks
            if t.open
            for b in t.blockers
        ]

    def blocked(self, section: str = "blocked", tasks: list[Task] | None = None) -> list[dict]:
        return self.shown(section, self._blockers(tasks))

    def escalate(self) -> list[dict]:
        limit, pm = self.data.config.escalate_after_days, self.data.config.pm
        pairs = [(seen, {**e, "escalate_to": pm}) for seen, e in self._blockers() if e["age_days"] > limit]
        return self.shown("escalate", pairs)

    def load(self) -> list[dict]:
        config, found = self.data.config, []
        for member in self.data.members:
            if not has_role(member.role, "writer"):
                continue
            mine = [t for t in self.data.tasks if member.login in t.assignees]
            doing = [t for t in mine if t.group == DOING]
            late = [t for t in mine if t.open and t.due_on is not None and t.due_on < self.today]
            as_qa = [t for t in self.data.tasks if t.group == REVIEW and member.login in t.qa]
            found.append(
                {
                    "login": member.login,
                    "role": member.role,
                    "doing": len(doing),
                    "wip_limit": config.wip_limit,
                    "overloaded": len(doing) > config.wip_limit,
                    "empty": not doing,
                    "review_as_qa": len(as_qa),
                    "late": len(late),
                    "doing_tasks": [t.ref for t in doing if t.visible],
                }
            )
        return found

    def report(self) -> dict:
        sprint = sprint_view.sprint(self.data, self.today, self.hidden)
        return {
            "project": self.data.project,
            "date": self.today,
            "previous_working_day": self.previous,
            "done_use_cases": self.done_use_cases(),
            "done_tasks": self.done_tasks(),
            "sprint": sprint,
            "late": self.late(),
            "review": self.review(),
            "stale": self.stale(),
            "blocked": self.blocked(),
            "escalate": self.escalate(),
            "load": self.load(),
            "hidden": self.hidden,
        }


def _who(task: Task) -> dict:
    return {"ref": task.ref, "title": task.title, "assignees": list(task.assignees)}


def escalations(data: Inputs, today: date) -> list[tuple[Task, Blocker, int]]:
    """(task, blocker, working days it has lasted) of each blocker of an open task that has lasted longer than the
    config's ``escalate_after_days``, as ``Daily.escalate`` lists them but whatever the reader sees."""
    cal, limit = data.config.calendar, data.config.escalate_after_days
    found = []
    for task in data.tasks:
        for blocker in task.blockers if task.open else ():
            age = cal.elapsed(cal.local(blocker.since), today)
            if age > limit:
                found.append((task, blocker, age))
    return found


def daily(data: Inputs, today: date) -> dict:
    """The daily report of the project on ``today``."""
    return Daily(data, today).report()


def member(data: Inputs, today: date, login: str) -> dict:
    """What ``login`` has on ``today``: the tasks they do and review, those late, the blockers on their tasks and the
    blockers that wait on them. ``hidden`` counts only what concerns them."""
    day = Daily(data, today, ("doing", "review", "late", "blocked", "blocking"))
    mine = [t for t in data.tasks if login in t.assignees]
    reviewing = [t for t in data.tasks if login in t.assignees or login in t.qa]
    doing = [(t.visible, _who(t)) for t in mine if t.group == DOING]
    waiting = [t for t in data.tasks if any(b.waiting_kind == "member" and b.waiting_on == login for b in t.blockers)]
    on_me = [
        (seen, e) for seen, e in day._blockers(waiting) if e["waiting_kind"] == "member" and e["waiting_on"] == login
    ]
    return {
        "project": data.project,
        "date": today,
        "login": login,
        "doing": day.shown("doing", doing),
        "review": day.review(reviewing),
        "late": day.late(mine),
        "blocked": day.blocked("blocked", mine),
        "blocking": day.shown("blocking", on_me),
        "hidden": day.hidden,
    }


__all__ = ["Daily", "daily", "escalations", "member"]
