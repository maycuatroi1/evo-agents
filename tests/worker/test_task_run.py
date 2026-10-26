"""Task runs on the worker, without Postgres: the daemon in this process with the fake runtime, the in-memory hub, and
the two checkouts (alpha and beta) of ``tests.worker.test_plan_runs`` (``evo_agents.worker.runner.task``).

What step 14 of the plan hub-agent-plugins names on the worker (criterion a9): the daemon says it runs task runs; a
task run's agent works in EVO_RUN_KIND task, one headless turn, in a writable worktree of each repo of its project on
the branch task/<run>; at its end the daemon commits what the agent left and pushes that branch of each repo it
changed, never a default branch, never forced, and tells the hub of each push for the draft pull request; Claude Code
gets the task verbatim and no question tool."""

from __future__ import annotations

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import author, task_runs
from evo_agents.worker import gitops
from evo_agents.worker.run import KINDS, TaskRun
from tests.worker.test_plan_runs import (
    WAIT,
    git,
    machine,  # noqa: F401 (a fixture)
    with_daemon,
)


def task_pulls(hub) -> list[tuple]:
    return [(c["run_id"], c["repo"], c["branch"], c["head"]) for c in hub.chat_calls if c["route"] == "task/pulls"]


def test_a_task_run_pushes_its_task_branch_of_each_repo_it_changed_for_a_draft_pull_request(machine):  # noqa: F811
    machine.scenarios(
        {
            "task": [
                {"sh": "echo $EVO_RUN_KIND; for r in alpha beta; do git -C $r symbolic-ref --short HEAD; done"},
                {"write": {"alpha/wait.py": "def wait_for():\n    return True\n"}},
                {"result": {"summary": "Replaced the sleep in alpha."}},
                {"say": "Done."},
            ]
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_task_run(["alpha", "beta"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
        assert hub.runs[run_id]["summary"] == "Replaced the sleep in alpha."
        assert "task" in hub.last_heartbeat["run_kinds"]
        branch = task_runs.branch_of(run_id)
        (look,) = [entry for entry in machine.commands() if entry["run"] == run_id]
        assert look["stdout"].splitlines() == ["task", branch, branch]
        head = machine.origin_rev("alpha", f"refs/heads/{branch}")
        assert head is not None and machine.origin("alpha", "log", "--format=%s", "-1", branch).startswith(
            f"run #{run_id}: "
        )
        assert machine.origin_rev("beta", f"refs/heads/{branch}") is None, "a repo it did not change is not pushed"
        assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"], "never a default branch"
        assert task_pulls(hub) == [(run_id, "alpha", branch, head)]
        assert any(f"The hub linked {branch} of alpha as a draft pull request" in text for text in hub.texts(run_id))
        assert hub.runs[run_id].get("chat") is None, "a task run says nothing in a chat"
        (start,) = machine.starts()
        assert start["resume"] is None and start["prompt"] == "You are @triager. Fix the wait."
        assert git("symbolic-ref", "--short", "HEAD", cwd=machine.directory(run_id) / "alpha") == branch

    with_daemon(machine, body)


def test_a_task_run_that_changes_nothing_pushes_nothing_and_is_one_turn(machine):  # noqa: F811
    machine.scenarios({"task": [{"result": {"summary": "Nothing to change."}}, {"say": "Nothing to do."}]})

    async def body(hub, daemon):
        run_id = hub.queue_task_run(["alpha"])
        hub.inbox[run_id].append({"id": 1, "text": "more", "decision_id": None, "delivered": False})
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert task_pulls(hub) == [] and len(machine.starts()) == 1
        assert machine.origin_rev("alpha", f"refs/heads/{task_runs.branch_of(run_id)}") is None

    with_daemon(machine, body)


def test_a_task_run_pushes_only_its_task_branch(machine):  # noqa: F811
    assert gitops.check_push("task/41", ("main",), kind="task", plan_branch=None) is False
    for branch, why in (
        ("main", "a default branch: a task run never pushes it"),
        ("trunk", "a default branch"),
        ("chat/41", "not a branch of a task run: a task run pushes task/<run> alone"),
        ("feat/x", "not a branch of a task"),
    ):
        with pytest.raises(gitops.PushRefused, match=why):
            gitops.check_push(branch, ("trunk",), kind="task", plan_branch=branch)
    with pytest.raises(gitops.PushRefused, match="not a branch of a chat run: a chat run pushes chat/<run> alone"):
        gitops.check_push("task/41", (), kind="chat", plan_branch=None)
    assert task_runs.is_task_branch("task/41") and not task_runs.is_task_branch("task/041")
    assert KINDS["task"] is TaskRun


def draft_branch(machine, name: str, branch: str) -> str:  # noqa: F811
    """A branch of ``name`` on origin with one commit past main, as a draft another run opened: its tip."""
    seed = machine.tmp / "seed" / name
    git("checkout", "--quiet", "-b", branch, cwd=seed)
    (seed / "draft.md").write_text("# Northwind launch\n", encoding="utf-8")
    git("add", "draft.md", cwd=seed)
    git("commit", "--quiet", "-m", "draft", cwd=seed)
    git("push", "--quiet", str(machine.origins[name]), branch, cwd=seed)
    return git("rev-parse", "HEAD", cwd=seed)


def test_a_task_run_works_on_the_branch_its_task_names_and_pushes_back_to_it(machine):  # noqa: F811
    machine.scenarios(
        {
            "task": [
                {"sh": "for r in alpha beta; do git -C $r symbolic-ref --short HEAD; done; cat alpha/draft.md"},
                {"write": {"alpha/draft.md": "# Northwind launch\n\nThe second paragraph.\n"}},
                {"write": {"beta/notes.md": "seen\n"}},
                {"result": {"summary": "Wrote the second paragraph."}},
                {"say": "Done."},
            ]
        }
    )
    named = "post/northwind-launch"
    tip = draft_branch(machine, "alpha", named)

    async def body(hub, daemon):
        run_id = hub.queue_task_run(["alpha", "beta"])
        branch = task_runs.branch_of(run_id)
        hub.runs[run_id]["spec"]["repos"][0]["branch"] = named
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        (look,) = [entry for entry in machine.commands() if entry["run"] == run_id]
        assert look["stdout"].splitlines() == [named, branch, "# Northwind launch"]
        head = machine.origin_rev("alpha", f"refs/heads/{named}")
        assert head != tip and machine.origin("alpha", "rev-parse", f"{head}~1") == tip, "on top of the draft"
        assert machine.origin_rev("alpha", f"refs/heads/{branch}") is None, "the named repo pushes no task branch"
        assert machine.origin_rev("beta", f"refs/heads/{branch}") is not None, "the other repos stay on task/<run>"
        assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"], "never a default branch"
        assert sorted((repo, pushed) for _, repo, pushed, _ in task_pulls(hub)) == [("alpha", named), ("beta", branch)]

    with_daemon(machine, body)


def test_a_task_run_never_works_on_a_default_branch_its_task_names(machine):  # noqa: F811
    machine.scenarios({"task": [{"result": {"summary": "Nothing."}}, {"say": "Nothing."}]})

    async def body(hub, daemon):
        run_id = hub.queue_task_run(["alpha"])
        hub.runs[run_id]["spec"]["repos"][0]["branch"] = "main"
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "failed"
        assert any("main is a default branch" in text for text in hub.texts(run_id)), hub.texts(run_id)
        assert machine.starts_path.exists() is False or machine.starts() == [], "the agent never started"
        assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"]

    with_daemon(machine, body)


def test_a_task_run_pushes_the_branch_its_task_names_alone_besides_its_own():
    named = "post/northwind-launch"
    assert gitops.check_push(named, ("main",), kind="task", plan_branch=None, named_branch=named) is False
    for branch, protected in (("main", ("main",)), ("trunk", ("trunk",)), ("post/other", ("main",))):
        with pytest.raises(gitops.PushRefused):
            gitops.check_push(branch, protected, kind="task", plan_branch=None, named_branch=named)
    with pytest.raises(gitops.PushRefused, match="a default branch"):
        gitops.check_push("trunk", ("trunk",), kind="task", plan_branch=None, named_branch="trunk")
    with pytest.raises(gitops.PushRefused, match="not a branch of a chat run"):
        gitops.check_push(named, (), kind="chat", plan_branch=None, named_branch=named)


def test_claude_code_gives_a_task_run_its_task_verbatim_and_no_question_tool(tmp_path):
    from tests.worker.test_budget import claude, command_of
    from tests.worker.test_runtimes import _samples

    task = claude(tmp_path, _samples, kind="task")
    task.options = task.build_options("/opt/bin/claude")
    assert task.options.verbatim_prompts is True
    assert task.options.disallowed_tools == list(author.QUESTION_TOOLS)
    assert "--disallowedTools" in command_of(task)
