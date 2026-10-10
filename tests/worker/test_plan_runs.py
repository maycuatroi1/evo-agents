"""Plan runs on the worker, without Postgres: the daemon in this process with the fake runtime of
``tests.worker.fake_adapter``, the in-memory hub of ``tests.worker.fake_hub``, and two checkouts (alpha and beta)
whose origins are bare repositories on disk. The fake agent runs the commands of a plan run's agent (``evo-agents
worker step|ask|notify|plan``) as subprocesses in its directory and environment, as a real agent does.

The checks step 5 of the plan-runs-and-decisions plan names: two repos get two worktrees, each on its branch; `worker
step done` with a verify command that fails pushes nothing; a default branch is pushed only when the plan names it,
with a notice (default_branch); a run of one step still refuses the default branch; a decision asked, then answered
through the inbox, takes the run from waiting back to running; a parked run keeps its worktrees and the run that
resumes it goes on in them and in its session; the four commands refuse to run without EVO_RUN_ID.

Criterion a6 of the run-reliability plan: on a default branch the plan names that another member pushed meanwhile,
`worker step` and the push at the run's end rebase the run's own commits on top of the remote's tip (the step's verify
runs again there) and push a fast-forward; a conflict, or a verify that fails after the rebase, puts the commits on
evo-run/<run>, sends the owner the notice run_failed naming it, and fails the run with push_conflict, main untouched.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.cli import main as cli_main
from evo_agents.hub.credentials import Lease
from evo_agents.worker import adapter as adapter_module
from evo_agents.worker import checkouts as checkouts_module
from evo_agents.worker import gitops
from evo_agents.worker.daemon import Daemon
from evo_agents.worker.home import WorkerConfig, WorkerHome
from evo_agents.worker.run import ANSWER_PROMPT
from tests.worker import fake_adapter
from tests.worker.fake_hub import TOKEN, FakeHub

ROOT = Path(__file__).parents[2]
PROJECT = "demo"
PLAN = "two-repos"
REPOS = ("alpha", "beta")
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Plan Owner",
    "GIT_AUTHOR_EMAIL": "owner@example.org",
    "GIT_COMMITTER_NAME": "Plan Owner",
    "GIT_COMMITTER_EMAIL": "owner@example.org",
}
WAIT = 60.0


def git(*args: str, cwd: Path | None = None) -> str:
    command = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    done = subprocess.run(command, capture_output=True, text=True, env={**os.environ, **GIT_IDENTITY}, timeout=60)
    assert done.returncode == 0, f"{command}: {done.stderr}"
    return done.stdout.strip()


def plan_body(alpha: str = "feat/alpha", beta: str = "feat/beta") -> dict:
    return {
        "id": PLAN,
        "title": "Two repos",
        "goal": "Touch both repos.",
        "repos": [{"repo": "alpha", "branch": alpha}, {"repo": "beta", "branch": beta}],
        "steps": [
            {"id": 1, "title": "Alpha file", "repo": "alpha", "what": "write a.txt", "status": "pending"},
            {
                "id": 2,
                "title": "Beta file",
                "repo": "beta",
                "what": "write b.txt",
                "depends_on": [1],
                "status": "pending",
            },
        ],
    }


def run_repos(body: dict) -> list[dict]:
    return [{"repo": entry["repo"], "branch": entry["branch"]} for entry in body["repos"]]


class Machine:
    """A worker's machine: a checkout of alpha and of beta, each cloned from a bare origin with one commit on main,
    the worker's state directory, and the files of the fake runtime."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origins = {name: tmp / f"{name}.git" for name in REPOS}
        self.checkouts = {name: tmp / "ws" / name for name in REPOS}
        self.seeds: dict[str, str] = {}
        for name in REPOS:
            seed = tmp / "seed" / name
            seed.mkdir(parents=True)
            git("init", "--quiet", "--initial-branch", "main", cwd=seed)
            (seed / "README.md").write_text(f"# {name}\n", encoding="utf-8")
            git("add", "README.md", cwd=seed)
            git("commit", "--quiet", "-m", "first commit", cwd=seed)
            git("clone", "--quiet", "--bare", str(seed), str(self.origins[name]))
            git("clone", "--quiet", str(self.origins[name]), str(self.checkouts[name]))
            self.seeds[name] = git("rev-parse", "HEAD", cwd=seed)
        self.home = WorkerHome(tmp / "worker")
        self.scenarios_path = tmp / "scenarios.json"
        self.cli_path = tmp / "cli.jsonl"
        self.starts_path = tmp / "starts.jsonl"
        self.messages_path = tmp / "messages.txt"
        self.scenarios({})
        pythonpath = os.pathsep.join(item for item in (str(ROOT), os.environ.get("PYTHONPATH")) if item)
        self.env = {
            **{key: value for key, value in os.environ.items() if not key.startswith("EVO_")},
            "PYTHONPATH": pythonpath,
            "EVO_WORKER_HEARTBEAT_SECONDS": "0.2",
            **GIT_IDENTITY,
            **fake_adapter.environment(self.scenarios_path, self.messages_path, self.starts_path, self.cli_path),
        }

    def scenarios(self, scenarios: dict) -> None:
        self.scenarios_path.write_text(json.dumps(scenarios), encoding="utf-8")

    def commands(self) -> list[dict]:
        if not self.cli_path.exists():
            return []
        return [json.loads(line) for line in self.cli_path.read_text(encoding="utf-8").splitlines()]

    def starts(self) -> list[dict]:
        return [json.loads(line) for line in self.starts_path.read_text(encoding="utf-8").splitlines()]

    def origin_rev(self, name: str, ref: str) -> str | None:
        done = subprocess.run(
            ["git", "--git-dir", str(self.origins[name]), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
        )
        return done.stdout.strip() or None

    def origin(self, name: str, *args: str) -> str:
        return git("--git-dir", str(self.origins[name]), *args)

    def directory(self, run_id: int) -> Path:
        return self.home.worktree_path(PROJECT, run_id)


@pytest.fixture
def machine(tmp_path, monkeypatch) -> Machine:
    monkeypatch.setattr(checkouts_module, "REGISTRY_PATHS", ())  # the checkouts of config.json alone
    unavailable = adapter_module.Detection(False, None, "not looked for in this test")
    monkeypatch.setattr(adapter_module, "probe_binary", lambda *args, **kwargs: unavailable)
    for key, value in GIT_IDENTITY.items():  # the daemon commits in this process
        monkeypatch.setenv(key, value)
    fake_adapter.FakeAdapter.starts.clear()
    return Machine(tmp_path)


def with_daemon(machine: Machine, body, *, plan: dict | None = None, **options):
    """Run ``await body(hub, daemon)`` with the fake hub holding ``plan`` and the daemon running in this process, then
    stop the daemon as SIGTERM does. ``options`` go to the Daemon (``keep_awake``, ``wake``)."""

    async def go():
        hub = FakeHub(PROJECT)
        hub.put_plan(plan or plan_body())
        url = await hub.start()
        config = WorkerConfig(
            url=url,
            worker_id=1,
            name="mac",
            projects=[PROJECT],
            checkouts={f"{PROJECT}/{name}": str(path) for name, path in machine.checkouts.items()},
        )
        machine.home.save(config, TOKEN)
        daemon = Daemon(
            machine.home, config, TOKEN, adapters={"claude-code": fake_adapter.FakeAdapter}, env=machine.env, **options
        )
        task = asyncio.create_task(daemon.run())
        try:
            for _ in range(600):
                if getattr(daemon, "beat_ok", None) is not None and daemon.beat_ok.is_set():
                    break
                assert not task.done(), task.exception() if task.done() else None
                await asyncio.sleep(0.05)
            await body(hub, daemon)
        finally:
            if not task.done():
                daemon._signal(signal.SIGTERM)
                done, _ = await asyncio.wait({task}, timeout=10)
                if not done:
                    daemon._signal(signal.SIGTERM)  # the second stops the agents now
                    done, _ = await asyncio.wait({task}, timeout=30)
                if not done:
                    task.cancel()
            await hub.stop()
        assert task.result() == 0

    asyncio.run(go())


async def wait_for(predicate, what: str, timeout: float = WAIT):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


def ask(step: str = "1") -> dict:
    return {
        "cli": [
            "ask",
            "--category",
            "deploy",
            "--question",
            "Deploy alpha to staging now?",
            "--option",
            "yes=Deploy:to staging only, not production",
            "--option",
            "no=Wait",
            "--recommended",
            "no",
            "--step",
            step,
        ]
    }


# The checks of the step


def test_two_repos_get_two_worktrees_and_a_failed_verify_pushes_nothing(machine):
    git("checkout", "--quiet", "-b", "feat/beta", cwd=machine.checkouts["beta"])  # the owner works on it here
    owner_head = git("rev-parse", "HEAD", cwd=machine.checkouts["beta"])
    alpha_origin = machine.origins["alpha"]
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {
                    "sh": "pwd -P; echo $EVO_RUN_ID $EVO_RUN_KIND; echo $EVO_WORKER_HOME; "
                    "git -C alpha symbolic-ref --short HEAD; git -C beta symbolic-ref --short HEAD; "
                    "head -c 1000 .evo-run/plan.yaml"
                },
                {"cli": ["plan"]},
                {"cli": ["step", "1", "in_progress", "--repo", "alpha"]},
                {"write": {"alpha/a.txt": "a\n"}},
                {
                    "cli": [
                        "step",
                        "1",
                        "done",
                        "--repo",
                        "alpha",
                        "--evidence",
                        "Wrote a.txt.",
                        "--verify",
                        "test -f a.txt",
                        "--verify",
                        "exit 3",
                    ]
                },
                {"sh": f"git ls-remote {alpha_origin} refs/heads/feat/alpha; git -C alpha status --porcelain"},
                {"cli": ["step", "1", "done", "--evidence", "Wrote a.txt.", "--verify", "test -f a.txt"]},
                {"write": {"beta/b.txt": "b\n"}},
                {"result": {"summary": "Step 1 done; b.txt is left for the worker to push."}},
            ]
        }
    )
    found = {}

    async def body(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        found["run"] = run_id
        assert await hub.wait_state(run_id, "done", "failed") == "done", (hub.runs[run_id], hub.texts(run_id))
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found["hub"] = hub

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    directory = machine.directory(run_id)
    commands = machine.commands()

    # The agent ran in the run's directory, with the run's variables, and each repo in a worktree on its branch.
    looked = commands[0]["stdout"].splitlines()
    assert Path(looked[0]) == directory.resolve()
    assert looked[1] == f"{run_id} plan"
    assert looked[2] == str(machine.home.root)
    assert looked[3:5] == ["feat/alpha", f"evo-run/{run_id}/beta"], "beta's branch is checked out by the owner"
    assert "id: two-repos" in commands[0]["stdout"], ".evo-run/plan.yaml holds the plan as claimed"
    assert commands[1]["exit"] == 0 and "title: Alpha file" in commands[1]["stdout"], commands[1]
    assert commands[2]["exit"] == 0, commands[2]

    # A done step whose verify command fails is refused, and nothing is committed, pushed or reported.
    refused = commands[3]
    assert refused["exit"] == 1, refused
    assert "`exit 3` exited 3" in refused["stdout"] and "Nothing was committed or pushed" in refused["stderr"]
    assert commands[4]["stdout"].strip() == "?? a.txt", "nothing on origin, a.txt not committed"
    assert commands[5]["exit"] == 0, commands[5]
    reports = [(item["key"], item["status"]) for item in hub.step_reports]
    assert reports == [("1", "in_progress"), ("1", "done")]
    done = hub.step_reports[1]
    assert done["repo"] == "alpha", "the plan's repo of the step when --repo is not given"
    assert done["commit_sha"] == machine.origin_rev("alpha", "refs/heads/feat/alpha")
    assert [(item["command"], item["exit_code"]) for item in done["verify"]] == [("test -f a.txt", 0)]
    assert machine.origin("alpha", "log", "--format=%s", "-1", "feat/alpha") == f"run #{run_id} step 1: Alpha file"

    # At the end the daemon committed and pushed what beta had left, and reported done without verify commands.
    assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
    assert machine.origin("beta", "log", "--format=%s", "-1", "feat/beta") == f"run #{run_id}: Two repos"
    assert "b.txt" in machine.origin("beta", "ls-tree", "-r", "--name-only", "feat/beta").split()
    assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"]
    assert machine.origin_rev("beta", "refs/heads/main") == machine.seeds["beta"]
    run = hub.runs[run_id]
    assert run["summary"] == "Step 1 done; b.txt is left for the worker to push."
    assert run["diffstat"] == {"files": 2, "insertions": 2, "deletions": 0}
    assert not hub.notices, "no default branch was pushed"
    assert git("rev-parse", "HEAD", cwd=machine.checkouts["beta"]) == owner_head, "the owner's branch did not move"
    assert git("symbolic-ref", "--short", "HEAD", cwd=machine.checkouts["beta"]) == "feat/beta"
    detached = subprocess.run(
        ["git", "-C", str(directory / "alpha"), "symbolic-ref", "-q", "HEAD"], capture_output=True
    )
    assert detached.returncode != 0, "the worktree on the plan's branch lets go of it once the run is over"
    record = machine.home.load_run(run_id)
    assert record["kind"] == "plan" and record["dir"] == str(directory) and record["finished_at"]
    assert [item["repo"] for item in record["repos"]] == ["alpha", "beta"]


def test_a_default_branch_the_plan_names_is_pushed_with_a_notice(machine):
    body = plan_body(alpha="main")
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"write": {"alpha/a.txt": "a\n"}},
                {"cli": ["step", "1", "done", "--verify", "test -f a.txt"]},
                {"write": {"alpha/later.txt": "later\n"}},
                {"result": {"summary": "Pushed main twice."}},
            ]
        }
    )
    found = {}

    async def run_it(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(body))
        assert await hub.wait_state(run_id, "done", "failed") == "done", (hub.runs[run_id], hub.texts(run_id))
        found.update(run=run_id, hub=hub)

    with_daemon(machine, run_it, plan=body)
    run_id, hub = found["run"], found["hub"]
    step = machine.commands()[0]
    assert step["exit"] == 0, step
    assert "Notified the run's owner of the push to main" in step["stdout"]
    main = machine.origin_rev("alpha", "refs/heads/main")
    assert machine.origin("alpha", "log", "--format=%s", "-3", "main").splitlines() == [
        f"run #{run_id}: Two repos",
        f"run #{run_id} step 1: Alpha file",
        "first commit",
    ]
    assert [(notice["kind"], notice["repo"], notice["branch"]) for notice in hub.notices] == [
        ("push_default_branch", "alpha", "main"),
        ("push_default_branch", "alpha", "main"),
    ], "one from `worker step`, one from the push at the end"
    first, last = hub.notices
    assert first["commits"] == [hub.step_reports[0]["commit_sha"]]
    assert last["commits"] == [main] and main in last["body"]
    texts = hub.texts(run_id)
    assert any("a default branch the plan names for it" in text for text in texts)
    assert f"evo-run/{run_id}/alpha" in " ".join(texts), "main is checked out in the owner's checkout"


def test_default_branch_no_longer_named_by_the_plan_is_not_pushed(machine):
    gate, go = machine.tmp / "gate", machine.tmp / "go"
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"touch": str(gate)},
                {"wait_for": str(go)},
                {"write": {"alpha/a.txt": "a\n"}},
                {"cli": ["step", "1", "done", "--verify", "test -f a.txt"]},
                {"result": {"summary": "The plan moved alpha off main."}},
            ]
        }
    )
    found = {}

    async def run_it(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body(alpha="main")))
        await wait_for(gate.exists, "the agent to start")
        hub.put_plan(plan_body(alpha="feat/alpha"))  # the plan on the hub no longer names main for alpha
        go.touch()
        assert await hub.wait_state(run_id, "done", "failed") == "failed"
        found.update(run=run_id, hub=hub)

    with_daemon(machine, run_it, plan=plan_body(alpha="main"))
    run_id, hub = found["run"], found["hub"]
    step = machine.commands()[0]
    assert step["exit"] == 1 and "the plan does not name it" in step["stderr"], step
    assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"], "main never moved"
    assert not hub.notices and not hub.step_reports
    assert "alpha: main is a default branch of the repo and the plan does not name it" in hub.runs[run_id]["error"]


def test_default_branch_is_refused_by_a_run_of_one_step(machine, tmp_path):
    started = tmp_path / "started"
    machine.scenarios(
        {"1": [{"touch": str(started)}, {"write": {"x.txt": "x"}}, {"result": {"verify_commands": ["true"]}}]}
    )
    found = {}

    async def run_it(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "main")
        assert await hub.wait_state(run_id, "done", "failed") == "failed"
        found.update(run=run_id, hub=hub)

    with_daemon(machine, run_it, plan=plan_body(alpha="main"))
    error = found["hub"].runs[found["run"]]["error"]
    assert "never pushes" in error and "main" in error
    assert not started.exists(), "the agent never starts on a run of one step that would push the default branch"
    assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"]


async def _refused_push(path: Path, **rules) -> str:
    with pytest.raises(gitops.PushRefused) as refused:
        await gitops.push(path, "main", protected=("main", "master"), **rules)
    return str(refused.value)


def test_default_branch_rules_of_gitops_push(machine):
    """gitops.push itself: a run of one step never pushes a default branch, a plan run only the one its plan names,
    and nothing reaches the remote when it refuses."""
    path = machine.checkouts["alpha"]
    (path / "c.txt").write_text("c\n", encoding="utf-8")
    git("add", "c.txt", cwd=path)
    git("commit", "--quiet", "-m", "c", cwd=path)

    async def go():
        assert "run of one step" in await _refused_push(path, kind="step")
        assert "plan does not name it" in await _refused_push(path, kind="plan", plan_branch="feat/alpha")
        assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"]
        pushed = await gitops.push(path, "main", protected=("main",), kind="plan", plan_branch="main")
        assert pushed.default and pushed.changed and list(pushed.commits) == [git("rev-parse", "HEAD", cwd=path)]
        again = await gitops.push(path, "main", protected=("main",), kind="plan", plan_branch="main")
        assert not again.changed and again.commits == (), "a branch at HEAD already is left alone"
        notice = gitops.push_notice(7, "alpha", pushed)
        assert notice["kind"] == "push_default_branch" and notice["commits"] == list(pushed.commits)
        assert notice["title"] == "Run #7 pushed 1 commit(s) to main of alpha"
        feature = await gitops.push(path, "feat/x", protected=("main",), kind="step")
        assert not feature.default and feature.changed

    asyncio.run(go())
    assert machine.origin_rev("alpha", "refs/heads/main") == git("rev-parse", "HEAD", cwd=machine.checkouts["alpha"])


def move_origin(machine: Machine, name: str, file: str) -> str:
    """A commit another member pushes to main of ``name``'s origin, which no checkout of the machine has fetched."""
    other = machine.tmp / "other" / name
    if not other.exists():
        git("clone", "--quiet", str(machine.origins[name]), str(other))
    git("pull", "--quiet", "--ff-only", cwd=other)
    (other / file).write_text(f"{file}\n", encoding="utf-8")
    git("add", file, cwd=other)
    git("commit", "--quiet", "-m", f"another member adds {file}", cwd=other)
    git("push", "--quiet", "origin", "main", cwd=other)
    return git("rev-parse", "HEAD", cwd=other)


def test_gitops_push_leaves_a_branch_the_remote_moved_past_alone(machine):
    """A worktree that is only behind its branch on the remote has nothing to push, though this repository has not
    fetched the remote's tip; a worktree with a commit of its own that the remote lacks is still refused."""
    path = machine.checkouts["alpha"]
    tip = move_origin(machine, "alpha", "theirs.txt")
    lacks = subprocess.run(["git", "-C", str(path), "cat-file", "-e", f"{tip}^{{commit}}"], capture_output=True)
    assert lacks.returncode != 0, "the checkout has not fetched the remote's tip"

    async def go():
        behind = await gitops.push(path, "main", protected=("main",), kind="plan", plan_branch="main")
        assert not behind.changed and behind.commits == ()
        assert behind.head == machine.seeds["alpha"], "the worktree's HEAD, which main on the remote has"
        assert machine.origin_rev("alpha", "refs/heads/main") == tip, "main on the remote did not move"
        (path / "mine.txt").write_text("mine\n", encoding="utf-8")
        git("add", "mine.txt", cwd=path)
        git("commit", "--quiet", "-m", "mine", cwd=path)
        with pytest.raises(gitops.GitError) as diverged:
            await gitops.push(path, "main", protected=("main",), kind="plan", plan_branch="main")
        assert "git push failed" in str(diverged.value)

    asyncio.run(go())
    assert machine.origin_rev("alpha", "refs/heads/main") == tip, "a push that is no fast-forward is never forced"


def test_a_plan_run_ends_done_when_a_default_branch_it_did_not_touch_moved_on_the_remote(machine):
    """Run #16 of evo-agents: the plan names main of a repo the agent never committed to, another member pushes main
    meanwhile, and the push at the end of the run finds nothing to send instead of failing the run."""
    gate, go = machine.tmp / "gate", machine.tmp / "go"
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"touch": str(gate)},
                {"wait_for": str(go)},
                {"write": {"beta/b.txt": "b\n"}},
                {"result": {"summary": "Wrote b.txt; alpha untouched."}},
            ]
        }
    )
    found = {}

    async def run_it(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body(alpha="main")))
        await wait_for(gate.exists, "the agent to start")
        found["tip"] = move_origin(machine, "alpha", "theirs.txt")
        go.touch()
        assert await hub.wait_state(run_id, "done", "failed") == "done", (hub.runs[run_id], hub.texts(run_id))
        found.update(run=run_id, hub=hub)

    with_daemon(machine, run_it, plan=plan_body(alpha="main"))
    run_id, hub = found["run"], found["hub"]
    assert machine.origin_rev("alpha", "refs/heads/main") == found["tip"], "main stays where the other member left it"
    assert "b.txt" in machine.origin("beta", "ls-tree", "-r", "--name-only", "feat/beta").split()
    assert not hub.notices, "nothing was pushed to a default branch"
    seed = machine.seeds["alpha"][:12]
    assert f"main of alpha on origin has {seed} already: nothing to push." in hub.texts(run_id)


# A default branch the plan names that the remote moved on while the run committed to it (plan run-reliability, a6)


def commit_file(path: Path, file: str, text: str | None = None) -> str:
    """A commit of ``file`` in the checkout or worktree at ``path``; its sha."""
    (path / file).write_text(text if text is not None else f"{file}\n", encoding="utf-8")
    git("add", file, cwd=path)
    git("commit", "--quiet", "-m", f"add {file}", cwd=path)
    return git("rev-parse", "HEAD", cwd=path)


def replay_rules(machine: Machine, verify=None, pushed=()) -> gitops.Replay:
    return gitops.Replay(
        scratch=machine.tmp / "scratch" / "replay", side_branch="evo-run/7", pushed=tuple(pushed), verify=verify
    )


async def _push_main(path: Path, replay: gitops.Replay) -> gitops.Pushed:
    return await gitops.push(path, "main", protected=("main",), kind="plan", plan_branch="main", replay=replay)


def test_gitops_push_rebases_the_runs_commits_on_a_default_branch_the_remote_moved_on(machine):
    """gitops.push with a Replay: HEAD's own commit goes on top of the tip another member pushed, the verify runs
    again in the worktree at the replayed commit, and main moves as a fast-forward; the replay's worktree is gone."""
    path = machine.checkouts["alpha"]
    mine = commit_file(path, "mine.txt")
    tip = move_origin(machine, "alpha", "theirs.txt")
    seen = []

    async def verify(head: str) -> str | None:
        seen.append((head, git("rev-parse", "HEAD", cwd=path), sorted(item.name for item in path.glob("*.txt"))))
        return None

    async def go():
        return await _push_main(path, replay_rules(machine, verify))

    pushed = asyncio.run(go())
    assert pushed.changed and pushed.default and pushed.onto == tip and pushed.replayed_from == mine
    assert pushed.head != mine and pushed.commits == (pushed.head,)
    assert seen == [(pushed.head, pushed.head, ["mine.txt", "theirs.txt"])], "the verify ran at the replayed commit"
    assert machine.origin_rev("alpha", "refs/heads/main") == pushed.head
    assert machine.origin("alpha", "rev-parse", f"{pushed.head}^") == tip, "on top of the remote's tip, not forced"
    assert git("rev-parse", "HEAD", cwd=path) == pushed.head, "the worktree is at the replayed commit"
    assert git("log", "-1", "--format=%s", cwd=path) == "add mine.txt"
    assert machine.origin_rev("alpha", "refs/heads/evo-run/7") is None, "no side branch"
    assert not (machine.tmp / "scratch" / "replay").exists()
    assert "replay" not in git("worktree", "list", cwd=path)
    notice = gitops.push_notice(7, "alpha", pushed)
    assert f"moved main on to {tip}" in notice["body"] and notice["commits"] == [pushed.head]


def test_gitops_push_sends_commits_that_do_not_rebase_or_verify_to_the_side_branch(machine):
    """A conflict, a verify that fails at the replayed commit, and a commit the run pushed that the remote dropped
    each leave main as the remote has it and put HEAD, as it was, on the side branch; any other branch the remote
    moved on is refused as before (diverged)."""
    alpha, beta = machine.checkouts["alpha"], machine.checkouts["beta"]
    mine = commit_file(alpha, "README.md", "mine\n")
    tip = move_origin(machine, "alpha", "README.md")
    ran = []

    async def never(head: str) -> str | None:
        ran.append(head)
        return None

    async def failing(head: str) -> str | None:
        return "`test ! -f theirs.txt` exited 1"

    async def go():
        with pytest.raises(gitops.PushConflict) as conflict:
            await _push_main(alpha, replay_rules(machine, never))
        return conflict.value

    conflict = asyncio.run(go())
    assert "did not go on top of it (conflict in README.md)" in str(conflict)
    assert conflict.side_branch == "evo-run/7" and conflict.head == mine and conflict.onto == tip
    assert conflict.commits == (mine,) and not ran, "no verify runs when nothing replays"
    assert machine.origin_rev("alpha", "refs/heads/main") == tip, "main was not touched"
    assert machine.origin_rev("alpha", "refs/heads/evo-run/7") == mine
    assert git("rev-parse", "HEAD", cwd=alpha) == mine and git("status", "--porcelain", cwd=alpha) == ""
    notice = gitops.conflict_notice(7, "alpha", conflict)
    assert (notice["kind"], notice["repo"], notice["branch"], notice["commits"]) == (
        "run_failed",
        "alpha",
        "evo-run/7",
        [mine],
    )
    assert notice["title"] == "Run #7 could not push main of alpha: its commits are on evo-run/7"

    mine = commit_file(beta, "mine.txt")
    tip = move_origin(machine, "beta", "theirs.txt")

    async def verify_fails():
        with pytest.raises(gitops.PushConflict) as conflict:
            await _push_main(beta, replay_rules(machine, failing))
        return conflict.value

    conflict = asyncio.run(verify_fails())
    assert "went on top of it, but the verify run again there failed: `test ! -f theirs.txt` exited 1" in str(conflict)
    assert git("rev-parse", "HEAD", cwd=beta) == mine, "the worktree is back at HEAD as it was"
    assert machine.origin_rev("beta", "refs/heads/main") == tip
    assert machine.origin_rev("beta", "refs/heads/evo-run/7") == mine

    # main of beta has mine.txt, then another member takes it out with a forced push of their own.
    git("reset", "--quiet", "--hard", tip, cwd=beta)
    pushed_before = commit_file(beta, "pushed.txt")
    git("push", "--quiet", "origin", "HEAD:main", cwd=beta)
    other = machine.tmp / "other" / "beta"
    git("fetch", "--quiet", "origin", cwd=other)
    git("reset", "--quiet", "--hard", tip, cwd=other)
    dropped_tip = commit_file(other, "rewritten.txt")
    git("push", "--quiet", "--force", "origin", "main", cwd=other)
    later = commit_file(beta, "later.txt")

    async def dropped():
        with pytest.raises(gitops.PushConflict) as conflict:
            await _push_main(beta, gitops.Replay(machine.tmp / "scratch" / "b", "evo-run/8", pushed=[pushed_before]))
        return conflict.value

    conflict = asyncio.run(dropped())
    assert f"include {pushed_before[:12]}, which the run pushed to main before and the remote no longer has" in str(
        conflict
    )
    assert machine.origin_rev("beta", "refs/heads/main") == dropped_tip, "what the other member pushed stays"
    assert machine.origin_rev("beta", "refs/heads/evo-run/8") == later

    # Any other branch the remote moved on is pushed as before, and refused: a plan's own branch is not replayed.
    git("push", "--quiet", "origin", f"{later}:refs/heads/feat/x", cwd=beta)
    git("fetch", "--quiet", "origin", cwd=other)
    git("checkout", "--quiet", "-b", "feat/x", "origin/feat/x", cwd=other)
    commit_file(other, "theirs-x.txt")
    git("push", "--quiet", "origin", "feat/x", cwd=other)
    commit_file(beta, "mine-x.txt")

    async def diverged():
        with pytest.raises(gitops.GitError) as refused:
            await gitops.push(beta, "feat/x", protected=("main",), kind="plan", replay=replay_rules(machine))
        return refused.value

    refused = asyncio.run(diverged())
    assert not isinstance(refused, gitops.PushConflict) and gitops.rejected(refused), str(refused)


def default_branch_run(machine: Machine, scenario: list, move, *, plan: dict | None = None) -> dict:
    """A plan run whose plan names main for alpha, whose agent follows ``scenario``; ``move()`` runs once the agent
    touched the gate and before it goes on, as another member pushing main of alpha meanwhile. The run's id, the hub
    and what ``move`` returned."""
    body = plan or plan_body(alpha="main")
    gate, go = machine.tmp / "gate", machine.tmp / "go"
    machine.scenarios({f"plan:{PLAN}": [{"touch": str(gate)}, {"wait_for": str(go)}, *scenario]})
    found = {}

    async def run_it(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(body))
        await wait_for(gate.exists, "the agent to start")
        found["moved"] = move()
        go.touch()
        await hub.wait_state(run_id, "done", "failed")
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(run=run_id, hub=hub)

    with_daemon(machine, run_it, plan=body)
    return found


def test_worker_step_rebases_its_commit_when_the_remote_moved_main_on_and_runs_its_verify_again(machine):
    """`worker step` of a step on main, which another member pushed meanwhile: the step's commit goes on top of their
    commit, its verify runs again there, main moves as a fast-forward, and the step's report names that commit."""
    found = default_branch_run(
        machine,
        [
            {"write": {"alpha/a.txt": "a\n"}},
            {"cli": ["step", "1", "done", "--evidence", "Wrote a.txt.", "--verify", "test -f a.txt"]},
            {"result": {"summary": "Step 1 done on main."}},
        ],
        lambda: move_origin(machine, "alpha", "theirs.txt"),
    )
    run_id, hub, tip = found["run"], found["hub"], found["moved"]
    assert hub.runs[run_id]["state"] == "done", (hub.runs[run_id], hub.texts(run_id))
    step = machine.commands()[0]
    assert step["exit"] == 0, step
    assert "moved on at origin: the run's commits were put on top of it" in step["stdout"]
    assert step["stdout"].count("verify: `test -f a.txt` exited 0") == 2, "once before the push, once after the replay"
    main = machine.origin_rev("alpha", "refs/heads/main")
    assert machine.origin("alpha", "log", "--format=%s", "main").splitlines() == [
        f"run #{run_id} step 1: Alpha file",
        "another member adds theirs.txt",
        "first commit",
    ]
    assert machine.origin("alpha", "rev-parse", "main^") == tip, "their commit stays as they pushed it"
    report = hub.step_reports[-1]
    assert (report["key"], report["status"], report["commit_sha"]) == ("1", "done", main), "the commit main has"
    assert [item["exit_code"] for item in report["verify"]] == [0]
    assert [(notice["kind"], notice["branch"], notice["commits"]) for notice in hub.notices] == [
        ("push_default_branch", "main", [main])
    ]
    assert f"moved main on to {tip}" in hub.notices[0]["body"]
    assert machine.home.pushed(run_id, "alpha", "main") == [main]
    assert machine.origin_rev("alpha", f"refs/heads/evo-run/{run_id}") is None
    assert hub.runs[run_id].get("failure_cause") is None


def test_worker_step_pushes_the_side_branch_and_the_run_fails_when_its_commit_does_not_rebase(machine):
    """`worker step` whose commit conflicts with what another member pushed to main: the commit goes to
    evo-run/<run>, the owner gets the notice run_failed naming it, nothing is reported, a second `worker step done`
    is refused, and the run fails with push_conflict once the turn ends."""
    found = default_branch_run(
        machine,
        [
            {"write": {"alpha/README.md": "mine\n"}},
            {"cli": ["step", "1", "done", "--verify", "test -f README.md"]},
            {"cli": ["step", "1", "done", "--verify", "true"]},
            {"result": {"summary": "Tried step 1."}},
        ],
        lambda: move_origin(machine, "alpha", "README.md"),
    )
    run_id, hub, tip = found["run"], found["hub"], found["moved"]
    run = hub.runs[run_id]
    assert run["state"] == "failed" and run["failure_cause"] == "push_conflict", (run, hub.texts(run_id))
    side = f"evo-run/{run_id}"
    assert "did not go on top of it (conflict in README.md)" in run["error"] and side in run["error"]
    first, second = machine.commands()
    assert first["exit"] == 1 and "ends failed (push_conflict) once this turn ends" in first["stderr"], first
    assert second["exit"] == 1 and "Nothing more is pushed or reported done" in second["stderr"], second
    assert machine.origin_rev("alpha", "refs/heads/main") == tip, "main stays as the other member left it"
    held = machine.origin_rev("alpha", f"refs/heads/{side}")
    assert machine.origin("alpha", "log", "-1", "--format=%s", side) == f"run #{run_id} step 1: Alpha file"
    assert machine.origin("alpha", "rev-parse", f"{side}^") == machine.seeds["alpha"]
    assert not hub.step_reports, "the step is not reported done"
    assert [(notice["kind"], notice["repo"], notice["branch"], notice["commits"]) for notice in hub.notices] == [
        ("run_failed", "alpha", side, [held])
    ]
    assert hub.moves(run_id) == ["leased", "running", "failed"], "the run fails at the end of the turn, pushing nothing"


def test_worker_step_does_not_push_main_when_the_verify_fails_after_the_rebase(machine):
    found = default_branch_run(
        machine,
        [
            {"write": {"alpha/a.txt": "a\n"}},
            {"cli": ["step", "1", "done", "--verify", "test ! -f theirs.txt"]},
            {"result": {"summary": "Tried step 1."}},
        ],
        lambda: move_origin(machine, "alpha", "theirs.txt"),
    )
    run_id, hub, tip = found["run"], found["hub"], found["moved"]
    run = hub.runs[run_id]
    assert run["failure_cause"] == "push_conflict", (run, hub.texts(run_id))
    assert "the verify run again there failed: `test ! -f theirs.txt` exited 1" in run["error"]
    step = machine.commands()[0]
    assert step["stdout"].count("verify: `test ! -f theirs.txt` exited") == 2, step
    assert machine.origin_rev("alpha", "refs/heads/main") == tip
    side = machine.origin_rev("alpha", f"refs/heads/evo-run/{run_id}")
    assert side is not None and machine.origin("alpha", "rev-parse", f"{side}^") == machine.seeds["alpha"]
    worktree = machine.directory(run_id) / "alpha"
    assert git("rev-parse", "HEAD", cwd=worktree) == side, "the worktree went back to the commit the side branch has"
    assert not hub.step_reports


def test_the_push_at_the_end_rebases_what_the_run_left_on_a_default_branch_the_remote_moved_on(machine):
    found = default_branch_run(
        machine,
        [{"write": {"alpha/a.txt": "a\n"}}, {"result": {"summary": "Wrote a.txt, left for the worker to push."}}],
        lambda: move_origin(machine, "alpha", "theirs.txt"),
    )
    run_id, hub, tip = found["run"], found["hub"], found["moved"]
    assert hub.runs[run_id]["state"] == "done", (hub.runs[run_id], hub.texts(run_id))
    assert machine.origin("alpha", "log", "--format=%s", "main").splitlines() == [
        f"run #{run_id}: Two repos",
        "another member adds theirs.txt",
        "first commit",
    ]
    main = machine.origin_rev("alpha", "refs/heads/main")
    texts = " ".join(hub.texts(run_id))
    assert f"main of alpha on origin had moved on to {tip[:12]}: the run's commits were put on top of it" in texts
    assert f"Pushed {main[:12]} of alpha to main on origin (1 commit(s))." in texts
    assert [(notice["kind"], notice["commits"]) for notice in hub.notices] == [("push_default_branch", [main])]


def test_the_push_at_the_end_sends_the_side_branch_when_the_rebase_conflicts(machine):
    found = default_branch_run(
        machine,
        [{"write": {"alpha/README.md": "mine\n"}}, {"result": {"summary": "Changed the README."}}],
        lambda: move_origin(machine, "alpha", "README.md"),
    )
    run_id, hub, tip = found["run"], found["hub"], found["moved"]
    run = hub.runs[run_id]
    side = f"evo-run/{run_id}"
    assert run["state"] == "failed" and run["failure_cause"] == "push_conflict", (run, hub.texts(run_id))
    assert run["error"].startswith("alpha: main of origin moved on to") and side in run["error"]
    assert hub.moves(run_id) == ["leased", "running", "verifying", "failed"]
    assert machine.origin_rev("alpha", "refs/heads/main") == tip
    assert machine.origin("alpha", "log", "-1", "--format=%s", side) == f"run #{run_id}: Two repos"
    assert [(notice["kind"], notice["branch"]) for notice in hub.notices] == [("run_failed", side)]
    assert any(f"{side} on origin has them" in text for text in hub.texts(run_id))


def test_a_decision_asked_then_answered_through_the_inbox_takes_the_run_from_waiting_to_running(machine):
    context = machine.tmp / "context.md"
    context.write_text("## Why\n\nStaging is shared with the other team.\n", encoding="utf-8")
    question = ask()
    question["cli"] += ["--context-file", str(context)]
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"cli": ["step", "1", "in_progress"]},
                {"write": {"alpha/a.txt": "a\n"}},
                question,
            ],
            f"plan:{PLAN}/2": [
                {
                    "cli": [
                        "step",
                        "1",
                        "done",
                        "--evidence",
                        "Decision: deployed, as the owner said.",
                        "--verify",
                        "true",
                    ]
                },
                {"write": {"beta/b.txt": "b\n"}},
                {"cli": ["step", "2", "done", "--verify", "test -f b.txt"]},
                {"result": {"summary": "Both steps done."}},
            ],
        }
    )
    found = {}

    async def run_it(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        assert await hub.wait_state(run_id, "waiting", "done", "failed") == "waiting", hub.texts(run_id)
        decision = hub.decisions[1]
        assert decision["run_id"] == run_id and decision["category"] == "deploy" and decision["step_key"] == "1"
        assert decision["options"] == [
            {"key": "yes", "label": "Deploy", "description": "to staging only, not production"},
            {"key": "no", "label": "Wait"},
        ]
        assert decision["recommended"] == "no" and "Staging is shared" in decision["context"]
        await asyncio.sleep(1.0)  # a few heartbeats: the run keeps waiting, its agent does not start again
        assert hub.runs[run_id]["state"] == "waiting" and len(machine.starts()) == 1
        hub.answer(1, "yes")
        assert await hub.wait_state(run_id, "done", "failed") == "done", (hub.runs[run_id], hub.texts(run_id))
        found.update(run=run_id, hub=hub)

    with_daemon(machine, run_it)
    run_id, hub = found["run"], found["hub"]
    assert hub.moves(run_id) == ["leased", "running", "waiting", "running", "verifying", "done"]
    asked = machine.commands()[1]
    assert asked["exit"] == 0, asked
    assert "Decision #1 is open" in asked["stdout"] and "end your turn" in asked["stdout"]
    assert json.loads(machine.home.decisions_path(run_id).read_text(encoding="utf-8"))["id"] == 1
    first, second = machine.starts()
    assert second["resume"] == first["session"], "the answer goes to the agent in the same session"
    assert second["prompt"].startswith(ANSWER_PROMPT)
    assert "Answer to decision #1 (deploy)" in second["prompt"] and "Chosen option: yes." in second["prompt"]
    assert hub.decisions[1]["delivered"], "the inbox message was acknowledged once the agent had it"
    assert any("waits for its owner's answer" in text for text in hub.texts(run_id))
    steps = {str(item["id"]): item["status"] for item in hub.plans[PLAN]["body"]["steps"]}
    assert steps == {"1": "done", "2": "done"}


def test_what_the_runtime_leaves_out_for_a_leased_oauth_token_is_noted_once_a_run_of_two_turns(machine):
    """The daemon's side of step 9 of the worker-credentials plan: the agent's context names what the leases set, and
    the runtime's notes of the agent's environment (``Adapter.environment_notes``; Claude Code's API_KEY_NOTE) reach
    the run's log once, though the agent starts once a turn."""
    token = "sk-ant-oat01-" + secrets.token_hex(16)
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"sh": 'test -n "$CLAUDE_CODE_OAUTH_TOKEN" && echo leased'},
                {"write": {"alpha/a.txt": "a\n"}},
                ask(),
            ],
            f"plan:{PLAN}/2": [
                {"cli": ["step", "1", "done", "--verify", "test -f a.txt"]},
                {"result": {"summary": "Step 1 done on the leased token."}},
            ],
        }
    )
    lease = Lease(1, "env", "secret", "claude-token", env_var="CLAUDE_CODE_OAUTH_TOKEN", value=token)
    found = {}

    async def run_it(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        hub.leases[run_id] = {"leases": [lease.to_json()], "missing": []}  # before the daemon claims it
        assert await hub.wait_state(run_id, "waiting", "done", "failed") == "waiting", hub.texts(run_id)
        hub.answer(1, "yes")
        assert await hub.wait_state(run_id, "done", "failed") == "done", (hub.runs[run_id], hub.texts(run_id))
        found.update(run=run_id, hub=hub)

    with_daemon(machine, run_it)
    run_id, hub = found["run"], found["hub"]
    assert [start["leased"] for start in machine.starts()] == [["CLAUDE_CODE_OAUTH_TOKEN"]] * 2
    assert machine.commands()[0]["stdout"].strip() == "leased"
    note = "The fake agent's environment has the leased CLAUDE_CODE_OAUTH_TOKEN."
    texts = hub.texts(run_id)
    assert texts.count(note) == 1, texts
    assert texts.index(note) > texts.index(f"claude-code started in {machine.directory(run_id)}."), "once it started"
    assert token not in json.dumps(hub.runs[run_id]["events"]), "the lease's value never reaches the log"


def test_a_parked_run_keeps_its_worktrees_and_the_run_that_resumes_it_goes_on_in_them_and_its_session(machine):
    machine.scenarios(
        {
            f"plan:{PLAN}": [{"write": {"alpha/a.txt": "a\n"}}, ask()],
            f"plan:{PLAN}/resume": [
                {"sh": "pwd -P; echo $EVO_RUN_ID; cat alpha/a.txt"},
                {"cli": ["step", "1", "done", "--verify", "test -f a.txt"]},
                {"result": {"summary": "Resumed and done."}},
            ],
        }
    )
    found = {}

    async def run_it(hub: FakeHub, daemon: Daemon):
        parked = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        assert await hub.wait_state(parked, "waiting", "done", "failed") == "waiting", hub.texts(parked)
        hub.park(parked)  # nobody answered within a day
        await wait_for(lambda: parked not in daemon.runs, "the parked run to free its slot")
        record = machine.home.load_run(parked)
        assert record["state"] == "parked" and record["session_id"] and record["parked_at"]
        directory = machine.directory(parked)
        assert (directory / "alpha" / "a.txt").read_text(encoding="utf-8") == "a\n", "the worktrees stay as they were"
        assert git("symbolic-ref", "--short", "HEAD", cwd=directory / "alpha") == "feat/alpha"
        assert any("The hub parked the run" in text for text in hub.texts(parked))
        resumed = hub.answer(1, "yes")  # the answer to a parked run queues the run that resumes it
        assert resumed != parked and hub.runs[resumed]["spec"]["resume_of_run_id"] == parked
        assert await hub.wait_state(resumed, "done", "failed") == "done", (hub.runs[resumed], hub.texts(resumed))
        found.update(parked=parked, resumed=resumed, hub=hub, session=record["session_id"])

    with_daemon(machine, run_it)
    parked, resumed, hub = found["parked"], found["resumed"], found["hub"]
    directory = machine.directory(parked)
    first, again = machine.starts()
    assert first["session"] == found["session"] and again["resume"] == found["session"], "the same session"
    assert again["run"] == resumed and Path(again["cwd"]) == directory, "the same directory and worktrees"
    assert f"goes on now as run #{resumed}" in again["prompt"] and "Answer to decision #1" in again["prompt"]
    looked = machine.commands()[1]["stdout"].splitlines()
    assert looked == [str(directory.resolve()), str(resumed), "a"]
    assert hub.moves(resumed) == ["leased", "running", "verifying", "done"]
    assert hub.runs[parked]["state"] == "done" and hub.runs[parked]["resumed_by"] == resumed
    assert machine.origin("alpha", "log", "--format=%s", "-1", "feat/alpha") == f"run #{resumed} step 1: Alpha file"
    assert hub.decisions[1]["delivered"]
    old, new = machine.home.load_run(parked), machine.home.load_run(resumed)
    assert old["resumed_by"] == resumed and old["dir"] is None and old["repos"] == [], "the cleanup leaves them alone"
    assert new["dir"] == str(directory) and [item["repo"] for item in new["repos"]] == ["alpha", "beta"]
    assert any(f"Goes on from parked run #{parked}" in text for text in hub.texts(resumed))


BUDGET = {"max_usd": 0.5, "max_turns": 40, "max_seconds": 1800, "spent_usd": 0.0, "spent_seconds": 0}


# The budget of a run the night shift queued


def test_a_plan_run_hands_each_agent_what_its_session_spent_and_its_log_names_the_budget_cap(machine):
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"cli": ["step", "1", "in_progress"]},
                {"usage": {"total_cost_usd": 0.3, "input_tokens": 12}},
                ask(),
            ],
            f"plan:{PLAN}/2": [
                {"usage": {"total_cost_usd": 0.5}},
                {
                    "fail": "claude-code stopped at the run's cost cap of $0.50: the session cost $0.50",
                    "cap": "cost",
                },
            ],
        }
    )
    found = {}

    async def run_it(hub, daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()), budget=BUDGET)
        assert await hub.wait_state(run_id, "waiting", "done", "failed") == "waiting", hub.texts(run_id)
        hub.answer(1, "no")
        assert await hub.wait_state(run_id, "done", "failed") == "failed", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, run_it)
    run_id, hub = found["run"], found["hub"]
    first, second = machine.starts()
    assert first["budget"] == BUDGET and (first["spent_usd"], first["spent_seconds"]) == (0.0, 0.0)
    assert second["spent_usd"] == 0.3, "the session's running total after the first turn"
    assert 0 < second["spent_seconds"] < 60, "the agent time of the first turn, not the wait for the answer"
    notes = [event["body"] for event in hub.runs[run_id]["events"] if event["kind"] == "system"]
    assert notes[-1] == {
        "text": "Run failed: claude-code stopped at the run's cost cap of $0.50: the session cost $0.50",
        "cap": "cost",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["step", "1", "done", "--verify", "true"],
        ["ask", "--category", "deploy", "--question", "Now?", "--option", "a=A", "--option", "b=B"],
        ["notify", "--kind", "push_default_branch", "--title", "Pushed main"],
        ["plan"],
    ],
    ids=["step", "ask", "notify", "plan"],
)
def test_the_four_commands_refuse_to_run_outside_a_plan_run(argv, tmp_path, monkeypatch, capsys):
    for key in [key for key in os.environ if key.startswith("EVO_RUN_")]:
        monkeypatch.delenv(key)
    home = WorkerHome(tmp_path / "worker")
    monkeypatch.setenv("EVO_WORKER_HOME", str(home.root))
    assert cli_main(["worker", *argv]) == 1
    assert "EVO_RUN_ID is not set" in capsys.readouterr().err

    home.save(WorkerConfig(url="http://127.0.0.1:9", worker_id=1, name="mac", projects=[PROJECT]), TOKEN)
    monkeypatch.setenv("EVO_RUN_ID", "41")
    assert cli_main(["worker", *argv]) == 1
    assert "run 41 is not running on this worker" in capsys.readouterr().err

    home.save_run({"id": 41, "kind": "step", "finished_at": None})
    assert cli_main(["worker", *argv]) == 1
    assert "run 41 is a run of one step" in capsys.readouterr().err
