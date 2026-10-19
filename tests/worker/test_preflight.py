"""The preflight of a run on the worker (``evo_agents.worker.preflight``), without Postgres: what it reads in a verify
command, and runs of each kind that it stops before anything is fetched or the agent starts, with the daemon in this
process, the fake runtime and the in-memory hub of ``tests.worker.test_plan_runs``.

The checks step 2 of the run-reliability plan names on the worker: a repo the project lists no origin for, a repo git
cannot read or push to with what the run holds, and a program a verify command calls that is not on the run's PATH each
fail the run with that cause (``failure_cause``) and a message naming the repo or the program; the agent never starts,
no worktree is made, and the run is reported failed, which the hub does not try again.

Criterion a1 of the chat-runs-start-fast plan: the preflight asks the origins of a run's repos several at a time, and a
run of several repos (chat, task, plan, review, author) fetches them and makes their worktrees several at a time, at
most ``parallel.GIT_AT_ONCE`` at once (``parallel.each``). With a git that takes a second for each ls-remote and fetch,
ten repos pass the preflight in under 3 seconds and have their worktrees in under 3 seconds; the record keeps the
run's order of its repos, and each repo's notes keep their order in the log.

Criterion a2 of the same plan: a chat run that goes on in the session of the one before, in the worktrees it adopted,
takes leases of its own and asks no origin (no ls-remote, no push --dry-run); it fetches each repo once, several at a
time, and a fetch git refuses fails it with cause credentials before its agent starts."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import stat
import subprocess
import time

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import author
from evo_agents.hub.judge import programs
from evo_agents.worker import preflight
from evo_agents.worker.runner import access, parallel
from evo_agents.worker.runner.author import AuthorRun
from evo_agents.worker.runner.branch_work import BranchWork
from evo_agents.worker.runner.common import RunFailed, Stopped
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.plan import PlanRun
from tests.worker.git_http import GitHttp
from tests.worker.test_author_run import ended_by_owner, skill_bundle
from tests.worker.test_plan_runs import (
    PLAN,
    WAIT,
    Machine,
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


# Several repos at a time


class _Run:
    """What ``parallel.each`` asks of a run: its check, which raises once a stop was asked."""

    def __init__(self):
        self.stop_reason: str | None = None

    def check(self) -> None:
        if self.stop_reason is not None:
            raise Stopped(self.stop_reason)


def test_each_works_on_at_most_its_bound_at_once_in_order_and_gives_the_results_in_order():
    running, most, started = 0, 0, []

    async def work(index: int) -> int:
        nonlocal running, most
        started.append(index)
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01 * (10 - index))  # the later items end first
        running -= 1
        return index * index

    results = asyncio.run(parallel.each(_Run(), list(range(10)), work, at_once=4))
    assert results == [index * index for index in range(10)]
    assert most == 4 and started == list(range(10))
    assert asyncio.run(parallel.each(_Run(), [], work)) == []


def test_each_raises_the_error_of_the_first_item_in_order_and_starts_none_after_one_raised():
    started, finished = [], []

    async def work(index: int) -> None:
        started.append(index)
        if index == 1:
            await asyncio.sleep(0.05)
            raise RunFailed("one", cause="checkout")
        if index == 2:
            await asyncio.sleep(0.02)
            raise RunFailed("two", cause="credentials")  # the first to raise
        await asyncio.sleep(0.1)
        finished.append(index)

    with pytest.raises(RunFailed) as caught:
        asyncio.run(parallel.each(_Run(), list(range(8)), work, at_once=4))
    assert caught.value.error == "one" and caught.value.fields == {"cause": "checkout"}, "the first in order"
    assert started == [0, 1, 2, 3], "nothing starts once one raised"
    assert sorted(finished) == [0, 3], "those started finish"


def test_each_stops_before_the_next_item_once_the_run_was_asked_to_stop():
    run, started, finished = _Run(), [], []

    async def work(index: int) -> None:
        started.append(index)
        if index == 0:
            run.stop_reason = "cancel"
        await asyncio.sleep(0.01)
        finished.append(index)

    with pytest.raises(Stopped) as caught:
        asyncio.run(parallel.each(run, list(range(6)), work, at_once=2))
    assert caught.value.reason == "cancel"
    assert started == finished == [0], "the item started finishes, and none starts after the stop"


TEN = tuple(f"repo{index}" for index in range(10))
SLOW = 1  # seconds the fake git takes for each ls-remote and fetch


@pytest.fixture
def ten(machine, tmp_path, monkeypatch):  # noqa: F811
    """A machine with ten checkouts whose origins read as https, so the preflight asks each (git's insteadOf takes
    each to its bare repository on disk), and a git first on PATH that takes SLOW seconds for each ls-remote and
    fetch, which it writes to slow.log."""
    many = Machine(tmp_path / "ten", TEN)
    for name in TEN:
        url = f"https://git.test/{name}.git"
        git("remote", "set-url", "origin", url, cwd=many.checkouts[name])
        git("config", f"url.{many.origins[name]}.insteadOf", url, cwd=many.checkouts[name])
    bin_dir = tmp_path / "slow-bin"
    bin_dir.mkdir()
    many.slow_log = tmp_path / "slow.log"
    fake = bin_dir / "git"
    fake.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        f'  case "$arg" in ls-remote|fetch) echo "$arg" >> {shlex.quote(str(many.slow_log))}; sleep {SLOW}; break;; esac\n'
        "done\n"
        f'exec {shlex.quote(shutil.which("git"))} "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    many.env["PATH"] = path  # the run's git with its leases: ls-remote, fetch, push
    monkeypatch.setenv("PATH", path)  # the daemon's own git
    return many


def ten_plan() -> dict:
    return {
        "id": PLAN,
        "title": "Ten repos",
        "goal": "Touch ten repos.",
        "repos": [{"repo": name, "branch": f"feat/{name}"} for name in TEN],
        "steps": [{"id": 1, "title": "First file", "repo": TEN[0], "what": "write a.txt", "status": "pending"}],
    }


PREPARES = {  # where each kind makes its worktrees
    "chat": (BranchWork, "prepare"),
    "task": (BranchWork, "prepare"),
    "plan": (PlanRun, "prepare"),
    "review": (RunDirectory, "prepare_detached"),
    "author": (AuthorRun, "prepare"),
}


def timed(monkeypatch, owner, attribute: str, spans: list[float]) -> None:
    """Note in ``spans`` how long each call of ``owner.attribute`` takes."""
    original = getattr(owner, attribute)

    async def measured(*args, **kwargs):
        began = time.monotonic()
        try:
            return await original(*args, **kwargs)
        finally:
            spans.append(time.monotonic() - began)

    monkeypatch.setattr(owner, attribute, measured)


async def run_of(kind: str, hub, machine) -> int:  # noqa: F811
    """A run of ``kind`` over the ten repos, queued, and the state it ended in."""
    if kind == "chat":
        run_id = hub.queue_chat_run(list(TEN))
    elif kind == "task":
        run_id = hub.queue_task_run(list(TEN))
    elif kind == "plan":
        run_id = hub.queue_plan_run(PLAN, run_repos(ten_plan()))
    elif kind == "review":
        run_id = hub.queue_review_run(list(TEN))
    else:
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp))
        run_id = hub.queue_author_run(list(TEN), [ticket])
        assert await ended_by_owner(hub, run_id) == "done", hub.texts(run_id)
        return run_id
    assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
    return run_id


@pytest.mark.parametrize("kind", sorted(PREPARES))
def test_ten_repos_pass_the_preflight_and_get_their_worktrees_in_under_three_seconds_each(kind, ten, monkeypatch):
    checked: list[float] = []
    prepared: list[float] = []
    timed(monkeypatch, access, "check", checked)
    timed(monkeypatch, *PREPARES[kind], prepared)
    found = {}

    async def body(hub, daemon):
        found.update(hub=hub, run=await run_of(kind, hub, ten))
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")

    with_daemon(ten, body, plan=ten_plan())
    hub, run_id = found["hub"], found["run"]
    asked = ten.slow_log.read_text(encoding="utf-8").split()
    assert asked[:20] == ["ls-remote"] * 10 + ["fetch"] * 10, "each origin asked, then each repo fetched"
    # at most parallel.GIT_AT_ONCE (8) at once: two rounds of a second for ten repos
    assert len(checked) == 1 and 2 * SLOW <= checked[0] < 3, checked
    assert len(prepared) == 1 and 2 * SLOW <= prepared[0] < 3, prepared

    texts = hub.texts(run_id)
    assert "Preflight passed: 10 repo(s)." in texts
    assert "Making the worktrees of 10 repos, 8 at a time." in texts
    record = ten.home.load_run(run_id)
    assert [item["repo"] for item in record["repos"]] == list(TEN), "the record keeps the run's order of its repos"
    notes = [event["body"] for event in hub.runs[run_id]["events"] if event["kind"] == "system"]
    for name in TEN:  # each repo's notes in their order, whatever order the repos were worked on in
        said = [
            note["text"] for note in notes if note.get("repo") == name and note["text"].startswith(("Fetch", "Work"))
        ]
        assert len(said) == 2 and said[0].startswith("Fetching origin in "), said
        assert said[1].startswith(f"Worktree {ten.directory(run_id) / name} of {name}"), said


def test_a_run_cancelled_while_its_preflight_asks_the_origins_starts_no_more_and_fetches_nothing(ten):
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_chat_run(list(TEN))
        await wait_for(lambda: ten.slow_log.exists(), "the first round of ls-remote")
        hub.runs[run_id]["cancel"] = True
        assert await hub.wait_state(run_id, "cancelled", "done", "failed", timeout=WAIT) == "cancelled"
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(hub=hub, run=run_id)

    with_daemon(ten, body)
    hub, run_id = found["hub"], found["run"]
    asked = ten.slow_log.read_text(encoding="utf-8").split()
    assert "fetch" not in asked and asked.count("ls-remote") < len(TEN), "the second round never started"
    texts = hub.texts(run_id)
    assert "Cancelled by the owner." in texts
    assert not any(text.startswith(("Fetching origin", "Preflight passed")) for text in texts), texts
    starts = ten.starts() if ten.starts_path.exists() else []
    assert [item for item in starts if item["run"] == run_id] == [], "the agent never started"


# The next run of a chat session (criterion a2 of chat-runs-start-fast)


def test_the_next_chat_run_of_a_session_asks_no_origin_and_fetches_the_worktrees_it_goes_on_in(ten, monkeypatch):
    ten.scenarios({"chat": [{"say": "Plugins add commands."}], "chat/resume": [{"say": "And skills."}]})
    checked: list[float] = []
    prepared: list[float] = []
    timed(monkeypatch, access, "check", checked)
    timed(monkeypatch, BranchWork, "prepare", prepared)
    found = {}

    async def body(hub, daemon):
        first = hub.queue_chat_run(list(TEN))
        assert await hub.wait_state(first, "done", "failed", timeout=WAIT) == "done", hub.texts(first)
        await wait_for(lambda: not daemon.runs, "the first run to let go of its slot")
        before = len(ten.slow_log.read_text(encoding="utf-8").split())
        session = ten.home.load_run(first)["session_id"]
        later = hub.queue_chat_run(list(TEN), resume_of=first, session_id=session, branch=f"chat/{first}")
        hub.thread_reply(later, "owner wrote in the thread (message #4):\nand skills?")
        assert await hub.wait_state(later, "done", "failed", timeout=WAIT) == "done", hub.texts(later)
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(hub=hub, first=first, later=later, before=before)

    with_daemon(ten, body)
    hub, first, later = found["hub"], found["first"], found["later"]
    asked = ten.slow_log.read_text(encoding="utf-8").split()
    assert asked[: found["before"]] == ["ls-remote"] * 10 + ["fetch"] * 10, "the first turn ran the preflight"
    assert asked[found["before"] :] == ["fetch"] * 10, "the next turn asks no origin: it fetches each repo once"
    assert len(checked) == 2 and checked[1] < SLOW, checked
    # the fetch of the ten adopted worktrees, at most parallel.GIT_AT_ONCE (8) at once: two rounds of a second
    assert len(prepared) == 2 and 2 * SLOW <= prepared[1] < 3, prepared
    assert ("ask", later) in hub.credential_calls, "the run takes leases of its own"

    texts = hub.texts(later)
    assert f"Goes on from run #{first} in {ten.directory(first)}, with its worktrees of {', '.join(TEN)}." in texts
    assert "Preflight passed: 10 repo(s) (10 left to their fetch: the run goes on in their worktrees)." in texts
    assert "Fetching origin of the 10 repos the run goes on in, 8 at a time." in texts
    assert not any(text.startswith(("Worktree ", "Making the worktrees")) for text in texts), "no new worktree"
    notes = [event["body"] for event in hub.runs[later]["events"] if event["kind"] == "system"]
    for name in TEN:
        said = [note["text"] for note in notes if note.get("repo") == name and note["text"].startswith("Fetch")]
        assert said == [f"Fetching origin in {ten.checkouts[name]}."], said
    record = ten.home.load_run(later)
    assert [item["repo"] for item in record["repos"]] == list(TEN), "the record keeps the run's order of its repos"
    one, two = ten.starts()
    assert two["run"] == later and two["resume"] == one["session"]


def test_the_next_chat_run_of_a_session_whose_fetch_git_refuses_fails_with_cause_credentials(machine, hermetic):  # noqa: F811
    machine.scenarios({"chat": [{"say": "First answer."}], "chat/resume": [{"say": "Never said."}]})
    with GitHttp(machine.tmp, "oauth2", "never-given") as server:
        machine.env.update(server.env)
        url = server.repo_url("alpha.git")
        found = {}

        async def body(hub, daemon):
            first = hub.queue_chat_run(["alpha"])
            assert await hub.wait_state(first, "done", "failed", timeout=WAIT) == "done", hub.texts(first)
            await wait_for(lambda: not daemon.runs, "the first run to let go of its slot")
            git("remote", "set-url", "origin", url, cwd=machine.checkouts["alpha"])
            session = machine.home.load_run(first)["session_id"]
            later = hub.queue_chat_run(["alpha"], resume_of=first, session_id=session, branch=f"chat/{first}")
            hub.leases[later] = {"leases": [], "missing": [{"repo": "alpha", "origin": url, "reason": "no secret"}]}
            hub.thread_reply(later, "owner wrote in the thread (message #6):\none more thing")
            assert await hub.wait_state(later, "done", "failed", timeout=WAIT) == "failed", hub.texts(later)
            found.update(hub=hub, first=first, later=later)

        with_daemon(machine, body)
        served = list(server.requests)
    hub, first, later = found["hub"], found["first"], found["later"]
    run = hub.runs[later]
    assert hub.moves(later) == ["leased", "failed"], hub.texts(later)
    assert run["failure_cause"] == "credentials"
    assert run["error"].startswith(f"git fetch in {machine.checkouts['alpha']} failed: "), run["error"]
    assert "could not read Username" in run["error"]
    texts = hub.texts(later)
    assert "Preflight passed: 1 repo(s) (1 left to their fetch: the run goes on in their worktrees)." in texts
    assert not any(text.startswith("Preflight: ") for text in texts), "the preflight asked no origin"
    assert served and all(path.endswith("/info/refs") for _, path, _, _ in served), "the fetch asked, refused"
    assert [item for item in machine.starts() if item["run"] == later] == [], "the agent never started"
    assert (machine.directory(first) / "alpha").is_dir(), "the worktree stays for the next run of the session"
