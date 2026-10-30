"""A judge run of the hub's merge check of a pull request on github.com (plan super-board-parity-2, step 1;
``evo_agents.worker.runner.judge``): its claim says ``merge_result``, so the worker merges origin's base into the head
in the object store, runs the plan's verify and the hidden checks on that merge, and leaves the Judge's agent at the
head itself; a merge that conflicts posts the paths, with no check run and no agent, for the hub to fail it with
needs a rebase. The repos are local ones and the hub is the fake of ``tests.worker.test_plan_runs``.
"""

from __future__ import annotations

from tests.worker.test_curator_runs import TEST_FILE, change_branch
from tests.worker.test_judge_hardening import judge_inputs, judged
from tests.worker.test_plan_runs import git, machine  # noqa: F401 (machine is a fixture)

PULL = {"change_id": None, "forge": "github", "pr_url": "https://github.com/northwind/alpha/pull/7"}
CHECK = {**PULL, "merge_result": True}


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
