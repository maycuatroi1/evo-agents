"""A judge run of the hub's merge check of a pull request on github.com (plan super-board-parity-2, step 1;
``evo_agents.worker.runner.judge``): its claim says ``merge_result``, so the worker merges origin's base into the head
in the object store, runs the plan's verify and the hidden checks on that merge, and leaves the Judge's agent at the
head itself; a merge that conflicts posts the paths, with no check run and no agent, for the hub to fail it with
needs a rebase. The repos are local ones and the hub is the fake of ``tests.worker.test_plan_runs``.
"""

from __future__ import annotations

import os

from evo_agents.hub import judge
from evo_agents.worker.runner import judge as judge_run
from tests.worker.test_curator_runs import TEST_FILE, change_branch, curator
from tests.worker.test_judge_hardening import judge_inputs, judged
from tests.worker.test_plan_runs import git, machine, with_daemon  # noqa: F401 (machine is a fixture)

PULL = {"change_id": None, "forge": "github", "pr_url": "https://github.com/northwind/alpha/pull/7"}
CHECK = {**PULL, "merge_result": True}
# A uv that logs its arguments outside the worktree and, for sync, makes .venv/bin/venv-tool.
FAKE_UV = """#!/bin/sh
echo "$*" >> {log}
if [ "$1" = sync ]; then
  mkdir -p .venv/bin && printf '#!/bin/sh\\necho from-the-venv\\n' > .venv/bin/venv-tool && chmod +x .venv/bin/venv-tool
fi
"""
PYPROJECT = '[project]\nname = "alpha"\nversion = "0"\n\n[tool.evo-agents.worker]\nvenv = true\nextras = ["test"]\n'


def base_moved(machine, files: dict[str, str]) -> str:  # noqa: F811
    """origin's main of alpha one commit further, writing ``files``; its head."""
    checkout = machine.checkouts["alpha"]
    git("checkout", "--quiet", "main", cwd=checkout)
    for name, text in files.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=checkout)
    git("commit", "--quiet", "-m", "the base moved on", cwd=checkout)
    git("push", "--quiet", "origin", "main", cwd=checkout)
    return git("rev-parse", "HEAD", cwd=checkout)


def test_github_merge_check_runs_the_checks_on_the_head_merged_with_the_base_and_the_judge_at_the_head(machine):  # noqa: F811
    head = change_branch(machine, {"tests/test_wait.py": TEST_FILE})
    base_moved(machine, {"docs/base-only.md": "The base moved on.\n"})
    seen = machine.tmp / "agent-saw.txt"
    verify = ["test -f tests/test_wait.py && test -f docs/base-only.md"]
    found = judged(
        machine,
        head,
        {**judge_inputs(head, ["test -f docs/base-only.md"], verify), "queue_id": 3},
        [
            {
                "sh": f"if test -f alpha/docs/base-only.md; then echo merged; elif test -f alpha/tests/test_wait.py; then echo head; else echo lost; fi > {seen}"
            },
            {"say": '{"verdict": "pass", "reasons": "Fine."}'},
        ],
        claim=CHECK,
    )
    (sent,) = found["hub"].verdicts
    assert sent["route"] == "merge-verdict" and sent.get("conflicts") is None
    assert [item["exit_code"] for item in sent["verify"]] == [0], "the verify ran on the merge with the base"
    assert [item["exit_code"] for item in sent["hidden"]] == [0], "so did the hidden check"
    assert sent["head_sha"] == head and sent["paths"] == ["tests/test_wait.py"], "the Judge reads the head's own diff"
    assert seen.read_text(encoding="utf-8").strip() == "head", "the Judge's worktree is at the head"


def test_github_merge_check_a_conflict_with_the_base_posts_its_paths_and_runs_nothing(machine):  # noqa: F811
    head = change_branch(machine, {"tests/test_wait.py": TEST_FILE})
    base_moved(machine, {"tests/test_wait.py": "def test_other():\n    assert True\n"})
    ran = machine.tmp / "hidden-ran"
    found = judged(
        machine,
        head,
        {**judge_inputs(head, [f"touch {ran}"]), "queue_id": 3},
        [{"say": '{"verdict": "pass", "reasons": "Should not be asked."}'}],
        claim=CHECK,
    )
    (sent,) = found["hub"].verdicts
    assert sent["conflicts"] == ["tests/test_wait.py"] and sent["verdict"] is None
    assert sent["head_sha"] == head and "needs a rebase" in sent["reasons"]
    assert not ran.exists(), "no check runs on a head that needs a rebase"


def test_github_merge_check_runs_each_check_in_the_python_environment_the_repo_asks_for(machine):  # noqa: F811
    """Run #1579 ran the plan's verify with the machine's python and ruff, older than the change's own. A repo whose
    pyproject asks for an environment of its worktree gets it before each check, made as code of the change, with its
    bin directory first on the check's PATH; the restore between checks removes it, so each check makes it again."""
    tools = machine.tmp / "machine-bin"
    tools.mkdir()
    log = machine.tmp / "uv.log"
    (tools / "uv").write_text(FAKE_UV.format(log=log), encoding="utf-8")
    (tools / "venv-tool").write_text("#!/bin/sh\necho from-the-machine\n", encoding="utf-8")
    for program in ("uv", "venv-tool"):
        (tools / program).chmod(0o755)
    machine.env["PATH"] = f"{tools}{os.pathsep}{machine.env['PATH']}"
    head = change_branch(machine, {"pyproject.toml": PYPROJECT, "tests/test_wait.py": TEST_FILE})
    from_venv = "venv-tool | grep -q from-the-venv"
    hidden = f'{from_venv} && test "$UV_VENV_CLEAR" = 1'  # a check's own `uv venv` replaces the venv made for it
    found = judged(
        machine,
        head,
        {**judge_inputs(head, [hidden], [from_venv, "test -x .venv/bin/venv-tool"]), "queue_id": 3},
        [{"say": '{"verdict": "pass", "reasons": "Fine."}'}],
        claim=CHECK,
    )
    (sent,) = found["hub"].verdicts
    assert [item["exit_code"] for item in sent["verify"]] == [0, 0], "the venv's tool came first on PATH"
    assert [item["exit_code"] for item in sent["hidden"]] == [0]
    syncs = [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("sync")]
    assert syncs == ["sync --frozen --extra test"] * 3, "made again before each check, as the restore removed it"


def test_github_merge_check_merges_in_a_worktree_on_a_git_without_write_tree(machine, monkeypatch):  # noqa: F811
    from evo_agents.worker.runner import judge as runner

    monkeypatch.setattr(runner, "WRITE_TREE", ("merge-tree", "--no-such-option"))  # git 2.34 answers usage, exit 129
    head = change_branch(machine, {"tests/test_wait.py": TEST_FILE})
    base_moved(machine, {"docs/base-only.md": "The base moved on.\n"})
    verify = ["test -f tests/test_wait.py && test -f docs/base-only.md"]
    found = judged(
        machine,
        head,
        {**judge_inputs(head, ["test -f docs/base-only.md"], verify), "queue_id": 3},
        [{"say": '{"verdict": "pass", "reasons": "Fine."}'}],
        claim=CHECK,
    )
    (sent,) = found["hub"].verdicts
    assert sent.get("conflicts") is None and sent["head_sha"] == head
    assert [item["exit_code"] for item in sent["verify"]] == [0], "the verify ran on the merge made in a worktree"
    assert not list(machine.checkouts["alpha"].glob(".git/worktrees/merge*")), "the merge's worktree is gone"


def test_github_merge_check_a_conflict_found_in_a_worktree_posts_its_paths(machine, monkeypatch):  # noqa: F811
    from evo_agents.worker.runner import judge as runner

    monkeypatch.setattr(runner, "WRITE_TREE", ("merge-tree", "--no-such-option"))
    head = change_branch(machine, {"tests/test_wait.py": TEST_FILE})
    base_moved(machine, {"tests/test_wait.py": "def test_other():\n    assert True\n"})
    found = judged(
        machine,
        head,
        {**judge_inputs(head, ["true"]), "queue_id": 3},
        [{"say": '{"verdict": "pass", "reasons": "Should not be asked."}'}],
        claim=CHECK,
    )
    (sent,) = found["hub"].verdicts
    assert sent["conflicts"] == ["tests/test_wait.py"] and sent["verdict"] is None


def test_github_merge_check_has_its_judge_weigh_the_signs_and_posts_a_verdict_again_after_a_502(machine, monkeypatch):  # noqa: F811
    """Run #1673 failed pull request #462 unjudged on the golden contract additions its plan asked for, and run #1671
    lost its verdict to the 502 of a hub that redeployed. A merge check's Judge weighs the signs against the plan, and
    a verdict the hub answers 5xx to is posted again."""
    monkeypatch.setattr(judge_run, "VERDICT_FIRST_DELAY", 0.01)
    head = change_branch(machine, {"curator.yaml": "protected: changed\n", "tests/test_wait.py": TEST_FILE})
    machine.scenarios({"judge": [{"say": '{"verdict": "pass", "reasons": "The plan asks for that file."}'}]})
    found = {}

    async def body(hub, daemon):
        hub.verdicts_down = 1
        inputs = {**judge_inputs(head, ["true"]), "queue_id": 3}
        run_id = hub.queue_judge_run("alpha", {**curator(head), **CHECK}, inputs)
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    hub, run_id = found["hub"], found["run"]
    (sent,) = hub.verdicts
    assert sent["verdict"] == "pass" and [item["kind"] for item in sent["signs"]] == ["protected_path"]
    assert sent["security"] is not None, "the security pass ran on a change with signs too"
    texts = hub.texts(run_id)
    assert any("the Judge weighs each against the plan" in text for text in texts), texts
    assert hub.verdicts_down == 0 and any("did not take the Judge's verdict" in text for text in texts), texts
    (start,) = [item for item in machine.starts() if item["run"] == run_id][:1]
    assert judge.WEIGH_SIGNS.strip() in start["prompt"]
