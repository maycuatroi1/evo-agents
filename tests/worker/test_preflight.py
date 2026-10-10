"""The preflight of a run on the worker (``evo_agents.worker.preflight``), without Postgres: what it reads in a verify
command, and runs of each kind that it stops before anything is fetched or the agent starts, with the daemon in this
process, the fake runtime and the in-memory hub of ``tests.worker.test_plan_runs``.

The checks step 2 of the run-reliability plan names on the worker: a repo the project lists no origin for, a repo git
cannot read or push to with what the run holds, and a program a verify command calls that is not on the run's PATH each
fail the run with that cause (``failure_cause``) and a message naming the repo or the program; the agent never starts,
no worktree is made, and the run is reported failed, which the hub does not try again."""

from __future__ import annotations

import os
import stat
import subprocess

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub.judge import programs
from evo_agents.worker import preflight
from tests.worker.git_http import GitHttp
from tests.worker.test_plan_runs import (
    PLAN,
    git,
    machine,  # noqa: F401 (a fixture)
    plan_body,
    run_repos,
    wait_for,
    with_daemon,
)

MISSING = "no-such-tool-7f3a"  # a program no PATH has
RUN_9 = "fatal: could not read Username for 'https://github.com': terminal prompts disabled"


# What the preflight reads


@pytest.mark.parametrize(
    "command, found",
    [
        ("ruff check . && ruff format --check . && cd web && pnpm gen:api", ["ruff", "pnpm"]),
        ("EVO_HUB_TEST_DSN=postgresql://x python -m pytest -q -k 'a or b'", ["python"]),
        ("pytest -q 2>&1 | tail -5", ["pytest", "tail"]),
        ("test -f a.txt && echo ok > /tmp/out", []),
        ("timeout 60 cargo test", ["timeout", "cargo"]),
        ("env FOO=1 node x.js", ["env", "node"]),
        ("if grep -q x f; then make check; fi", ["grep", "make"]),
        ("for f in a b; do shellcheck $f; done", ["shellcheck"]),
        ("sh -c 'rg -q foo && jq . x.json'", ["sh", "rg", "jq"]),
        ("./scripts/check.sh && /usr/bin/true", ["/usr/bin/true"]),
        ("uv run pytest", ["uv"]),
        ("PATH=.venv/bin:$PATH pytest", []),
        ("source .venv/bin/activate && pytest", []),
        ("export PATH=$HOME/bin:$PATH; mytool", []),
        ("$TOOL --check", []),
        ("echo 'open quote", []),
    ],
)
def test_preflight_reads_the_programs_a_verify_command_calls(command, found):
    assert programs(command) == found


def test_preflight_looks_for_each_program_on_the_runs_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "mytool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    path = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    commands = [f"mytool --check && {MISSING} run", f"{MISSING} again", "/no/such/program x", "sh -c true"]
    assert preflight.missing_programs(commands, path) == [
        (MISSING, f"mytool --check && {MISSING} run"),
        ("/no/such/program", "/no/such/program x"),
    ]
    assert preflight.missing_programs(["mytool"], "/usr/bin") == [("mytool", "mytool")]
    (problem,) = preflight.verify_problems([f"{MISSING} --version"], path)
    assert problem.cause == "missing_tool"
    assert (
        problem.message == f"verify command `{MISSING} --version` calls {MISSING}, which is not on this worker's PATH"
    )
    (hidden,) = preflight.hidden_problems(["true", f"{MISSING} secret-flag"], path)
    assert hidden.message == "hidden check 2 of 2 calls a program that is not on this worker's PATH"
    assert MISSING not in hidden.message and "secret-flag" not in hidden.message


@pytest.mark.parametrize(
    "said, refused",
    [
        (RUN_9, True),
        ("remote: Permission to me/repo.git denied to bot. fatal: ... The requested URL returned error: 403", True),
        ("git@github.com: Permission denied (publickey). fatal: Could not read from remote repository.", True),
        ("remote: Repository not found. fatal: repository 'https://github.com/me/x.git/' not found", True),
        ("ERROR: The key you are authenticating with has been marked as read only.", True),
        ("fatal: unable to access 'https://h/x.git/': Could not resolve host: h", False),
        ("! [rejected] HEAD -> evo-run/preflight-7 (non-fast-forward)", False),
    ],
)
def test_preflight_tells_a_remote_that_wants_a_credential_from_other_trouble(said, refused):
    assert preflight.refused(said) is refused


def test_preflight_asks_no_credential_for_an_origin_on_this_machine():
    assert preflight.is_local("/srv/git/alpha.git") and preflight.is_local("file:///srv/git/alpha.git")
    assert not preflight.is_local("git@github.com:me/alpha.git") and not preflight.is_local("http://h/alpha.git")


# Runs it stops


def step_plan(verify: str) -> dict:
    body = plan_body()
    body["steps"][0]["verify"] = verify
    return body


def stopped(hub, run_id: int, machine) -> dict:  # noqa: F811
    """What a run the preflight stopped left: its record on the hub, after the checks every such run passes."""
    run = hub.runs[run_id]
    assert hub.moves(run_id) == ["leased", "failed"], hub.texts(run_id)
    starts = machine.starts() if machine.starts_path.exists() else []
    assert [item for item in starts if item["run"] == run_id] == [], "the agent never started"
    assert not machine.directory(run_id).exists(), "no worktree, no directory of the run"
    assert not any(text.startswith("Fetching origin") for text in hub.texts(run_id)), "nothing was fetched"
    assert ("ask", run_id) in hub.credential_calls, "the leases were taken first"
    return run


def test_preflight_a_step_run_whose_verify_calls_a_missing_tool_fails_before_its_agent(machine):  # noqa: F811
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        assert await hub.wait_state(run_id, "done", "failed") == "failed", hub.texts(run_id)
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body, plan=step_plan(f"test -f a.txt && {MISSING} --check a.txt"))
    hub, run_id = found["hub"], found["run"]
    run = stopped(hub, run_id, machine)
    assert run["failure_cause"] == "missing_tool"
    assert run["error"] == (
        f"preflight: verify command `test -f a.txt && {MISSING} --check a.txt` calls {MISSING}, which is not on "
        "this worker's PATH; the agent did not start"
    )
    texts = hub.texts(run_id)
    assert f"Preflight: verify command `test -f a.txt && {MISSING} --check a.txt` calls {MISSING}" in " ".join(texts)
    assert machine.origin_rev("alpha", "refs/heads/feat/alpha") is None, "nothing was pushed"


def test_preflight_a_plan_run_names_every_repo_without_origin_and_every_missing_tool(machine):  # noqa: F811
    body = plan_body()
    body["steps"][1]["verify"] = f"{MISSING} lint && git status"
    reason = "project demo lists no origin for beta"
    found = {}

    async def run_it(hub, daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(body))
        hub.leases[run_id] = {"leases": [], "missing": [{"repo": "beta", "origin": None, "reason": reason}]}
        assert await hub.wait_state(run_id, "done", "failed") == "failed", hub.texts(run_id)
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(hub=hub, run=run_id)

    with_daemon(machine, run_it, plan=body)
    hub, run_id = found["hub"], found["run"]
    run = stopped(hub, run_id, machine)
    assert run["failure_cause"] == "origin", "the cause of the first problem"
    error = run["error"]
    assert error.startswith("preflight: project demo lists no origin for beta, so no credential is leased for it")
    assert f"verify command `{MISSING} lint && git status` calls {MISSING}" in error
    notes = [event["body"] for event in hub.runs[run_id]["events"] if event["kind"] == "system"]
    assert [note.get("cause") for note in notes if note["text"].startswith("Preflight: ")] == ["origin", "missing_tool"]


def test_preflight_a_review_run_of_a_repo_without_origin_fails_with_that_cause(machine):  # noqa: F811
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_review_run(["alpha"])
        missing = {"repo": "alpha", "origin": None, "reason": "project demo lists no origin for alpha"}
        hub.leases[run_id] = {"leases": [], "missing": [missing]}
        assert await hub.wait_state(run_id, "done", "failed") == "failed", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    run = stopped(found["hub"], found["run"], machine)
    assert run["failure_cause"] == "origin" and "lists no origin for alpha" in run["error"]


def test_preflight_an_author_run_whose_harness_has_no_origin_fails_with_that_cause(machine):  # noqa: F811
    from evo_agents.hub import author
    from tests.worker.test_author_run import skill_bundle

    found = {}

    async def body(hub, daemon):
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp))
        run_id = hub.queue_author_run(["alpha", "beta"], [ticket])
        missing = {"repo": "alpha", "origin": None, "reason": "project demo lists no origin for alpha"}
        hub.leases[run_id] = {"leases": [], "missing": [missing]}
        assert await hub.wait_state(run_id, "done", "failed") == "failed", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    run = stopped(found["hub"], found["run"], machine)
    assert run["failure_cause"] == "origin" and "lists no origin for alpha" in run["error"]
    assert "beta" not in run["error"], "beta has its origin"


@pytest.fixture
def hermetic(machine):  # noqa: F811
    """The machine with no credential of its own: git reads no global or system configuration."""
    empty = machine.tmp / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    machine.env.update({"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    return machine


def test_preflight_a_step_run_git_cannot_push_with_what_it_holds_fails_naming_the_repo(machine, hermetic):  # noqa: F811
    with GitHttp(machine.tmp, "oauth2", "never-given", anonymous_reads=True) as server:
        machine.env.update(server.env)
        url = server.repo_url("alpha.git")
        git("remote", "set-url", "origin", url, cwd=machine.checkouts["alpha"])
        found = {}

        async def body(hub, daemon):
            run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
            assert await hub.wait_state(run_id, "done", "failed") == "failed", hub.texts(run_id)
            found.update(hub=hub, run=run_id)

        with_daemon(machine, body)
        served = list(server.requests)
    hub, run_id = found["hub"], found["run"]
    run = stopped(hub, run_id, machine)
    assert run["failure_cause"] == "credentials"
    assert run["error"].startswith(
        f"preflight: git cannot push to alpha at {url} with this machine's own credentials (no lease of the run "
        "covers it): "
    ), run["error"]
    assert "could not read Username" in run["error"], "the remote asked for a user to push, and git had none"
    reads = [status for verb, path, user, status in served if "upload-pack" in path or verb == "GET"]
    assert 200 in reads, "it read the remote first, which takes no credential"
    assert not any(path.endswith("/git-receive-pack") for _, path, _, _ in served), "a dry run sends nothing"


def test_preflight_a_review_run_git_cannot_read_fails_with_gits_own_words(machine, hermetic):  # noqa: F811
    with GitHttp(machine.tmp, "oauth2", "never-given") as server:
        machine.env.update(server.env)
        url = server.repo_url("alpha.git")
        git("remote", "set-url", "origin", url, cwd=machine.checkouts["alpha"])
        found = {}

        async def body(hub, daemon):
            run_id = hub.queue_review_run(["alpha"])
            hub.leases[run_id] = {"leases": [], "missing": [{"repo": "alpha", "origin": url, "reason": "no secret"}]}
            assert await hub.wait_state(run_id, "done", "failed") == "failed", hub.texts(run_id)
            found.update(hub=hub, run=run_id)

        with_daemon(machine, body)
    run = stopped(found["hub"], found["run"], machine)
    assert run["failure_cause"] == "credentials"
    assert run["error"].startswith(
        f"preflight: git cannot read alpha at {url} with this machine's own credentials (the hub leased none: no "
        "secret): "
    ), run["error"]
    assert "could not read Username" in run["error"]


def test_preflight_passes_a_run_whose_repos_and_programs_are_all_there(machine):  # noqa: F811
    found = {}
    machine.scenarios({"1": [{"write": {"a.txt": "a\n"}}, {"result": {"verify_commands": ["test -f a.txt"]}}]})

    async def body(hub, daemon):
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        assert await hub.wait_state(run_id, "done", "failed", "review") == "done", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body, plan=step_plan("git status && test -f a.txt"))
    hub, run_id = found["hub"], found["run"]
    assert "Preflight passed: 1 repo(s) and the programs of 1 command(s)." in hub.texts(run_id)
    assert hub.runs[run_id].get("failure_cause") is None
    done = subprocess.run(
        ["git", "--git-dir", str(machine.origins["alpha"]), "rev-parse", "--verify", "-q", "refs/heads/feat/alpha"],
        capture_output=True,
        text=True,
    )
    assert done.stdout.strip(), "the run went on and pushed"
