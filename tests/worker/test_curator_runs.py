"""The Curator's runs on the worker, without Postgres: the daemon in this process with the fake runtime, the in-memory
hub, and the two checkouts (alpha and beta) of ``tests.worker.test_plan_runs``, whose origins are bare repositories on
disk; one of them answers git's push options as GitLab does, a pre-receive hook keeping what it was sent.

The checks step 6 of the curator-agent plan names on the worker: a judge run reads the change at the commit it judges,
runs the plan's verify and the project's hidden checks (whose commands reach no event, log line, file or prompt),
finds the signs of score hacking in the diff before its agent starts (and does not start it when there is one), posts
its verdict and pushes nothing; a Builder of the Curator pushes its branch curator/... alone and never a default branch,
and to GitLab with the push options that open a merge request; the watchdog stops a run of the Curator whose worktree
touched a protected path of the charter, or that passed its cost or time cap, and the run fails saying why."""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker import gitops
from tests.worker.test_plan_runs import (
    PLAN,
    git,
    machine,  # noqa: F401 (a fixture)
    wait_for,
    with_daemon,
)

BRANCH = "curator/1-wait-helper"
MARK = "HIDDEN-CHECK-7f3a"  # in the hidden check's command alone: found anywhere else, it leaked
PROTECTED = ["curator.yaml", ".github/workflows/**"]
TEST_FILE = "def test_wait():\n    assert wait_for()\n"


def change_branch(machine, files: dict[str, str], *, branch: str = BRANCH) -> str:  # noqa: F811
    """``branch`` of alpha's origin with one commit over main that writes ``files``; its head."""
    checkout = machine.checkouts["alpha"]
    git("checkout", "--quiet", "-b", branch, cwd=checkout)
    for name, text in files.items():
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git("add", "-A", cwd=checkout)
    git("commit", "--quiet", "-m", "the Builder's change", cwd=checkout)
    git("push", "--quiet", "origin", branch, cwd=checkout)
    head = git("rev-parse", "HEAD", cwd=checkout)
    git("checkout", "--quiet", "main", cwd=checkout)
    return head


def inputs(machine, head: str | None, *, hidden: list[str] | None = None) -> dict:  # noqa: F811
    marker = machine.tmp / "hidden-ran"
    return {
        "change_id": 1,
        "repo": "alpha",
        "branch": BRANCH,
        "base_branch": "main",
        "head_sha": head,
        "proposal": {"id": 1, "title": "A wait helper", "kind": "test_add", "tier": 0, "summary": "Why.", "paths": []},
        "verify": ["test -f tests/test_wait.py"],
        "protected_paths": PROTECTED,
        "hidden_checks": hidden if hidden is not None else [f"test -f tests/test_wait.py && echo {MARK} > {marker}"],
    }


def curator(head: str | None, **extra) -> dict:
    return {
        "change_id": 1,
        "branch": BRANCH,
        "forge": "github",
        "base_branch": "main",
        "head_sha": head,
        "protected_paths": PROTECTED,
        **extra,
    }


def leaked(machine, hub, run_id: int) -> list[str]:  # noqa: F811
    """Where MARK shows up besides the hidden check's own command: the run's events, its log and files on the worker,
    the prompts the agent was given."""
    found = []
    if MARK in json.dumps(hub.runs[run_id]["events"]):
        found.append("the run's events")
    for path in machine.home.run_dir(run_id).rglob("*"):
        if path.is_file() and MARK in path.read_text(encoding="utf-8", errors="replace"):
            found.append(str(path))
    if machine.starts_path.exists() and MARK in machine.starts_path.read_text(encoding="utf-8"):
        found.append("the agent's prompt")
    return found


# The judge run


def test_judge_run_reads_the_change_runs_the_checks_and_posts_its_verdict(machine):  # noqa: F811
    head = change_branch(machine, {"tests/test_wait.py": TEST_FILE})
    base = machine.seeds["alpha"]
    machine.scenarios(
        {
            "judge": [
                {"sh": "git -C alpha rev-parse HEAD; git -C alpha symbolic-ref -q HEAD || echo detached"},
                {"sh": "git -C alpha diff --name-only origin/main...HEAD"},
                {"write": {".evo-run/verdict.json": json.dumps({"verdict": "pass", "reasons": "The test is real."})}},
            ]
        }
    )
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_judge_run("alpha", curator(head), inputs(machine, head))
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
    (sent,) = hub.verdicts
    assert sent["verdict"] == "pass" and sent["reasons"] == "The test is real."
    assert (sent["head_sha"], sent["base_sha"]) == (head, base)
    assert [(item["command"], item["exit_code"]) for item in sent["verify"]] == [("test -f tests/test_wait.py", 0)]
    assert [(item["index"], item["exit_code"]) for item in sent["hidden"]] == [(1, 0)]
    assert sent["signs"] == []
    assert (machine.tmp / "hidden-ran").read_text(encoding="utf-8").strip() == MARK  # it ran, in the worktree
    commands = [entry for entry in machine.commands() if entry["run"] == run_id]
    at, branch = commands[0]["stdout"].split()
    assert (at, branch) == (head, "detached")  # at the commit judged, on no branch
    assert commands[1]["stdout"].split() == ["tests/test_wait.py"]
    (start,) = [item for item in machine.starts() if item["run"] == run_id]
    assert (
        "hidden check 1 exited 0" in start["prompt"]
        and "verify `test -f tests/test_wait.py` exited 0" in (start["prompt"])
    )
    assert leaked(machine, hub, run_id) == []
    assert any("hidden check 1 of 1 exited 0" in text for text in hub.texts(run_id))
    assert machine.origin("alpha", "for-each-ref", "--format=%(refname)", "refs/heads").split() == [
        f"refs/heads/{BRANCH}",
        "refs/heads/main",
    ]
    assert machine.origin_rev("alpha", f"refs/heads/{BRANCH}") == head  # nothing pushed
    assert hub.judge_reads == [run_id]


def test_judge_run_with_a_sign_of_score_hacking_fails_the_change_without_its_agent(machine):  # noqa: F811
    hacked = "import pytest\n\n\n@pytest.mark.skip(reason='later')\ndef test_wait():\n    assert wait_for()\n"
    head = change_branch(machine, {"tests/test_wait.py": hacked, "curator.yaml": "auto_merge: [0, 1]\n"})
    machine.scenarios({"judge": [{"write": {".evo-run/verdict.json": '{"verdict": "pass"}'}}]})
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_judge_run("alpha", curator(head), inputs(machine, head))
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    (sent,) = hub.verdicts
    assert sent["verdict"] is None  # no agent, no verdict of its own
    assert {item["kind"] for item in sent["signs"]} == {"skip_added", "protected_path"}
    starts = machine.starts() if machine.starts_path.exists() else []
    assert [item for item in starts if item["run"] == run_id] == []  # the Judge's agent never started
    assert any("signs of score hacking (protected_path, skip_added)" in text for text in hub.texts(run_id))


def test_judge_run_of_a_gitlab_change_reads_the_tip_of_its_branch(machine):  # noqa: F811
    head = change_branch(machine, {"tests/test_wait.py": TEST_FILE})
    machine.scenarios({"judge": [{"write": {".evo-run/verdict.json": '{"verdict": "fail", "reasons": "Too thin."}'}}]})
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_judge_run("alpha", curator(None, forge="gitlab"), inputs(machine, None, hidden=[]))
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(hub=hub)

    with_daemon(machine, body)
    (sent,) = found["hub"].verdicts
    assert (sent["verdict"], sent["reasons"], sent["head_sha"], sent["hidden"]) == ("fail", "Too thin.", head, [])


def test_judge_run_refuses_a_head_origin_does_not_have(machine):  # noqa: F811
    change_branch(machine, {"tests/test_wait.py": TEST_FILE})
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_judge_run("alpha", curator("9" * 40), inputs(machine, "9" * 40))
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "failed"
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    assert "has no commit 999999999999" in found["hub"].runs[found["run"]]["error"]
    assert found["hub"].verdicts == []


# A Builder of the Curator


def curator_plan(branch: str = BRANCH) -> dict:
    return {
        "id": PLAN,
        "title": "A wait helper",
        "goal": "No sleep.",
        "repos": [{"repo": "alpha", "branch": branch}],
        "steps": [{"id": 1, "title": "Helper", "repo": "alpha", "what": "write it", "status": "pending"}],
    }


def builder_spec(branch: str = BRANCH, **extra) -> dict:
    return {"curator": {"role": "builder", **curator(None, branch=branch, **extra)}}


def test_curator_builder_pushes_its_branch_alone_and_never_a_default_branch(machine):  # noqa: F811
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"write": {"alpha/tests/test_wait.py": TEST_FILE}},
                {"cli": ["step", "1", "done", "--verify", "test -f tests/test_wait.py"]},
                {"result": {"summary": "Wrote the test."}},
            ]
        }
    )
    found = {}

    async def body(hub, daemon):
        main_run = hub.queue_plan_run(PLAN, [{"repo": "alpha", "branch": "main"}], **builder_spec("main"))
        assert await hub.wait_state(main_run, "done", "failed", timeout=60) == "failed"
        hub.put_plan(curator_plan())
        run_id = hub.queue_plan_run(PLAN, [{"repo": "alpha", "branch": BRANCH}], **builder_spec())
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(hub=hub, main=main_run, run=run_id)

    with_daemon(machine, body, plan=curator_plan("main"))
    hub = found["hub"]
    assert "main is a default branch: a run of the Curator never pushes it" in hub.runs[found["main"]]["error"]
    assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"]  # main never moved
    pushed = machine.origin_rev("alpha", f"refs/heads/{BRANCH}")
    assert pushed is not None and hub.step_reports[0]["commit_sha"] == pushed
    assert hub.notices == []  # no push to a default branch to tell of
    record = machine.home.load_run(found["run"])
    assert record["curator"]["role"] == "builder" and record["curator"]["targets"] == {"alpha": "main"}


def test_curator_push_rules_of_gitops(machine):  # noqa: F811
    path = machine.checkouts["alpha"]
    (path / "c.txt").write_text("c\n", encoding="utf-8")
    git("add", "c.txt", cwd=path)
    git("commit", "--quiet", "-m", "c", cwd=path)

    async def go():
        for branch, why in (
            ("main", "a run of the Curator never pushes it"),
            ("master", "a run of the Curator never pushes it"),
            ("release", "is a default branch"),
            ("feat/x", "pushes curator/... alone"),
            ("curator/", "pushes curator/... alone"),
        ):
            with pytest.raises(gitops.PushRefused) as refused:
                await gitops.push(path, branch, protected=("release",), kind="curator", plan_branch=branch)
            assert why in str(refused.value), (branch, refused.value)
        for kind in ("review", "judge"):
            with pytest.raises(gitops.PushRefused, match=f"a {kind} run reads and pushes nothing"):
                await gitops.push(path, BRANCH, kind=kind)
        assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"]
        pushed = await gitops.push(path, BRANCH, protected=("main",), kind="curator", plan_branch="main")
        assert pushed.changed and not pushed.default

    asyncio.run(go())
    assert machine.origin_rev("alpha", f"refs/heads/{BRANCH}") == git("rev-parse", "HEAD", cwd=path)


def gitlab_origin(machine) -> object:  # noqa: F811
    """alpha's origin taking push options, as GitLab does, with a pre-receive hook that writes them to a file."""
    origin = machine.origins["alpha"]
    git("--git-dir", str(origin), "config", "receive.advertisePushOptions", "true")
    seen = machine.tmp / "push-options.txt"
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f'i=0; while [ "$i" -lt "${{GIT_PUSH_OPTION_COUNT:-0}}" ]; do eval "echo \\$GIT_PUSH_OPTION_$i" >> {seen}; '
        "i=$((i+1)); done\ncat > /dev/null\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return seen


def test_curator_builder_on_gitlab_opens_its_merge_request_with_push_options(machine):  # noqa: F811
    seen = gitlab_origin(machine)
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"write": {"alpha/tests/test_wait.py": TEST_FILE}},
                {"cli": ["step", "1", "done", "--verify", "test -f tests/test_wait.py"]},
                {"write": {"alpha/tests/test_more.py": TEST_FILE}},
                {"result": {"summary": "Wrote the tests."}},
            ]
        }
    )
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_plan_run(PLAN, [{"repo": "alpha", "branch": BRANCH}], **builder_spec(forge="gitlab"))
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(hub=hub)

    with_daemon(machine, body, plan=curator_plan())
    sent = seen.read_text(encoding="utf-8").splitlines()
    assert sent[:3] == ["merge_request.create", "merge_request.target=main", "merge_request.remove_source_branch"]
    assert "merge_request.title=Helper" in sent  # from `worker step`; the push at the end names the run's title
    assert sent.count("merge_request.create") == 2  # the step's push and the push at the end
    assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"]


# The watchdog


def test_watchdog_stops_a_builder_whose_worktree_touches_a_protected_path(machine):  # noqa: F811
    never = machine.tmp / "never"
    machine.scenarios(
        {f"plan:{PLAN}": [{"write": {"alpha/curator.yaml": "auto_merge: [0, 1]\n"}}, {"wait_for": str(never)}]}
    )
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_plan_run(PLAN, [{"repo": "alpha", "branch": BRANCH}], **builder_spec())
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "failed"
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body, plan=curator_plan())
    error = found["hub"].runs[found["run"]]["error"]
    assert error == (
        "the watchdog of the Curator's runs stopped it: alpha:curator.yaml is protected by the charter (curator.yaml)"
    )
    assert machine.origin_rev("alpha", f"refs/heads/{BRANCH}") is None  # nothing pushed


def test_watchdog_stops_a_run_of_the_curator_past_its_cost_cap(machine):  # noqa: F811
    never = machine.tmp / "never"
    machine.scenarios({"review": [{"samples": True}, {"wait_for": str(never)}]})
    found = {}
    budget = {"max_usd": 0.1, "max_turns": 40, "max_seconds": 1800, "spent_usd": 0.0, "spent_seconds": 0}

    async def body(hub, daemon):
        run_id = hub.queue_review_run(
            ["alpha"], budget=budget, curator={"role": "reviewer", "protected_paths": PROTECTED}
        )
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "failed"
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    assert "the run cost more than its cost cap of $0.10" in found["hub"].runs[found["run"]]["error"]


def test_watchdog_stops_a_run_of_the_curator_past_its_time_cap(machine):  # noqa: F811
    never = machine.tmp / "never"
    machine.scenarios({"review": [{"wait_for": str(never)}]})
    found = {}
    budget = {"max_usd": 5.0, "max_turns": 40, "max_seconds": 1, "spent_usd": 0.0, "spent_seconds": 0}

    async def body(hub, daemon):
        run_id = hub.queue_review_run(["alpha"], budget=budget, curator={"role": "reviewer", "protected_paths": []})
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "failed"
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    assert "more than its time cap" in found["hub"].runs[found["run"]]["error"]


def test_watchdog_leaves_a_members_plan_run_alone(machine):  # noqa: F811
    gate = machine.tmp / "gate"
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"write": {"alpha/curator.yaml": "anything\n"}},
                {"touch": str(gate)},
                {"sleep": 1.0},
                {"cli": ["step", "1", "done", "--verify", "test -f curator.yaml"]},
                {"result": {"summary": "A member's own plan may touch it."}},
            ]
        }
    )
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_plan_run(PLAN, [{"repo": "alpha", "branch": "feat/alpha"}])
        await wait_for(gate.exists, "the agent to write the file")
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(hub=hub)

    with_daemon(machine, body, plan=curator_plan("feat/alpha"))
    assert found["hub"].step_reports[0]["status"] == "done"


def test_curator_watch_reads_every_change_of_a_worktree(machine):  # noqa: F811
    path = machine.checkouts["alpha"]
    base = machine.seeds["alpha"]
    (path / "committed.txt").write_text("c\n", encoding="utf-8")
    git("add", "committed.txt", cwd=path)
    git("commit", "--quiet", "-m", "c", cwd=path)
    (path / "README.md").write_text("changed\n", encoding="utf-8")
    (path / ".github" / "workflows").mkdir(parents=True)
    (path / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    (path / ".evo-run").mkdir()
    (path / ".evo-run" / "result.json").write_text("{}", encoding="utf-8")
    found = asyncio.run(gitops.changed_paths(path, base))
    assert found == [".github/workflows/ci.yml", "README.md", "committed.txt"]
    done = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True)
    assert ".evo-run" in done.stdout  # there, and left out of what the watchdog reads
