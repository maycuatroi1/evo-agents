"""What is left of a command Claude Code stopped (``evo_agents.worker.runtimes.task_reaper``): a stopped `pnpm check`
left the `tsc` of each package's typecheck, which turbo ran in process groups of their own, running for close to an
hour on a worker of 14 GB, and every run of the machine stalled."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evo_agents.worker.runtimes.task_reaper import Process, TaskReaper


def test_the_reaper_takes_one_stop_of_a_command_and_follows_the_commands_that_run():
    reaper = TaskReaper(lambda: None, enabled=True)
    assert reaper.see("task_started", {"task_id": "b1", "is_backgrounded": True}) == [] and reaper.running == {"b1"}
    assert reaper.see("task_updated", {"task_id": "b1", "patch": {"status": "killed"}}) == ["b1"]
    assert reaper.see("task_notification", {"task_id": "b1", "status": "stopped"}) == [], "the same stop"
    reaper.see("task_started", {"task_id": "b2"})
    assert reaper.see("task_notification", {"task_id": "b2", "status": "completed"}) == []
    assert reaper.running == set()
    assert (
        TaskReaper(lambda: None, enabled=False).see("task_updated", {"task_id": "b3", "patch": {"status": "killed"}})
        == []
    )


def test_reap_kills_what_the_stopped_command_left_and_spares_the_cli_and_the_other_commands():
    cli, other_server = 100, 500
    before = {
        cli: Process(1, cli, "t0"),
        200: Process(cli, 200, "t1"),  # the shell of `pnpm check`, which writes the output file of b1
        201: Process(200, 200, "t1"),  # pnpm
        202: Process(201, 200, "t1"),  # turbo
        300: Process(202, 300, "t2"),  # a tsc in a group of its own; its output goes to a log file
        400: Process(cli, 400, "t3"),  # the shell of b2, still running
        401: Process(400, 400, "t3"),
        other_server: Process(cli, cli, "t0"),  # an MCP server in the CLI's group
    }
    after = {  # the CLI killed the group of b1's shell
        cli: before[cli],
        201: Process(1, 9, "t9"),  # another process now has pid 201
        202: Process(1, 200, "t1"),  # turbo, reparented
        300: before[300],
        301: Process(202, 301, "t4"),  # turbo started one more typecheck as it shut down
        400: before[400],
        401: before[401],
        other_server: before[other_server],
    }
    tasks = {200: "b1", 201: "b1", 202: "b1", 400: "b2", 401: "b2"}
    table = {"now": before}
    groups, pids = [], []
    reaper = TaskReaper(
        lambda: cli,
        table=lambda: table["now"],
        task_of=tasks.get,
        kill_group=groups.append,
        kill=pids.append,
        enabled=True,
    )
    reaper.note()
    table["now"] = after
    assert reaper.reap("b1") == 3
    assert sorted(pids) == [202, 300, 301] and sorted(groups) == [200, 300, 301]
    assert reaper.reap("b1") == 0, "forgotten once reaped"
    assert reaper.reap_all() == 2 and sorted(pids[3:]) == [400, 401] and groups[3:] == [400]
    assert cli not in pids + groups and other_server not in pids


@pytest.mark.timeout(30)
def test_a_child_in_a_group_of_its_own_dies_with_its_stopped_command(tmp_path):
    """Real processes: the test is the CLI, a shell writes the command's output file and starts a child that leads a
    session of its own (as turbo's tasks lead their groups); the CLI's stop kills the shell's group only."""
    output = tmp_path / "tasks" / "bstop1.output"
    output.parent.mkdir()
    ready = tmp_path / "ready"
    child = f"import os, time; os.setsid(); open({str(ready)!r}, 'w').close(); time.sleep(60)"
    with output.open("w") as out:
        shell = subprocess.Popen(
            ["sh", "-c", f'{sys.executable} -c "{child}" & wait'], stdout=out, start_new_session=True
        )
    reaper = TaskReaper(lambda: os.getpid(), enabled=True)
    if not Path("/proc/self/fd").is_dir():  # no /proc to read the output file of a process from
        reaper.task_of = lambda pid: "bstop1" if pid == shell.pid else None
    for _ in range(200):
        if ready.exists():
            break
        time.sleep(0.05)
    reaper.note()
    (left,) = set(reaper.noted["bstop1"]) - {shell.pid}
    os.killpg(shell.pid, signal.SIGKILL)  # what the CLI does
    shell.wait()
    assert left in reaper.table(), "the child outlived the stop of its command"
    assert reaper.reap("bstop1") == 1
    for _ in range(100):
        if left not in reaper.table():
            break
        time.sleep(0.05)
    assert left not in reaper.table()
