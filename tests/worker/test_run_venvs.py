"""The Python environment of a worktree (``evo_agents.worker.venvs``, ``evo_agents.worker.runner.venv``), without
Postgres: the daemon of ``tests.worker.test_plan_runs`` with alpha's pyproject.toml asking for an environment and beta
asking for none, and a stand-in ``uv`` on the run's PATH that makes ``.venv`` as uv does (its ``.gitignore`` of ``*``)
with a ``bin/python`` that runs this interpreter and says, in EVO_FAKE_VENV, which environment it belongs to.

A plan run makes alpha's environment once its worktree is made and puts its bin directory first on the agent's PATH,
without the daemon's VIRTUAL_ENV; ``evo-agents worker step`` makes it again before a done step's verify, so an entry
point the step added to pyproject.toml is in it, and runs the verify with it; beta, which asks for none, is left as it
was, and nothing of .venv is committed. A run of one step makes it before its agent and again before its verify. uv
missing from the run's PATH fails the run with missing_tool, and a sync that fails fails it with environment, both
before the agent starts.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker import adapter as adapter_module
from evo_agents.worker import checkouts as checkouts_module
from evo_agents.worker import venvs
from evo_agents.worker.daemon import Daemon
from tests.worker import fake_adapter
from tests.worker.fake_hub import FakeHub
from tests.worker.test_plan_runs import GIT_IDENTITY, PLAN, Machine, git, plan_body, run_repos, wait_for, with_daemon

ASKS = """[project]
name = "alpha"
version = "0.1.0"

[project.entry-points."alpha.plugins"]
core = "alpha.core:MANIFEST"

[tool.evo-agents.worker]
venv = true
extras = ["test"]
requirements = ["plugins.txt"]
"""

FAKE_UV = """#!{python}
import json, os, pathlib, stat, sys

args = sys.argv[1:]
with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"args": args, "cwd": os.getcwd(), "virtual_env": os.environ.get("VIRTUAL_ENV")}}) + "\\n")
code = int(os.environ.get("FAKE_UV_EXIT") or 0)
if code:
    print("error: Distribution `missing==1.0` can't be installed because it is not in the lockfile")
    sys.exit(code)
here = pathlib.Path.cwd()
if args[:1] == ["sync"]:
    venv = here / ".venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / ".gitignore").write_text("*\\n")
    python = venv / "bin" / "python"
    python.write_text('#!/bin/sh\\nEVO_FAKE_VENV="%s" exec "{python}" "$@"\\n' % venv)
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    (venv / "pyproject.seen").write_text((here / "pyproject.toml").read_text())
elif args[:2] == ["pip", "install"]:
    assert pathlib.Path(args[args.index("--python") + 1]).is_file(), "uv pip install after uv sync"
"""


@pytest.fixture
def machine(tmp_path, monkeypatch) -> Machine:
    monkeypatch.setattr(checkouts_module, "REGISTRY_PATHS", ())
    unavailable = adapter_module.Detection(False, None, "not looked for in this test")
    monkeypatch.setattr(adapter_module, "probe_binary", lambda *args, **kwargs: unavailable)
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    fake_adapter.FakeAdapter.starts.clear()
    machine = Machine(tmp_path)
    alpha = machine.checkouts["alpha"]
    (alpha / "pyproject.toml").write_text(ASKS, encoding="utf-8")
    (alpha / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (alpha / "plugins.txt").write_text("alpha-plugins==1.0\n", encoding="utf-8")
    git("add", "-A", cwd=alpha)
    git("commit", "--quiet", "-m", "alpha asks for a venv", cwd=alpha)
    git("push", "--quiet", "origin", "main", cwd=alpha)
    machine.seeds["alpha"] = git("rev-parse", "HEAD", cwd=alpha)
    bin_dir = tmp_path / "uv-bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV.format(python=sys.executable), encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    machine.uv_log = tmp_path / "uv.jsonl"
    machine.env.update(
        PATH=os.pathsep.join([str(bin_dir), machine.env.get("PATH", os.defpath)]),
        FAKE_UV_LOG=str(machine.uv_log),
        VIRTUAL_ENV=str(tmp_path / "machine-venv"),
    )
    return machine


def uv_calls(machine: Machine) -> list[dict]:
    if not machine.uv_log.exists():
        return []
    return [json.loads(line) for line in machine.uv_log.read_text(encoding="utf-8").splitlines()]


# The arguments of the uv calls that make alpha's environment, each in its worktree.
MADE = [
    ["sync", "--frozen", "--extra", "test"],
    ["pip", "install", "--python", ".venv/bin/python", "--no-deps", "-r", "plugins.txt"],
]


ADDS_PM = ASKS.replace('core = "alpha.core:MANIFEST"\n', 'core = "alpha.core:MANIFEST"\npm = "alpha.pm:MANIFEST"\n')
# The python of the environment in the worktree the command runs in.
IN_THE_VENV = (
    "python -c \"import os, sys; sys.exit(os.environ.get('EVO_FAKE_VENV') != os.path.join(os.getcwd(), '.venv'))\""
)


def test_a_plan_run_puts_the_venv_of_the_repo_that_asks_first_on_path_and_makes_it_again_before_verify(machine):
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"sh": "command -v python; echo VIRTUAL_ENV=${VIRTUAL_ENV:-unset}; ls -a beta"},
                {"write": {"alpha/pyproject.toml": ADDS_PM, "alpha/a.txt": "a\n"}},
                {
                    "cli": [
                        "step",
                        "1",
                        "done",
                        "--evidence",
                        "Added the pm entry point.",
                        "--verify",
                        IN_THE_VENV,
                        "--verify",
                        "grep -q '^pm = ' .venv/pyproject.seen",
                    ]
                },
                {"result": {"summary": "Step 1 done."}},
            ]
        }
    )
    found = {}

    async def body(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        assert await hub.wait_state(run_id, "done", "failed") == "done", (hub.runs[run_id], hub.texts(run_id))
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    alpha = machine.directory(run_id) / "alpha"
    commands = machine.commands()

    # The agent's python is alpha's, without the daemon's VIRTUAL_ENV, and beta has no .venv.
    looked = commands[0]["stdout"].splitlines()
    assert Path(looked[0]).resolve() == (alpha / ".venv" / "bin" / "python").resolve(), looked
    assert looked[1] == "VIRTUAL_ENV=unset"
    assert ".venv" not in looked[2:]

    # uv made alpha's environment before the agent, without VIRTUAL_ENV, and worker step made it again, so its verify
    # ran with alpha's python and saw the entry point the step added.
    calls = uv_calls(machine)
    assert [call["args"] for call in calls] == MADE * 2, calls
    assert {Path(call["cwd"]).resolve() for call in calls} == {alpha.resolve()}
    assert all(call["virtual_env"] is None for call in calls)
    done = commands[1]
    assert done["exit"] == 0, done
    assert "Made the Python environment of alpha again: `uv sync --frozen --extra test`" in done["stdout"]
    assert [(item["command"], item["exit_code"]) for item in hub.step_reports[-1]["verify"]] == [
        (IN_THE_VENV, 0),
        ("grep -q '^pm = ' .venv/pyproject.seen", 0),
    ]
    texts = hub.texts(run_id)
    assert any(text.startswith("`uv sync --frozen --extra test` in alpha: ") for text in texts), texts
    assert any(
        text == f"{alpha / '.venv' / 'bin'} is first on the PATH of the run's agent and of its verify."
        for text in texts
    )

    # Nothing of .venv was committed, and the run's record names the environment.
    tree = machine.origin("alpha", "ls-tree", "-r", "--name-only", "feat/alpha").split()
    assert "a.txt" in tree and not any(path.startswith(".venv") for path in tree), tree
    assert machine.home.load_run(run_id)["venvs"] == {"alpha": str(alpha / ".venv" / "bin")}


def test_a_run_of_one_step_makes_the_venv_before_its_agent_and_again_before_its_verify(machine):
    machine.scenarios({"1": [{"write": {"pyproject.toml": ADDS_PM}}, {"result": {"verify_commands": [IN_THE_VENV]}}]})
    found = {}

    async def body(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        assert await hub.wait_state(run_id, "done", "review", "failed") != "failed", hub.runs[run_id].get("error")
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run = found["hub"].runs[found["run"]]
    worktree = machine.home.worktree_path("demo", found["run"])
    assert [(item["command"], item["exit_code"]) for item in run["verify"]] == [(IN_THE_VENV, 0)]
    assert [call["args"] for call in uv_calls(machine)] == MADE * 2
    assert "pm = " in (worktree / ".venv" / "pyproject.seen").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("change", "cause", "said"),
    [
        ({"PATH": "/usr/bin:/bin"}, "missing_tool", "uv is not on this worker's PATH; the agent did not start"),
        (
            {"FAKE_UV_EXIT": "2"},
            "environment",
            "`uv sync --frozen --extra test` in alpha exited 2: error: Distribution `missing==1.0` can't be installed",
        ),
    ],
)
def test_a_venv_that_is_not_made_fails_the_run_before_the_agent_starts(machine, tmp_path, change, cause, said):
    started = tmp_path / "started"
    machine.scenarios({f"plan:{PLAN}": [{"touch": str(started)}, {"result": {"summary": "no"}}]})
    machine.env.update(change)
    found = {}

    async def body(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        assert await hub.wait_state(run_id, "done", "failed") == "failed"
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run = found["hub"].runs[found["run"]]
    assert run["failure_cause"] == cause, run
    assert said in run["error"], run["error"]
    assert not started.exists(), "the agent did not start"


# What a repo asks (venvs.wanted) and the commands that make it


def write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")
    return tmp_path


def test_a_repo_asks_for_a_venv_only_with_venv_true(tmp_path):
    assert venvs.wanted(tmp_path) is None, "no pyproject.toml"
    assert venvs.wanted(write(tmp_path, '[project]\nname = "x"\n')) is None
    assert venvs.wanted(write(tmp_path, "[tool.evo-agents.worker]\nextras = ['test']\n")) is None
    assert venvs.wanted(write(tmp_path, "[tool.evo-agents.worker]\nvenv = false\n")) is None
    assert venvs.wanted(write(tmp_path, "[tool.evo-agents.worker]\nvenv = true\n")) == venvs.Wanted()
    want = venvs.wanted(
        write(tmp_path, ASKS.replace('extras = ["test"]', 'extras = ["test", "test"]\ngroups = ["lint"]'))
    )
    assert want == venvs.Wanted(("test",), ("lint",), ("plugins.txt",))
    assert want.commands("/opt/uv") == [
        ["/opt/uv", "sync", "--frozen", "--extra", "test", "--group", "lint"],
        ["/opt/uv", "pip", "install", "--python", ".venv/bin/python", "--no-deps", "-r", "plugins.txt"],
    ]
    assert venvs.shown(want.commands("/opt/uv")[0]) == "uv sync --frozen --extra test --group lint"


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("[tool.evo-agents.worker\n", "pyproject.toml is not read"),
        ("[tool.evo-agents]\nworker = 1\n", "is not a table"),
        ("[tool.evo-agents.worker]\nvenv = 'yes'\n", "venv of [tool.evo-agents.worker] is true or false"),
        ("[tool.evo-agents.worker]\nvenv = true\nextras = 'test'\n", "extras of [tool.evo-agents.worker] is a list"),
        ("[tool.evo-agents.worker]\nvenv = true\nextras = ['--all']\n", "'--all' is not a name uv takes"),
        ("[tool.evo-agents.worker]\nvenv = true\nrequirements = ['../x.txt']\n", "is not a path inside the repo"),
        ("[tool.evo-agents.worker]\nvenv = true\nrequirements = ['/etc/x.txt']\n", "is not a path inside the repo"),
    ],
)
def test_settings_that_are_not_as_the_module_says_are_refused(tmp_path, text, said):
    with pytest.raises(ValueError, match=None) as caught:
        venvs.wanted(write(tmp_path, text))
    assert said in str(caught.value)


def test_front_puts_the_directories_first_each_once():
    assert venvs.front("/a:/v/bin:/b", ["/v/bin", "/w/bin"]) == "/v/bin:/w/bin:/a:/b"
    assert venvs.front(None, ["/v/bin"]).startswith("/v/bin:")
    assert venvs.uv_env({"PATH": "/a", "VIRTUAL_ENV": "/x", "UV_PROJECT_ENVIRONMENT": "/y"}) == {"PATH": "/a"}
