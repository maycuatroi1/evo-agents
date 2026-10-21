"""The pure computation of the daily report of the core plugin pm (``evo_agents.core_plugins.pm.report``), as criteria
a3, a4 and a5 of plan pm-daily-tracking name it. No database and no clock: the facts are built by hand and the date is
passed in. The project counts in Asia/Ho_Chi_Minh (UTC+7), Monday to Friday; 2026-10-19 is a Monday.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from evo_agents.core_plugins.pm import report
from evo_agents.core_plugins.pm.facts import (
    Blocker,
    Config,
    Event,
    Inputs,
    Member,
    Sprint,
    Task,
    UseCase,
)
from evo_agents.core_plugins.pm.workdays import Calendar

ZONE = ZoneInfo("Asia/Ho_Chi_Minh")
CALENDAR = Calendar(ZONE, frozenset({1, 2, 3, 4, 5}))
GROUPS = {"todo": "todo", "doing": "doing", "review": "review", "done": "done", "cancelled": "cancelled"}
MONDAY, TUESDAY, WEDNESDAY = date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)
FRIDAY, SATURDAY, SUNDAY = date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 18)
LONG_AGO = datetime(2026, 9, 1, tzinfo=UTC)


def at(day: date, hour: int = 9) -> datetime:
    """``hour`` o'clock on ``day`` in the project's zone."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=ZONE).astimezone(UTC)


def moved(day: date, to: str, before: str = "todo", hour: int = 9) -> Event:
    return Event("status", before, to, at(day, hour))


def task(number: int, status: str = "todo", **given) -> Task:
    return Task(
        ref=f"T-{number}",
        number=number,
        title=given.pop("title", f"Task {number}"),
        status=status,
        group=GROUPS[status],
        created_at=given.pop("created_at", LONG_AGO),
        **given,
    )


def facts(*tasks: Task, sprint: Sprint | None = None, members=(), **config) -> Inputs:
    return Inputs("shop", Config(CALENDAR, **config), GROUPS, tuple(tasks), sprint, tuple(members))


def refs(entries: list[dict]) -> list[str]:
    return [entry["ref"] for entry in entries]


def test_pm_daily_a_monday_reports_friday_saturday_and_sunday():
    inside = [
        task(1, "done", events=(moved(FRIDAY, "done", hour=0),)),  # the first minute of the window
        task(2, "done", events=(moved(SATURDAY, "done"),)),
        task(3, "done", events=(moved(SUNDAY, "done", hour=23),)),
    ]
    outside = [
        task(4, "done", events=(moved(date(2026, 10, 15), "done", hour=23),)),  # Thursday evening
        task(5, "done", events=(moved(MONDAY, "done", hour=0),)),  # the first minute of Monday
        task(6, "done", events=(moved(SATURDAY, "done"), moved(SUNDAY, "todo", "done"))),  # reopened: not done now
    ]
    got = report.daily(facts(*inside, *outside[:2], replace(outside[2], status="todo", group="todo")), MONDAY)
    assert got["previous_working_day"] == FRIDAY
    assert refs(got["done_tasks"]) == ["T-1", "T-2", "T-3"]
    tuesday = report.daily(facts(*inside, *outside[:2]), TUESDAY)
    assert tuesday["previous_working_day"] == MONDAY
    assert refs(tuesday["done_tasks"]) == ["T-5"]


def test_pm_daily_a_use_case_is_done_through_its_frame_task_and_shows_its_outcome():
    case = UseCase("ORD-3", "Cancel an order")
    done = "x\n\n## Outcome\nOrders cancel in one tap.\n\n## Notes\nlater"
    frame = task(10, "done", frame=True, use_cases=(case,), description_md=done, events=(moved(SATURDAY, "done"),))
    child = task(11, "todo", use_cases=(case,), created_at=at(SATURDAY))
    other = task(12, "done", use_cases=(UseCase("ORD-4", "Pay"),), events=(moved(date(2026, 10, 14), "done"),))
    got = report.daily(facts(frame, child, other), MONDAY)
    assert [(c["code"], c["task"], c["outcome"]) for c in got["done_use_cases"]] == [
        ("ORD-3", "T-10", "Orders cancel in one tap.")
    ]


def test_pm_daily_the_sprint_scope_is_the_one_it_opened_with_whatever_moved_after():
    opened = at(date(2026, 10, 12))
    sprint = Sprint("Sprint 7", LONG_AGO, date(2026, 10, 12), date(2026, 10, 23), activated_at=opened)
    moves = lambda *rows: tuple(Event("sprint", a, b, when) for a, b, when in rows)  # noqa: E731
    kept = task(1, "done", sprint="Sprint 7", events=(moved(SATURDAY, "done"),))
    dropped = task(2, sprint=None, events=moves(("Sprint 7", None, at(SATURDAY))))
    added = task(3, sprint="Sprint 7", events=moves((None, "Sprint 7", at(SATURDAY))))
    other = task(4, sprint="Sprint 8", events=moves(("Sprint 7", "Sprint 8", at(FRIDAY))))  # left before? no: after
    unborn = task(5, sprint="Sprint 7", created_at=at(SATURDAY))
    cancelled = task(6, "cancelled", sprint="Sprint 7")
    got = report.daily(facts(kept, dropped, added, other, unborn, cancelled, sprint=sprint), MONDAY)["sprint"]
    assert got["scope"] == 4  # kept, dropped, other, cancelled
    assert (got["added"], got["removed"]) == (2, 2)
    assert got["done_percent"] == round(100 / 3, 1)  # the cancelled task leaves the whole
    assert got["opened_at"] == opened
    assert (got["working_days"], got["working_days_gone"]) == (10, 5)
    assert got["time_percent"] == 50.0


def test_pm_daily_the_sprint_opens_at_its_first_day_or_its_creation_when_it_was_never_activated():
    first = Sprint("S", at(date(2026, 10, 1)), date(2026, 10, 19), date(2026, 10, 23))
    early = task(1, sprint="S", created_at=at(date(2026, 10, 10)))
    late = task(2, sprint="S", created_at=at(date(2026, 10, 19), 8))
    got = report.daily(facts(early, late, sprint=first), TUESDAY)["sprint"]
    assert got["opened_at"] == at(date(2026, 10, 19), 0)
    assert (got["scope"], got["added"]) == (1, 1)
    made = replace(first, starts_on=None, ends_on=None)
    got = report.daily(facts(early, late, sprint=made), TUESDAY)["sprint"]
    assert got["opened_at"] == made.created_at and got["working_days"] is None and got["time_percent"] is None
    assert report.daily(facts(early), TUESDAY)["sprint"] is None


def test_pm_daily_late_and_at_risk_are_counted_in_working_days_across_a_weekend():
    tasks = [
        task(1, "doing", due_on=FRIDAY),  # Monday: 1 working day late (Mon), not the 3 calendar days
        task(2, "todo", due_on=date(2026, 10, 14)),  # Wednesday of last week: 3 working days late
        task(3, "todo", due_on=TUESDAY),  # at risk: Monday and Tuesday left
        task(4, "todo", due_on=WEDNESDAY),  # Monday, Tuesday, Wednesday are 3 days left: not at risk yet
        task(5, "doing", due_on=TUESDAY),  # in progress and not past due: neither
        task(6, "done", due_on=FRIDAY),
        task(7, "todo", due_on=MONDAY),  # due today: at risk with 1 day left
    ]
    got = report.daily(facts(*tasks), MONDAY)
    assert {e["ref"]: (e["kind"], e["days_late"], e["days_left"]) for e in got["late"]} == {
        "T-1": ("overdue", 1, None),
        "T-2": ("overdue", 3, None),
        "T-3": ("at_risk", None, 2),
        "T-7": ("at_risk", None, 1),
    }
    friday_view = report.daily(facts(task(8, "todo", due_on=date(2026, 10, 19))), FRIDAY)["late"]
    assert (
        friday_view[0]["kind"] == "at_risk" and friday_view[0]["days_left"] == 2
    )  # Friday and Monday; the weekend is no days


def test_pm_daily_review_age_counts_working_days_since_it_entered_review():
    entered = task(1, "review", qa=("bao",), events=(moved(FRIDAY, "review", "doing", 17),))
    weekend = task(2, "review", events=(moved(SATURDAY, "review", "doing"),))
    today = task(3, "review", events=(moved(MONDAY, "review", "doing"),))
    bounced = task(
        4, "review", events=(moved(date(2026, 10, 12), "review", "doing"), moved(FRIDAY, "review", "doing", 10))
    )
    unrecorded = task(5, "review", created_at=at(date(2026, 10, 14)))
    got = {e["ref"]: e for e in report.daily(facts(entered, weekend, today, bounced, unrecorded), MONDAY)["review"]}
    assert {ref: e["age_days"] for ref, e in got.items()} == {"T-1": 1, "T-2": 1, "T-3": 0, "T-4": 1, "T-5": 3}
    assert got["T-1"]["qa"] == ["bao"] and got["T-1"]["since"] == at(FRIDAY, 17)


def test_pm_daily_stale_is_idle_time_since_the_latest_activity_of_any_source():
    comment = at(date(2026, 10, 15))
    old = at(date(2026, 10, 12))
    tasks = [
        task(1, "doing", events=(moved(date(2026, 10, 14), "doing"),)),  # history, Wednesday
        task(2, "doing", activity={"comment": comment}),
        task(3, "doing", events=(Event("title", "a", "b", old),), activity={"run": at(FRIDAY), "comment": old}),
        task(4, "doing", activity={"plan_step": at(date(2026, 10, 14))}),
        task(5, "doing", created_at=at(date(2026, 10, 14))),  # nothing happened since it was made
        task(6, "todo", events=(moved(old, "todo"),)),  # not in progress: never stale
        task(7, "doing", events=(moved(SATURDAY, "doing"),)),  # fresh
    ]
    got = {e["ref"]: (e["source"], e["idle_days"]) for e in report.daily(facts(*tasks), MONDAY)["stale"]}
    assert got == {
        "T-1": ("history", 3),
        "T-4": ("plan_step", 3),
        "T-5": ("created", 3),
    }
    assert not {"T-2", "T-3", "T-6", "T-7"} & set(got)  # 2 days is not more than stale_days; a run and a move count
    assert {e["ref"] for e in report.daily(facts(*tasks, stale_days=1), MONDAY)["stale"]} == {
        "T-1",
        "T-2",
        "T-4",
        "T-5",
    }
    assert report.daily(facts(*tasks, stale_days=3), MONDAY)["stale"] == []


def test_pm_daily_blockers_by_hand_and_by_decision_wait_on_a_member_a_task_or_a_text():
    by_hand = Blocker("manual", "needs the key", "member", "bao", at(SATURDAY), "alice")
    asked = Blocker("decision", "Ship the queue first?", "member", "alice", at(FRIDAY), None, decision_id=4, run_id=9)
    text = Blocker("manual", "legal review", "text", "the bank", at(MONDAY, 8), "alice")
    blocked = task(1, "doing", blockers=(by_hand, asked))
    free = task(2, "doing", blockers=(text,))
    finished = task(3, "done", blockers=(by_hand,))  # a finished task waits on nothing
    got = report.daily(facts(blocked, free, finished), MONDAY)["blocked"]
    assert [(e["ref"], e["kind"], e["waiting_on"], e["age_days"]) for e in got] == [
        ("T-1", "manual", "bao", 1),
        ("T-1", "decision", "alice", 1),
        ("T-2", "manual", "the bank", 0),
    ]
    assert got[1]["decision_id"] == 4 and got[1]["run_id"] == 9 and got[0]["set_by"] == "alice"


def test_pm_daily_escalate_lists_the_blockers_older_than_the_limit_for_the_pm():
    one = Blocker("manual", "a", "text", "x", at(FRIDAY, 9), "alice")
    fresh = Blocker("manual", "b", "text", "y", at(MONDAY, 9), "alice")
    tasks = [task(1, "doing", blockers=(one, fresh))]
    got = report.daily(facts(*tasks, pm="carol"), TUESDAY)["escalate"]  # age: Friday -> Tuesday is 2, Monday -> 1
    assert [(e["reason"], e["escalate_to"]) for e in got] == [("a", "carol")]
    assert report.daily(facts(*tasks, pm="carol", escalate_after_days=0), TUESDAY)["escalate"][1]["reason"] == "b"
    assert report.daily(facts(*tasks, escalate_after_days=2), TUESDAY)["escalate"] == []
    assert report.daily(facts(*tasks), TUESDAY)["escalate"][0]["escalate_to"] is None


def test_pm_daily_load_marks_the_overloaded_and_the_empty_members_among_writers():
    members = [Member("alice", "writer"), Member("bao", "admin"), Member("dung", "writer"), Member("eve", "reader")]
    tasks = [
        task(1, "doing", assignees=("alice",)),
        task(2, "doing", assignees=("alice", "bao")),
        task(3, "doing", assignees=("alice",), due_on=FRIDAY),
        task(4, "review", assignees=("bao",), qa=("dung",)),
        task(5, "todo", assignees=("dung",)),
    ]
    load = {m["login"]: m for m in report.daily(facts(*tasks, members=members), MONDAY)["load"]}
    assert list(load) == ["alice", "bao", "dung"]  # a reader has no load
    assert (load["alice"]["doing"], load["alice"]["overloaded"], load["alice"]["empty"]) == (3, True, False)
    assert (load["alice"]["late"], load["alice"]["doing_tasks"]) == (1, ["T-1", "T-2", "T-3"])
    assert (load["bao"]["doing"], load["bao"]["overloaded"]) == (1, False)
    assert (load["dung"]["empty"], load["dung"]["review_as_qa"], load["dung"]["overloaded"]) == (True, 1, False)
    assert report.daily(facts(*tasks, members=members, wip_limit=3), MONDAY)["load"][0]["overloaded"] is False


def test_pm_daily_hidden_tasks_add_to_counts_and_show_in_no_list():
    seen = task(1, "doing", due_on=FRIDAY, assignees=("alice",))
    secret = task(2, "doing", due_on=FRIDAY, assignees=("alice",), visible=False, title="the merger")
    hidden_done = task(3, "done", visible=False, events=(moved(SATURDAY, "done"),))
    blocker = Blocker("manual", "x", "text", "y", at(date(2026, 10, 14)), "alice")
    blocked = task(4, "doing", visible=False, blockers=(blocker,))
    got = report.daily(facts(seen, secret, hidden_done, blocked, members=[Member("alice", "writer")]), MONDAY)
    assert refs(got["late"]) == ["T-1"] and got["done_tasks"] == [] and got["blocked"] == []
    assert got["hidden"]["late"] == 1 and got["hidden"]["done_tasks"] == 1
    assert (got["hidden"]["blocked"], got["hidden"]["escalate"]) == (1, 1)
    assert got["load"][0]["doing"] == 2 and got["load"][0]["doing_tasks"] == ["T-1"]  # counted, not named
    assert "the merger" not in repr({k: v for k, v in got.items() if k != "hidden"})


def test_pm_member_shows_the_tasks_blockers_and_waits_of_one_member():
    wait_on_bao = Blocker("manual", "needs the key", "member", "bao", at(FRIDAY), "alice")
    asked = Blocker("decision", "Which queue?", "member", "alice", at(FRIDAY), None, decision_id=1, run_id=2)
    tasks = [
        task(1, "doing", assignees=("alice",), blockers=(asked,), due_on=FRIDAY),
        task(2, "doing", assignees=("carol",), blockers=(wait_on_bao,)),
        task(3, "review", assignees=("carol",), qa=("alice",), events=(moved(FRIDAY, "review", "doing"),)),
        task(4, "doing", assignees=("alice",), visible=False),
        task(5, "doing", assignees=("carol",), blockers=(wait_on_bao,), visible=False),
    ]
    data = facts(*tasks, members=[Member("alice", "writer")])
    alice = report.member(data, MONDAY, "alice")
    assert refs(alice["doing"]) == ["T-1"] and refs(alice["late"]) == ["T-1"]
    assert refs(alice["review"]) == ["T-3"] and [b["ref"] for b in alice["blocked"]] == ["T-1"]
    assert [b["ref"] for b in alice["blocking"]] == ["T-1"]  # the decision of her run waits on her
    assert alice["hidden"]["doing"] == 1
    bao = report.member(data, MONDAY, "bao")
    assert bao["doing"] == [] and [b["ref"] for b in bao["blocking"]] == ["T-2"]
    assert bao["hidden"]["blocking"] == 1 and bao["hidden"]["doing"] == 0
    assert (alice["project"], alice["date"], alice["login"]) == ("shop", MONDAY, "alice")


def test_pm_daily_the_calendar_counts_the_working_days_the_project_names():
    six_days = Calendar(ZONE, frozenset({1, 2, 3, 4, 5, 6}))
    assert six_days.previous(MONDAY) == SATURDAY
    assert CALENDAR.previous(MONDAY) == FRIDAY and CALENDAR.previous(WEDNESDAY) == TUESDAY
    assert CALENDAR.elapsed(FRIDAY, MONDAY) == 1 and six_days.elapsed(FRIDAY, MONDAY) == 2
    assert CALENDAR.span(SATURDAY, SUNDAY) == 0 and CALENDAR.span(MONDAY, FRIDAY + (MONDAY - FRIDAY)) == 1
    assert CALENDAR.local(datetime(2026, 10, 18, 18, tzinfo=UTC)) == MONDAY  # 01:00 on Monday in Ho Chi Minh
