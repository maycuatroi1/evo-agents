"""The verify of a plan run's step runs once, as a job of the worker (``evo_agents.worker.verify_jobs``).

`worker step <id> verify` runs the step's verify commands in a process of their own and keeps the result with the tree
the worktree held and the commands; `worker step <id> done` reuses a passed result of the same commands on the same
tree, and runs the verify again when the tree changed, the commands differ, the result failed, or the run's commits were
replayed on top of a default branch the remote moved on. A job outlives the command that started it, a later command
waits on it, and the end of the run stops one that still runs. Each test counts the verify's runs in a file outside
the worktree.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker import verify_jobs
from evo_agents.worker.orphans import process_start
from tests.worker.test_plan_runs import (  # noqa: F401 (machine, a fixture)
    PLAN,
    Machine,
    default_branch_run,
    git,
    machine,
    move_origin,
    plan_body,
    run_repos,
    wait_for,
    with_daemon,
)


def plan_run(machine: Machine, scenario: list) -> tuple[int, object]:
    """A plan run of ``plan_body()`` whose agent follows ``scenario``; its id and the hub, once it ended."""
    machine.scenarios({f"plan:{PLAN}": scenario})
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        await hub.wait_state(run_id, "done", "failed")
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    return found["run"], found["hub"]


def runs_of(count: Path) -> int:
    return len(count.read_text(encoding="utf-8").splitlines()) if count.exists() else 0


def step(*args: str) -> dict:
    return {"cli": ["step", "1", *args]}


def done(check: str, *more: str) -> dict:
    return step("done", "--repo", "alpha", "--evidence", "Wrote a.txt.", "--verify", check, *more)


def test_done_reuses_the_verify_that_passed_on_the_same_tree(machine):
    count = machine.tmp / "count"
    check = f"echo ran >> {count}; test -f a.txt"
    run_id, hub = plan_run(
        machine,
        [
            {"write": {"alpha/a.txt": "a\n"}},
            step("verify", "--repo", "alpha", "--verify", check),
            {"sh": "git -C alpha add a.txt && git -C alpha commit --quiet -m 'Alpha file'"},  # as a subagent commits
            done(check),
            {"result": {"summary": "Step 1 done."}},
        ],
    )
    assert hub.runs[run_id]["state"] == "done", (hub.runs[run_id], hub.texts(run_id))
    verify, committed, reported = machine.commands()
    assert verify["exit"] == 0 and "The verify of step 1 passed (run, job" in verify["stdout"], verify
    assert committed["exit"] == 0, committed
    assert reported["exit"] == 0, reported
    assert runs_of(count) == 1, "the commit kept the tree, so done ran nothing again"
    assert "(reused: job" in reported["stdout"] and f"verify: `{check}` exited 0" in reported["stdout"]
    report = hub.step_reports[-1]
    assert [(item["command"], item["exit_code"]) for item in report["verify"]] == [(check, 0)]
    assert report["evidence"].startswith("Wrote a.txt.\nVerify by the worker: job "), report["evidence"]
    assert report["evidence"].endswith("reused by done, no run again."), report["evidence"]
    assert report["commit_sha"] == machine.origin_rev("alpha", "refs/heads/feat/alpha")


def test_done_runs_the_verify_again_when_the_tree_or_the_commands_changed(machine):
    count = machine.tmp / "count"
    check = f"echo ran >> {count}; test -f a.txt"
    run_id, hub = plan_run(
        machine,
        [
            {"write": {"alpha/a.txt": "a\n"}},
            step("verify", "--repo", "alpha", "--verify", check),
            {"write": {"alpha/a.txt": "changed after the verify\n"}},
            step("verify", "--repo", "alpha", "--verify", check),
            done(check, "--verify", "true"),
            {"result": {"summary": "Step 1 done."}},
        ],
    )
    assert hub.runs[run_id]["state"] == "done", (hub.runs[run_id], hub.texts(run_id))
    first, second, reported = machine.commands()
    assert first["exit"] == second["exit"] == reported["exit"] == 0, machine.commands()
    assert runs_of(count) == 3, "once per tree, and once more for the commands done names"
    assert "(reused" not in second["stdout"] and "(reused" not in reported["stdout"]
    report = hub.step_reports[-1]
    assert [item["command"] for item in report["verify"]] == [check, "true"]
    assert report["evidence"].endswith("run by done."), report["evidence"]


def test_a_verify_that_failed_is_not_reused(machine):
    count, flag = machine.tmp / "count", machine.tmp / "flag"
    check = f"echo ran >> {count}; test -f {flag}"
    run_id, hub = plan_run(
        machine,
        [
            {"write": {"alpha/a.txt": "a\n"}},
            step("verify", "--repo", "alpha", "--verify", check),
            done(check),
            {"touch": str(flag)},
            done(check),
            {"result": {"summary": "Step 1 done."}},
        ],
    )
    verify, refused, reported = machine.commands()
    assert verify["exit"] == 1 and f"`{check}` exited 1" in verify["stderr"], verify
    assert refused["exit"] == 1 and "Nothing was committed or pushed" in refused["stderr"], refused
    assert reported["exit"] == 0, reported
    assert runs_of(count) == 3, "a failed verify runs again each time, on the same tree"
    assert [(item["key"], item["status"]) for item in hub.step_reports] == [("1", "done")]
    assert hub.runs[run_id]["state"] == "done"


def test_the_verify_runs_again_after_the_runs_commits_are_replayed_on_a_moved_main(machine):
    count = machine.tmp / "count"
    check = f"echo ran >> {count}; test -f a.txt"
    found = default_branch_run(
        machine,
        [
            {"write": {"alpha/a.txt": "a\n"}},
            step("verify", "--repo", "alpha", "--verify", check),
            done(check),
            {"result": {"summary": "Step 1 done on main."}},
        ],
        lambda: move_origin(machine, "alpha", "theirs.txt"),
    )
    run_id, hub = found["run"], found["hub"]
    assert hub.runs[run_id]["state"] == "done", (hub.runs[run_id], hub.texts(run_id))
    _, reported = machine.commands()
    assert reported["exit"] == 0, reported
    assert "(reused: job" in reported["stdout"] and "runs again at" in reported["stdout"], reported["stdout"]
    assert runs_of(count) == 2, "once before the push, reused by done, and once on the replayed tree"
    report = hub.step_reports[-1]
    assert report["commit_sha"] == machine.origin_rev("alpha", "refs/heads/main")
    assert report["evidence"].endswith("run by done."), "the evidence names the job of the replayed tree"


def test_a_job_outlives_the_command_that_started_it_and_done_waits_on_it(machine):
    count = machine.tmp / "count"
    check = f"sleep 2; echo ran >> {count}"
    run_id, hub = plan_run(
        machine,
        [
            {"write": {"alpha/a.txt": "a\n"}},
            step("verify", "--repo", "alpha", "--wait", "0", "--verify", check),
            done(check),
            {"result": {"summary": "Step 1 done."}},
        ],
    )
    started, reported = machine.commands()
    assert started["exit"] == verify_jobs.EXIT_RUNNING and "still runs, on its own" in started["stderr"], started
    assert reported["exit"] == 0 and "(running since" in reported["stdout"], reported
    assert runs_of(count) == 1
    evidence = hub.step_reports[-1]["evidence"]
    assert evidence.endswith("started before done, which waited on it."), evidence
    assert hub.runs[run_id]["state"] == "done"


def test_the_end_of_the_run_stops_a_verify_that_still_runs(machine):
    run_id, hub = plan_run(
        machine,
        [
            step("verify", "--repo", "alpha", "--wait", "0", "--verify", "sleep 120"),
            {"result": {"summary": "Left step 1 pending."}},
        ],
    )
    assert machine.commands()[0]["exit"] == verify_jobs.EXIT_RUNNING
    jobs = verify_jobs.records(machine.home.run_dir(run_id) / verify_jobs.JOBS_DIR)
    assert [record["state"] for _, record in jobs] == [verify_jobs.STOPPED], jobs
    record = jobs[0][1]
    assert record["error"] == "the run ended"
    assert record.get("pid") is None or process_start(record["pid"]) is None, "its process is gone"


# The parts


def test_tree_of_leaves_the_index_alone_and_the_runs_own_files_out(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "--quiet", "--initial-branch", "main", cwd=repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "a.txt", cwd=repo)
    git("commit", "--quiet", "-m", "first", cwd=repo)
    scratch = tmp_path / "scratch"
    clean = asyncio.run(verify_jobs.tree_of(repo, scratch))
    assert clean == git("rev-parse", "HEAD^{tree}", cwd=repo)
    (repo / ".evo-run").mkdir()
    (repo / ".evo-run" / "result.json").write_text("{}", encoding="utf-8")
    assert asyncio.run(verify_jobs.tree_of(repo, scratch)) == clean, ".evo-run/ is the worker's, not the step's"
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    changed = asyncio.run(verify_jobs.tree_of(repo, scratch))
    assert changed != clean
    assert git("status", "--porcelain", cwd=repo) == "?? .evo-run/\n?? b.txt", "nothing was staged"
    assert not list(scratch.glob("index-*")), "the index copy is gone"


def test_a_job_that_passed_counts_for_the_tree_it_left(tmp_path):
    directory = tmp_path / "verify"
    directory.mkdir()
    record = {"key": "k", "repo": "alpha", "commands": ["make"], "tree": "t1", "tree_after": "t2", "state": "passed"}
    verify_jobs.write(directory / "k.json", record)
    assert verify_jobs.passed(directory, "alpha", "t1", ["make"]) == record
    assert verify_jobs.passed(directory, "alpha", "t2", ["make"]) == record, "what the verify left, as done commits"
    assert verify_jobs.passed(directory, "alpha", "t3", ["make"]) is None
    assert verify_jobs.passed(directory, "alpha", "t1", ["make", "lint"]) is None
    assert verify_jobs.passed(directory, "beta", "t1", ["make"]) is None
    verify_jobs.write(directory / "k.json", {**record, "state": "failed"})
    assert verify_jobs.passed(directory, "alpha", "t1", ["make"]) is None


def test_a_job_whose_process_is_gone_is_marked_stopped(tmp_path, monkeypatch):
    directory = tmp_path / "verify"
    directory.mkdir()
    gone = subprocess.Popen(["true"])
    gone.wait()
    path = directory / "k.json"
    verify_jobs.write(path, {"key": "k", "id": "x", "state": "running", "pid": gone.pid, "started": "then"})
    monkeypatch.setattr(verify_jobs, "ALIVE_EVERY", 0.0)
    record = asyncio.run(verify_jobs.wait(path, 5))
    assert record["state"] == "stopped" and "ended without a result" in record["error"]
    assert os.path.exists(path)
