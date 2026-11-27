"""The commands of a run's agent go before its CLI when the machine runs out of memory (evo_agents/worker/oom.py)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from evo_agents.worker import oom


def test_the_agent_gets_bash_env_naming_the_script_and_keeps_the_daemons_own(tmp_path):
    adj = tmp_path / "oom_score_adj"
    adj.write_text("200\n")
    given = oom.agent_env({"BASH_ENV": "/etc/evo/profile.sh"}, tmp_path / "home", adj=adj)
    script = tmp_path / "home" / oom.FILE
    assert given == {oom.VARIABLE: str(script), oom.PREVIOUS: "/etc/evo/profile.sh"}
    assert script.read_text(encoding="utf-8") == oom.SCRIPT
    assert oom.agent_env({}, tmp_path / "home", adj=adj) == {oom.VARIABLE: str(script)}
    assert oom.agent_env({"BASH_ENV": str(script)}, tmp_path / "home", adj=adj) == {oom.VARIABLE: str(script)}
    assert oom.agent_env({}, tmp_path / "home", adj=tmp_path / "missing") == {}, "no /proc: the environment stays"


@pytest.mark.skipif(not oom.ADJ.exists() or shutil.which("bash") is None, reason="needs Linux's /proc and bash")
def test_a_command_raises_its_score_and_what_it_starts_inherits_it(tmp_path):
    previous = tmp_path / "previous.sh"
    previous.write_text("echo previous\n")
    env = {**os.environ, **oom.agent_env({"BASH_ENV": str(previous)}, tmp_path, adj=Path(oom.ADJ))}
    done = subprocess.run(
        ["bash", "-c", "cat /proc/self/oom_score_adj; sh -c 'cat /proc/self/oom_score_adj'"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = done.stdout.split()
    assert lines[0] == "previous" and all(int(score) >= oom.SCORE for score in lines[1:])
