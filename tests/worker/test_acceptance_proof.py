"""One line of proof for each acceptance criterion, on the worker (step 4 of the super-board-parity-2 plan).

``evo-agents worker step KEY done`` of a step whose acceptance names criteria of the plan's is refused, naming the
criteria left without a ``--proof``, before anything is verified or pushed; with one for each, the hub gets them
structured. A step run gives them in ``"proofs"`` of its result file, and a run without them fails. A step that names
no criterion keeps the behaviour it had."""

from __future__ import annotations

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker.runner.common import RunFailed
from evo_agents.worker.runner.verify import result_proofs
from tests.worker.fake_hub import FakeHub
from tests.worker.test_plan_runs import PLAN, git, machine, plan_body, run_repos, wait_for, with_daemon  # noqa: F401

A4 = "The file a.txt holds one line."
A9 = "The contract golden is regenerated."


def plan_with_acceptance() -> dict:
    body = plan_body()
    body["acceptance"] = [{"id": "a4", "criterion": A4}, {"id": "a9", "criterion": A9, "invariant": True}]
    body["steps"][0]["acceptance"] = ["a4", "a9"]
    return body


def done(*proofs: str) -> dict:
    flags = [part for proof in proofs for part in ("--proof", proof)]
    return {"cli": ["step", "1", "done", "--evidence", "Wrote a.txt.", "--verify", "test -f a.txt", *flags]}


def test_acceptance_proof_worker_step_done_refuses_a_missing_line_and_takes_one_for_each(machine):
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"write": {"alpha/a.txt": "a\n", "alpha/test_a.py": "def test_one_line():\n    pass\n"}},
                done(),
                done("a4=test_a.py::test_one_line"),
                done("a4=test_a.py::test_two_lines", "a9=n/a: no contract here"),
                done("a4=test_a.py::test_one_line", "a9=n/a: no contract here", "a12=pytest"),
                done("a4=test_a.py::test_one_line", "a9=n/a: no contract here"),
                {"result": {"summary": "Step 1 done."}},
            ]
        }
    )
    found = {}

    async def body(hub: FakeHub, daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        assert await hub.wait_state(run_id, "done", "failed") in ("done", "failed")
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found["hub"] = hub

    with_daemon(machine, body, plan=plan_with_acceptance())
    hub = found["hub"]
    commands = machine.commands()
    none, one, absent, extra, whole = commands[0:5]
    for refused in (none, one, absent, extra):
        assert refused["exit"] == 1, refused
        assert "Nothing was verified, committed or pushed" in refused["stderr"], refused
    assert "2 has none" in none["stderr"] and f"a4: {A4}" in none["stderr"] and f"a9: {A9}" in none["stderr"]
    assert "1 has none" in one["stderr"] and f"a9: {A9}" in one["stderr"] and "a4: " not in one["stderr"]
    assert "has no test named test_two_lines" in absent["stderr"]
    assert "not a12" in extra["stderr"]
    assert whole["exit"] == 0, whole
    reports = [item for item in hub.step_reports if item["status"] == "done"]
    assert len(reports) == 1, "the refused ones reached no hub"
    assert reports[0]["proofs"] == [
        {"id": "a4", "kind": "test", "locator": "test_a.py::test_one_line"},
        {"id": "a9", "kind": "n/a", "reason": "no contract here"},
    ]
    assert reports[0]["evidence"].startswith("Wrote a.txt."), "free-text evidence stays"


def test_acceptance_proof_a_step_without_acceptance_is_done_without_one(machine):
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"write": {"alpha/a.txt": "a\n"}},
                done("a4=pytest"),
                done(),
                {"result": {"summary": "Step 1 done."}},
            ]
        }
    )
    found = {}

    async def body(hub: FakeHub, daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        assert await hub.wait_state(run_id, "done", "failed") in ("done", "failed")
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found["hub"] = hub

    with_daemon(machine, body)
    extra, plain = machine.commands()[0:2]
    assert extra["exit"] == 1 and "names no criterion" in extra["stderr"], extra
    assert plain["exit"] == 0, plain
    reports = [item for item in found["hub"].step_reports if item["status"] == "done"]
    assert len(reports) == 1 and "proofs" not in reports[0]


def test_acceptance_proof_of_a_step_run_comes_from_its_result_file(tmp_path):
    (tmp_path / "t.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    assert result_proofs({"summary": "s"}, None, tmp_path) is None
    whole = {"proofs": {"a4": "t.py::test_x", "a9": "n/a: untouched"}}
    assert result_proofs(whole, ["a4", "a9"], tmp_path) == [
        {"id": "a4", "kind": "test", "locator": "t.py::test_x"},
        {"id": "a9", "kind": "n/a", "reason": "untouched"},
    ]
    with pytest.raises(RunFailed, match="a9"):
        result_proofs({"summary": "s"}, ["a4", "a9"], tmp_path)
    with pytest.raises(RunFailed, match="a9"):
        result_proofs({"proofs": {"a4": "t.py::test_x"}}, ["a4", "a9"], tmp_path)
    with pytest.raises(RunFailed, match="no test named test_y"):
        result_proofs({"proofs": {"a4": "t.py::test_y"}}, ["a4"], tmp_path)
