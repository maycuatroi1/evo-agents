"""``evo-agents hub run watch``: it wakes on a run's new state, a decision that opens or closes, and a plan's step that
needs someone or was named, and on nothing else; it skips a look the hub could not answer for a moment, gives up
after --for minutes with EXIT_NO_CHANGE, and exits at once when every run named has ended. Against a fake hub and a
fake clock, without Postgres."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evo_agents.cli import main
from evo_agents.hub import run_cli_watch
from evo_agents.hub.client import HubError, Unreachable
from evo_agents.hub.run_cli_watch import EXIT_NO_CHANGE, Clock, watch

PROJECT = "demo"


class FakeHub:
    """Answers each look from one of ``looks``, the next after each sleep: a dict of runs' states, open decisions and
    plans' step statuses, or an exception to raise. The last look repeats."""

    url = "https://hub.example.org"

    def __init__(self, *looks):
        self.looks = list(looks)
        self.index = 0
        self.calls = []

    def _current(self):
        return self.looks[min(self.index, len(self.looks) - 1)]

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        current = self._current()
        if isinstance(current, Exception):
            raise current
        if "/runs/" in path:
            return {"state": current["runs"][int(path.rsplit("/", 1)[1])]}
        if "/decisions?" in path:
            return {"decisions": current.get("decisions", []), "total": 0, "limit": 200, "offset": 0}
        plan = path.rsplit("/", 1)[1]
        steps = [{"id": int(key), "status": status} for key, status in current["plans"][plan].items()]
        return {"body": {"steps": steps}}

    def next_look(self):
        self.index += 1


class FakeClock(Clock):
    def __init__(self, hub: FakeHub):
        self.now = 0.0
        self.slept = []
        self.hub = hub
        super().__init__(monotonic=lambda: self.now, sleep=self._sleep, wall=self._wall)

    def _sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        self.hub.next_look()

    def _wall(self):
        return datetime(2026, 11, 7, 9, 0, tzinfo=UTC) + timedelta(seconds=self.now)


def run_watch(hub, runs=(), plans=None, **options):
    lines, notes = [], []
    clock = FakeClock(hub)
    code = watch(
        hub,
        PROJECT,
        tuple(runs),
        plans or {},
        clock=clock,
        out=lines.append,
        warn=notes.append,
        **options,
    )
    return SimpleNamespace(code=code, lines=lines, notes=notes, slept=clock.slept)


def decision(decision_id, category="question", run_id=7):
    return {"id": decision_id, "category": category, "run_id": run_id, "question": "Merge it now?"}


def test_a_run_in_another_state_wakes_the_watch_and_says_what_changed():
    hub = FakeHub(
        {"runs": {7: "running", 8: "parked"}}, {"runs": {7: "running", 8: "parked"}}, {"runs": {7: "done", 8: "parked"}}
    )
    result = run_watch(hub, runs=(7, 8), every=60)
    assert result.code == 0
    assert result.slept == [60, 60]
    assert result.lines[0].startswith("Watching until 15:00 UTC, a look every 60 s: run #7 running; run #8 parked")
    assert result.lines[1] == "Changed at 09:02 UTC, after 2 min:"
    assert result.lines[2] == "  run #7: running -> done"
    assert result.lines[3] == "Now: run #7 done; run #8 parked; 0 open decision(s)."


def test_with_state_only_those_states_wake_it():
    hub = FakeHub({"runs": {7: "running"}}, {"runs": {7: "waiting"}}, {"runs": {7: "parked"}}, {"runs": {7: "failed"}})
    result = run_watch(hub, runs=(7,), wanted=frozenset({"done", "failed"}))
    assert result.code == 0 and len(result.slept) == 3
    assert "  run #7: running -> failed" in result.lines


def test_a_decision_that_opens_or_closes_wakes_it_but_a_runs_own_wait_does_not():
    first = {"runs": {}, "decisions": [decision(5)]}
    waits = {"runs": {}, "decisions": [decision(5), decision(9, "wait")]}
    asked = {"runs": {}, "decisions": [decision(11)]}
    result = run_watch(FakeHub(first, waits, asked))
    assert result.code == 0 and len(result.slept) == 2
    assert "  decision #11 opened (run #7): Merge it now?" in result.lines
    assert "  decision #5 closed" in result.lines


def test_without_decisions_none_is_read_and_none_wakes_it():
    hub = FakeHub({"runs": {7: "running"}}, {"runs": {7: "running"}, "decisions": [decision(3)]})
    result = run_watch(hub, runs=(7,), decisions=False, minutes=3)
    assert result.code == EXIT_NO_CHANGE
    assert not any("/decisions" in path for _, path in hub.calls)
    assert "open decision" not in result.lines[-1]


def test_a_plans_steps_wake_it_when_blocked_named_or_all_closed():
    def plan(*statuses):
        return {"runs": {}, "plans": {"rollout": {str(i + 1): s for i, s in enumerate(statuses)}}}

    progress = run_watch(
        FakeHub(plan("done", "pending", "pending"), plan("done", "done", "in_progress")),
        plans={"rollout": ("3",)},
        minutes=2,
    )
    assert progress.code == 0 and "  plan rollout: step 3 pending -> in_progress" in progress.lines
    unnamed = run_watch(
        FakeHub(plan("done", "pending", "pending"), plan("done", "done", "pending")), plans={"rollout": ()}, minutes=2
    )
    assert unnamed.code == EXIT_NO_CHANGE
    blocked = run_watch(FakeHub(plan("done", "pending"), plan("done", "blocked")), plans={"rollout": ()})
    assert "  plan rollout: step 2 pending -> blocked" in blocked.lines
    closed = run_watch(FakeHub(plan("done", "in_progress"), plan("done", "done")), plans={"rollout": ()})
    assert closed.lines[1:3] == ["Changed at 09:01 UTC, after 1 min:", "  plan rollout: every step is closed"]


def test_a_step_named_that_the_plan_does_not_have_is_an_error():
    hub = FakeHub({"runs": {}, "plans": {"rollout": {"1": "done", "2": "pending"}}})
    with pytest.raises(HubError, match=r"plan rollout has no step 9; its steps: 1, 2"):
        run_watch(hub, plans={"rollout": ("9",)})


def test_a_look_the_hub_could_not_answer_is_skipped_with_one_note():
    hub = FakeHub(
        {"runs": {7: "running"}},
        HubError("bad gateway", 502),
        Unreachable("cannot reach hub.example.org"),
        {"runs": {7: "running"}},
        {"runs": {7: "done"}},
    )
    result = run_watch(hub, runs=(7,))
    assert result.code == 0 and "  run #7: running -> done" in result.lines
    assert len(result.notes) == 2
    assert result.notes[0].startswith("note: the hub did not answer (bad gateway)")
    assert result.notes[1] == "note: the hub answers again"


def test_a_refusal_ends_the_watch():
    hub = FakeHub({"runs": {7: "running"}}, HubError("no such run", 403))
    with pytest.raises(HubError, match="no such run"):
        run_watch(hub, runs=(7,))


def test_with_no_change_it_looks_last_at_the_deadline_and_exits_no_change():
    result = run_watch(FakeHub({"runs": {7: "running"}}), runs=(7,), minutes=1, every=45)
    assert result.code == EXIT_NO_CHANGE
    assert result.slept == [45, 15]
    assert result.lines[-1] == "No change in 1 min, at 09:01 UTC. Now: run #7 running; 0 open decision(s)."


def test_runs_that_have_all_ended_leave_nothing_to_wait_for():
    result = run_watch(FakeHub({"runs": {7: "done", 8: "cancelled"}}), runs=(7, 8))
    assert result.code == 0 and result.slept == []
    assert result.lines == ["Nothing left to wait for: run #7 done; run #8 cancelled; 0 open decision(s)."]


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))


def cli(monkeypatch, capsys, *args):
    code = main(["hub", "run", "watch", *args])
    out, err = capsys.readouterr()
    return SimpleNamespace(code=code, out=out, err=err)


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        (("--no-decisions", "--project", PROJECT), "nothing to watch"),
        (("7", "--every", "5", "--project", PROJECT), "--every takes 15 seconds or more"),
        (("7", "--for", "0", "--project", PROJECT), "--for takes 1 to"),
    ],
)
def test_usage_errors(monkeypatch, capsys, args, problem):
    result = cli(monkeypatch, capsys, *args)
    assert result.code == 2 and problem in result.err


def test_the_command_reads_runs_decisions_and_plans_of_the_project(monkeypatch, capsys):
    hub = FakeHub(
        {"runs": {7: "done"}, "plans": {"rollout": {"1": "pending"}}},
        {"runs": {7: "done"}, "plans": {"rollout": {"1": "done"}}},
    )
    monkeypatch.setattr(run_cli_watch, "signed_in", lambda: (hub, None))
    monkeypatch.setattr(run_cli_watch, "Clock", lambda: FakeClock(hub))
    result = cli(monkeypatch, capsys, "7", "7", "--plan", "rollout:1", "--plan", "rollout", "--project", PROJECT)
    assert result.code == 0, result.err
    assert hub.calls[:3] == [
        ("GET", "/v1/projects/demo/runs/7"),
        ("GET", "/v1/projects/demo/decisions?state=open&limit=200"),
        ("GET", "/v1/projects/demo/plans/rollout"),
    ]
    assert "  plan rollout: step 1 pending -> done" in result.out
    assert cli(monkeypatch, capsys, "--plan", "rollout:", "--project", PROJECT).code == 1
