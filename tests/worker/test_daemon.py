"""The worker daemon against a real hub: ``evo-agents worker run`` in a process of its own, with an adapter that
follows the adapter interface (``tests.worker.fake_adapter``, sending the sample events of the runtimes), a hub under
uvicorn in a thread of this process, and a bare repository on disk as the origin of the checkout.

The checks step 8 of the worker-fleet plan names: a run is claimed, its events reach the hub, the daemon runs the
verify commands again, commits what was left and pushes the branch; a push to main is refused (and so is a
detached HEAD); events written while the hub is down wait in the spool and reach it in seq order once it is back;
SIGTERM in the middle of a run stops the claims and lets the run end; a verify command that exits other than 0 fails
the run; worker.log holds no token. Around them: the owner's messages and cancel, joining with a pairing code,
status, drain and revoke.

Step 6 of the plan-runs-and-decisions plan: the run's commit leaves out what hooks wrote (``.claude/skills/.learned/``)
and copies of hub plans as the hub wrote them, and names them in the run's log; an agent a daemon killed with SIGKILL
left running is stopped by the next daemon, which removes the run's worktree and evo-run branch once the hub no longer
holds the run, and keeps the worktree of a run the hub parked."""

import asyncio
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

import httpx
import uvicorn

from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import open_pool
from evo_agents.hub.mirror import render
from evo_agents.hub.server import run_state
from evo_agents.hub.server.app import create_app
from tests.hub.live import ADMIN, bearer
from tests.hub.test_plans import registration
from tests.hub.test_runs import OWNER, PLAN, PROJECT, WRITER, plan_body
from tests.worker import fake_adapter

ROOT = Path(__file__).parents[2]
REPO = "evo-agents"
BRANCH = "feat/queue"  # the branch plan_body() gives the repo
WORKER = "mac-mini"
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Plan Owner",
    "GIT_AUTHOR_EMAIL": "owner@example.org",
    "GIT_COMMITTER_NAME": "Plan Owner",
    "GIT_COMMITTER_EMAIL": "owner@example.org",
}
TOKEN_LEAK = re.compile(r"ev[hsw]_(?!\*\*\*)[A-Za-z0-9_-]{8,}")
WAIT = 90.0


def wait_until(predicate, what: str, timeout: float = WAIT, explain=lambda: ""):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:g}s waiting for {what}\n{explain()}")
        time.sleep(0.1)


class Server:
    """The hub under uvicorn on a port of its own, in a thread; it can stop and start again on the same port."""

    def __init__(self, config, port: int):
        self.config = config
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.server = None
        self.thread = None

    def start(self) -> None:
        app = create_app(self.config)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            lifespan="on",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="hub-under-test", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            assert self.thread.is_alive(), "the hub did not start"
            assert time.monotonic() < deadline, "the hub did not start within 30s"
            time.sleep(0.05)

    def stop(self) -> None:
        if self.server is None:
            return
        self.server.should_exit = True
        self.thread.join(30)
        assert not self.thread.is_alive(), "the hub did not stop"
        self.server = None


def git(*args, cwd: Path | None = None, env=None) -> str:
    command = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    done = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, f"{command}: {done.stderr}"
    return done.stdout.strip()


class Stack:
    """A hub with project evo-agents and the plan rollout pushed by owner, a home directory for the worker with the
    owner signed in to the hub, a checkout of evo-agents whose origin is a bare repository, and the harness registry
    pointing at it."""

    def __init__(self, tmp: Path, server: Server, client: httpx.Client, headers: dict, machine_token: str):
        self.tmp = tmp
        self.server = server
        self.url = server.url
        self.client = client
        self.owner = headers
        self.machine_token = machine_token
        self.home = tmp / "home"
        self.state = self.home / ".evo" / "worker"
        self.origin = tmp / "origin.git"
        self.checkout = self.home / "ws" / REPO
        self.scenarios_path = tmp / "scenarios.json"
        self.messages_path = tmp / "messages.txt"
        self.daemon_log = tmp / "daemon.out"
        self.worker_token: str | None = None
        self.daemons: list[subprocess.Popen] = []
        bin_dir = tmp / "bin"  # git alone on PATH: the runtimes of this machine stay out of the test
        bin_dir.mkdir()
        os.symlink(shutil.which("git"), bin_dir / "git")
        self.env = pg.clean_env(
            HOME=str(self.home),
            PATH=f"{bin_dir}:/usr/bin:/bin",
            PYTHONPATH=str(ROOT),
            EVO_WORKER_HEARTBEAT_SECONDS="1",
            **GIT_IDENTITY,
            **fake_adapter.environment(self.scenarios_path, self.messages_path),
        )
        self.home.mkdir()
        self.seed_sha = self._repos()
        self._sign_in()
        self._registry()
        self.scenarios({})

    def _repos(self) -> str:
        seed = self.tmp / "seed"
        seed.mkdir()
        env = {**os.environ, **GIT_IDENTITY}
        git("init", "--quiet", "--initial-branch", "main", cwd=seed, env=env)
        (seed / "README.md").write_text("# evo-agents\n", encoding="utf-8")
        git("add", "README.md", cwd=seed, env=env)
        git("commit", "--quiet", "-m", "first commit", cwd=seed, env=env)
        git("clone", "--quiet", "--bare", str(seed), str(self.origin), env=env)
        self.checkout.parent.mkdir(parents=True)
        git("clone", "--quiet", str(self.origin), str(self.checkout), env=env)
        return git("rev-parse", "HEAD", cwd=seed)

    def _sign_in(self) -> None:
        hub_dir = self.home / ".evo" / "hub"
        hub_dir.mkdir(parents=True, mode=0o700)
        (hub_dir / "config.json").write_text(json.dumps({"url": self.url, "login": OWNER}), encoding="utf-8")
        (hub_dir / "token").write_text(self.machine_token + "\n", encoding="utf-8")
        for path in hub_dir.iterdir():
            path.chmod(0o600)

    def _registry(self) -> None:
        registry = self.home / ".claude" / "harness" / "registry.json"
        registry.parent.mkdir(parents=True)
        cluster = {
            "name": PROJECT,
            "root": str(self.home / "ws" / "evo-agents-harness"),
            "workspace": str(self.home / "ws"),
            "repos": [str(self.checkout)],
            "hub": {"url": self.url, "project": PROJECT},
        }
        registry.write_text(json.dumps({"clusters": [cluster]}), encoding="utf-8")

    # The worker

    def scenarios(self, scenarios: dict) -> None:
        self.scenarios_path.write_text(json.dumps(scenarios), encoding="utf-8")

    def cli(self, *args: str, env=None) -> subprocess.CompletedProcess:
        return pg.cli(["worker", *args], env or self.env)

    def register(self, slots: int = 1) -> dict:
        result = self.cli("register", "--name", WORKER, "--project", PROJECT, "--slots", str(slots))
        assert result.returncode == 0, result.stderr
        self.worker_token = (self.state / "token").read_text(encoding="utf-8").strip()
        assert self.worker_token not in result.stdout + result.stderr
        return json.loads((self.state / "config.json").read_text(encoding="utf-8"))

    def start_daemon(self) -> subprocess.Popen:
        with open(self.daemon_log, "ab") as out:
            proc = subprocess.Popen(
                [sys.executable, "-m", "evo_agents", "worker", "run"],
                env=self.env,
                stdout=out,
                stderr=subprocess.STDOUT,
                cwd=ROOT,
            )
        self.daemons.append(proc)
        self.wait_log("worker started", proc)
        return proc

    def kill_daemons(self) -> None:
        """Stop every daemon the test started, also when it failed before stopping them."""
        for proc in self.daemons:
            if proc.poll() is None:
                proc.kill()
                proc.wait(30)

    def daemon_output(self) -> str:
        return self.daemon_log.read_text(encoding="utf-8", errors="replace") if self.daemon_log.exists() else ""

    def wait_log(self, text: str, proc=None, timeout: float = WAIT) -> None:
        def seen():
            if proc is not None and proc.poll() is not None and text not in self.daemon_output():
                pytest.fail(f"the daemon exited {proc.returncode} before logging {text!r}:\n{self.daemon_output()}")
            return text in self.daemon_output()

        wait_until(seen, f"{text!r} in the daemon's log", timeout, self.daemon_output)

    def stop_daemon(self, proc: subprocess.Popen, timeout: float = 60) -> int:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            return proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail(f"the daemon did not stop within {timeout}s of SIGTERM:\n{self.daemon_output()}")

    # The hub

    def push_plan(self, body: dict) -> None:
        path = f"/v1/projects/{PROJECT}/plans/{body['id']}"
        response = self.client.put(path, json={"body": body}, headers=self.owner)
        assert response.status_code == 200, response.text

    def dispatch(self, steps, plan: str = PLAN, **extra) -> list[dict]:
        body = {"plan_id": plan, "steps": steps, "runtime": "claude-code", "approval": "auto", **extra}
        response = self.client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=self.owner)
        assert response.status_code == 201, response.text
        return response.json()

    def run(self, run_id: int) -> dict:
        response = self.client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=self.owner)
        assert response.status_code == 200, response.text
        return response.json()

    def wait_state(self, run_id: int, *states: str, timeout: float = WAIT) -> dict:
        found = {}

        def reached():
            found.update(self.run(run_id))
            return found["state"] in states

        def explain():
            return f"run {run_id} is {found.get('state')} ({found.get('error')})\n{self.daemon_output()[-6000:]}"

        wait_until(reached, f"run {run_id} to be {' or '.join(states)}", timeout, explain)
        return found

    def events(self, run_id: int) -> list[dict]:
        events, after = [], 0
        while True:
            path = f"/v1/projects/{PROJECT}/runs/{run_id}/events"
            response = self.client.get(path, params={"after": after, "limit": 1000}, headers=self.owner)
            assert response.status_code == 200, response.text
            page = response.json()
            events += page["events"]
            if not page["more"]:
                return events
            after = page["events"][-1]["seq"]

    def step(self, key: int, plan: str = PLAN) -> dict:
        response = self.client.get(f"/v1/projects/{PROJECT}/plans/{plan}", headers=self.owner)
        assert response.status_code == 200, response.text
        return next(item for item in response.json()["body"]["steps"] if item["id"] == key)

    # Git

    def origin_rev(self, ref: str) -> str | None:
        done = subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
        )
        return done.stdout.strip() or None

    def origin_git(self, *args: str) -> str:
        return git("--git-dir", str(self.origin), *args)

    def worktree(self, run_id: int) -> Path:
        return self.state / "worktrees" / f"{PROJECT}-{run_id}"

    # Secrets

    def assert_no_token(self, text: str, where: str) -> None:
        for token in (self.worker_token, self.machine_token):
            assert token and token not in text, f"a token is in {where}"
        leaked = TOKEN_LEAK.findall(text)
        assert not leaked, f"{where} holds what looks like a token"


@pytest.fixture
def make_stack(hub_db, tmp_path, github):
    """Start the hub with the configuration changes given and set up the rest (``Stack``)."""
    with ExitStack() as stack:

        def started(**changes) -> Stack:
            config = live.hub_config(hub_db, tmp_path, github, session_secret=live.session_secret(), **changes)
            server = Server(config, pg.free_port())
            server.start()
            stack.callback(server.stop)
            client = stack.enter_context(httpx.Client(base_url=server.url, timeout=30))
            admin = bearer(live.sign_in(client, github, ADMIN, 801)["token"])
            owner_token = live.sign_in(client, github, OWNER, 802)["token"]
            owner = bearer(owner_token)
            assert client.put(f"/v1/projects/{PROJECT}", json=registration(), headers=admin).status_code == 200
            granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=WRITER, headers=admin)
            assert granted.status_code == 200, granted.text
            pushed = client.put(f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": plan_body()}, headers=owner)
            assert pushed.status_code == 200, pushed.text
            made = Stack(tmp_path, server, client, owner, owner_token)
            stack.callback(made.kill_daemons)  # before the hub stops
            return made

        yield started


def finished_cleanly(stack: Stack, proc: subprocess.Popen) -> None:
    """The daemon stops on SIGTERM with exit status 0, and nothing it wrote holds a token."""
    assert stack.stop_daemon(proc) == 0, stack.daemon_output()
    log_text = (stack.state / "worker.log").read_text(encoding="utf-8")
    stack.assert_no_token(log_text, "worker.log")
    stack.assert_no_token(stack.daemon_output(), "the daemon's stderr")
    for line in log_text.splitlines():
        json.loads(line)  # every line is one JSON object


# The checks of the step


def test_a_run_is_claimed_verified_committed_and_pushed_with_its_events_on_the_hub(make_stack, s3):
    stack = make_stack(**s3.config())
    stack.scenarios(
        {
            "2": [
                {"samples": True},
                {"write": {"feature.txt": "hello\n"}},
                {"commit": "agent: add feature.txt"},
                {"write": {"notes.txt": "left for the worker to commit\n"}},
                {
                    "result": {
                        "verify_commands": ["test -f feature.txt", "grep -q hello feature.txt"],
                        "summary": "Added feature.txt and notes.txt.",
                    }
                },
            ]
        }
    )
    config = stack.register()
    assert config["name"] == WORKER and config["projects"] == [PROJECT]
    assert config["repos"][PROJECT]["repos"], "register keeps the project's repos as the hub lists them"
    proc = stack.start_daemon()
    run_id = stack.dispatch([2])[0]["id"]
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], stack.daemon_output()[-6000:])

    # The commit the hub records is the branch on origin: the agent's commit, then the rest as "run #N: title".
    assert run["commit_sha"] == stack.origin_rev(f"refs/heads/{BRANCH}")
    assert stack.origin_git("log", "--format=%s", "-2", BRANCH).splitlines() == [
        f"run #{run_id}: Queue",
        "agent: add feature.txt",
    ]
    files = set(stack.origin_git("ls-tree", "-r", "--name-only", BRANCH).splitlines())
    assert files == {"README.md", "feature.txt", "notes.txt"}, "the result file stays out of the commits"
    assert stack.origin_rev("refs/heads/main") == stack.seed_sha, "main is never pushed"
    assert run["diffstat"] == {"files": 2, "insertions": 2, "deletions": 0}
    assert [(item["command"], item["exit_code"]) for item in run["verify"]] == [
        ("test -f feature.txt", 0),
        ("grep -q hello feature.txt", 0),
    ]
    assert run["session_id"] and run["usage"]["total_cost_usd"] == 0.2041276
    uploaded = wait_until(lambda: stack.run(run_id)["diff_sha256"] and stack.run(run_id), "the run's log and diff")
    assert uploaded["log_sha256"] and uploaded["diff_sha256"], "the log and the diff go to the blob store at the end"

    # The events reached the hub in order, numbered without a gap.
    events = stack.events(run_id)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    kinds = [event["kind"] for event in events]
    for kind in ("system", "output", "tool_call", "tool_call_update", "agent_message_chunk", "usage_update", "state"):
        assert kind in kinds, kind
    call = next(event for event in events if event["kind"] == "tool_call")
    assert call["body"]["toolCallId"] == "toolu_01P69eEarUZUJB9qpZopfcYy"
    assert call["body"]["rawInput"]["command"] == "sleep 8 && echo slept"
    assert kinds.index("tool_call") < kinds.index("tool_call_update") < kinds.index("agent_message_chunk")
    texts = [event["body"].get("text", "") for event in events if event["kind"] == "system"]
    assert "verify: `test -f feature.txt` exited 0" in " ".join(texts)
    assert any(text.startswith("Pushed ") for text in texts)
    moves = [event["body"]["to"] for event in events if event["kind"] == "state"]
    assert moves == ["leased", "running", "verifying", "done"]

    # The plan has the step done; the owner's checkout was left alone; the worktree let go of the branch.
    assert stack.step(2)["status"] == "done"
    assert git("symbolic-ref", "--short", "HEAD", cwd=stack.checkout) == "main"
    assert git("status", "--porcelain", cwd=stack.checkout) == ""
    worktree = stack.worktree(run_id)
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "hello\n"
    detached = subprocess.run(["git", "-C", str(worktree), "symbolic-ref", "-q", "HEAD"], capture_output=True)
    assert detached.returncode != 0, "the worktree leaves the plan's branch once the run is over"
    assert not any((stack.state / "spool").iterdir()), "the spool is empty once the hub has every event"

    # The state directory is private.
    assert stat.S_IMODE((stack.state).stat().st_mode) == 0o700
    for name in ("token", "config.json", "worker.log"):
        assert stat.S_IMODE((stack.state / name).stat().st_mode) == 0o600, name
    finished_cleanly(stack, proc)


def test_the_daemon_refuses_to_push_main_or_a_detached_head(make_stack):
    stack = make_stack()
    started = stack.tmp / "mainline-started"
    stack.push_plan(
        {
            "id": "mainline",
            "goal": "Straight to main.",
            "repos": [{"repo": REPO, "branch": "main", "status": "pending"}],
            "steps": [{"id": 1, "title": "On main", "repo": REPO, "what": "anything", "status": "pending"}],
        }
    )
    stack.scenarios(
        {
            "1": [{"touch": str(started)}, {"write": {"x.txt": "x"}}, {"result": {"verify_commands": ["true"]}}],
            "4": [
                {"write": {"docs.txt": "docs\n"}},
                {"commit": "agent: docs"},
                {"git": ["checkout", "--quiet", "--detach"]},
                {"result": {"verify_commands": ["test -f docs.txt"]}},
            ],
        }
    )
    stack.register()
    proc = stack.start_daemon()

    run_id = stack.dispatch([1], plan="mainline")[0]["id"]
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "failed"
    assert "never pushes" in run["error"] and "main" in run["error"]
    assert not started.exists(), "the agent never starts on a run that would push the default branch"
    assert stack.origin_rev("refs/heads/main") == stack.seed_sha
    assert stack.step(1, plan="mainline")["status"] == "pending"

    run_id = stack.dispatch([4])[0]["id"]
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "failed"
    assert "detached" in run["error"]
    assert stack.origin_rev(f"refs/heads/{BRANCH}") is None, "nothing was pushed"
    assert stack.origin_rev("refs/heads/main") == stack.seed_sha
    assert stack.step(4)["status"] == "pending"
    finished_cleanly(stack, proc)


def test_events_wait_in_the_spool_while_the_hub_is_down_and_reach_it_in_seq_order(make_stack):
    stack = make_stack()
    reached, go = stack.tmp / "reached", stack.tmp / "go"
    stack.scenarios(
        {
            "2": [
                {"chunks": [1, 5]},
                {"touch": str(reached)},
                {"wait_for": str(go)},
                {"chunks": [6, 45]},
                {"write": {"f.txt": "f\n"}},
                {"result": {"verify_commands": ["test -f f.txt"]}},
            ]
        }
    )
    stack.register()
    proc = stack.start_daemon()
    run_id = stack.dispatch([2])[0]["id"]
    wait_until(reached.exists, "the agent to reach the gate", explain=stack.daemon_output)

    def chunks_on_hub() -> list[str]:
        return [
            event["body"]["content"]["text"] for event in stack.events(run_id) if event["kind"] == "agent_message_chunk"
        ]

    wait_until(lambda: "chunk 5" in chunks_on_hub(), "the first chunks on the hub", explain=stack.daemon_output)
    stack.server.stop()
    go.touch()
    spool = stack.state / "spool" / f"{run_id}.jsonl"

    def spooled() -> int:
        if not spool.exists():
            return 0
        return sum(1 for line in spool.read_text(encoding="utf-8").splitlines() if '"chunk ' in line)

    wait_until(
        lambda: spooled() >= 40,
        "the chunks written while the hub is down to be in the spool",
        explain=stack.daemon_output,
    )
    stack.wait_log("events not sent; they wait in the spool", proc)
    time.sleep(2)  # the agent ends and the daemon runs the verify command while the hub is away
    assert proc.poll() is None, "the run goes on without the hub"
    stack.server.start()

    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", run["error"]
    assert chunks_on_hub() == [f"chunk {number}" for number in range(1, 46)], "every chunk once, in order"
    events = stack.events(run_id)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert not spool.exists() or spool.stat().st_size == 0
    finished_cleanly(stack, proc)


def test_sigterm_stops_the_claims_and_lets_the_held_run_end(make_stack):
    stack = make_stack()
    started, go = stack.tmp / "started", stack.tmp / "go"
    stack.scenarios(
        {
            "2": [
                {"touch": str(started)},
                {"wait_for": str(go)},
                {"write": {"f.txt": "f\n"}},
                {"result": {"verify_commands": ["test -f f.txt"]}},
            ]
        }
    )
    stack.register(slots=1)
    proc = stack.start_daemon()
    first = stack.dispatch([2])[0]["id"]
    wait_until(started.exists, "the agent to start", explain=stack.daemon_output)

    proc.send_signal(signal.SIGTERM)
    stack.wait_log("stopping: no new claims", proc)
    second = stack.dispatch([4])[0]["id"]
    time.sleep(2.5)  # heartbeats go on; a claim would have taken the run by now
    assert stack.run(second)["state"] == "queued"
    assert proc.poll() is None, "the daemon waits for the run it holds"
    go.touch()

    assert proc.wait(WAIT) == 0, stack.daemon_output()
    assert stack.run(first)["state"] == "done"
    assert stack.run(second)["state"] == "queued", "a stopping daemon claims nothing"
    assert "worker stopped" in stack.daemon_output()
    stack.assert_no_token((stack.state / "worker.log").read_text(encoding="utf-8"), "worker.log")


def test_a_verify_command_that_exits_non_zero_fails_the_run_and_nothing_is_pushed(make_stack):
    stack = make_stack()
    stack.scenarios({"2": [{"write": {"f.txt": "f\n"}}, {"result": {"verify_commands": ["test -f f.txt", "exit 3"]}}]})
    stack.register()
    proc = stack.start_daemon()
    run_id = stack.dispatch([2])[0]["id"]
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "failed"
    assert "`exit 3` exited 3" in run["error"]
    assert [(item["command"], item["exit_code"]) for item in run["verify"]] == [("test -f f.txt", 0), ("exit 3", 3)]
    assert stack.origin_rev(f"refs/heads/{BRANCH}") is None
    assert stack.step(2)["status"] == "pending"
    assert (stack.worktree(run_id) / "f.txt").exists(), "the work stays in the worktree"
    texts = [event["body"].get("text", "") for event in stack.events(run_id) if event["kind"] == "system"]
    assert "verify: `exit 3` exited 3" in " ".join(texts)
    finished_cleanly(stack, proc)


# Around them


def test_a_plan_branch_checked_out_by_the_owner_is_left_alone_and_pushed_from_a_run_branch(make_stack):
    stack = make_stack()
    env = {**os.environ, **GIT_IDENTITY}
    git("checkout", "--quiet", "-b", BRANCH, cwd=stack.checkout, env=env)
    (stack.checkout / "mine.txt").write_text("the owner's own work, not committed\n", encoding="utf-8")
    head = git("rev-parse", "HEAD", cwd=stack.checkout)
    stack.scenarios({"2": [{"write": {"f.txt": "f\n"}}, {"result": {"verify_commands": ["test -f f.txt"]}}]})
    stack.register()
    proc = stack.start_daemon()
    run_id = stack.dispatch([2])[0]["id"]
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", run["error"]
    assert run["commit_sha"] == stack.origin_rev(f"refs/heads/{BRANCH}")
    assert git("symbolic-ref", "--short", "HEAD", cwd=stack.checkout) == BRANCH
    assert git("rev-parse", "HEAD", cwd=stack.checkout) == head, "the owner's branch did not move under them"
    assert git("status", "--porcelain", cwd=stack.checkout) == "?? mine.txt"
    assert git("symbolic-ref", "--short", "HEAD", cwd=stack.worktree(run_id)) == f"evo-run/{run_id}"
    texts = [event["body"].get("text", "") for event in stack.events(run_id) if event["kind"] == "system"]
    assert any(f"the run works on evo-run/{run_id} and pushes it to {BRANCH}" in text for text in texts)
    finished_cleanly(stack, proc)


def test_the_owners_messages_reach_the_agent_and_a_cancel_stops_it(make_stack):
    stack = make_stack()
    started, never = stack.tmp / "started", stack.tmp / "never"
    stack.scenarios({"2": [{"touch": str(started)}, {"wait_for": str(never)}]})
    stack.register()
    proc = stack.start_daemon()
    run_id = stack.dispatch([2])[0]["id"]
    wait_until(started.exists, "the agent to start", explain=stack.daemon_output)
    stack.wait_state(run_id, "running")

    sent = stack.client.post(
        f"/v1/projects/{PROJECT}/runs/{run_id}/messages", json={"text": "also update the docs"}, headers=stack.owner
    )
    assert sent.status_code in (200, 201), sent.text
    wait_until(
        lambda: stack.messages_path.exists() and "also update the docs" in stack.messages_path.read_text("utf-8"),
        "the message to reach the agent",
        explain=stack.daemon_output,
    )

    cancelled = stack.client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/cancel", headers=stack.owner)
    assert cancelled.status_code == 200, cancelled.text
    run = stack.wait_state(run_id, "cancelled")
    assert run["state"] == "cancelled"
    events = stack.events(run_id)
    agent = [event["body"]["content"]["text"] for event in events if event["kind"] == "agent_message_chunk"]
    assert "got: also update the docs" in agent
    assert any("Cancelled by the owner" in event["body"].get("text", "") for event in events)
    finished_cleanly(stack, proc)


def test_join_with_a_pairing_code_then_status_drain_and_revoke(make_stack):
    stack = make_stack()
    pairing = stack.client.post(
        "/v1/workers/pairings",
        json={"name": "lab-01", "projects": [PROJECT], "slots": 2, "labels": ["gpu"]},
        headers=stack.owner,
    )
    assert pairing.status_code == 201, pairing.text
    code = pairing.json()["code"]
    signed_in = stack.home / ".evo" / "hub"
    parked = stack.tmp / "hub-credentials"
    signed_in.rename(parked)  # joining needs no sign-in

    joined = stack.cli("join", "--url", stack.url, "--code", code.lower())
    assert joined.returncode == 0, joined.stderr
    assert "Joined" in joined.stdout and "lab-01" in joined.stdout
    assert f"checkout {PROJECT}/{REPO}: {stack.checkout}" in joined.stdout, "found through the harness registry"
    token = (stack.state / "token").read_text(encoding="utf-8").strip()
    assert token.startswith("evw_") and token not in joined.stdout + joined.stderr
    config = json.loads((stack.state / "config.json").read_text(encoding="utf-8"))
    assert (config["name"], config["slots"], config["labels"]) == ("lab-01", 2, ["gpu"])

    again = stack.cli("join", "--url", stack.url, "--code", code)
    assert again.returncode == 1 and "this machine is worker lab-01" in again.stderr

    status = stack.cli("status", "--json")
    assert status.returncode == 0, status.stderr
    shown = json.loads(status.stdout)
    assert shown["name"] == "lab-01" and shown["daemon_pid"] is None
    assert shown["runtimes"]["claude-code"]["available"] is True
    assert shown["runtimes"]["codex"] == {"available": False, "version": None, "reason": "codex is not on PATH"}
    assert list(shown["checkouts"]) == [f"{PROJECT}/{REPO}"]

    refused = stack.cli("drain")
    assert refused.returncode == 1 and "evo-agents hub login" in refused.stderr
    parked.rename(signed_in)
    worker_id = config["worker_id"]
    assert stack.cli("drain").returncode == 0
    shown = stack.client.get(f"/v1/workers/{worker_id}", headers=stack.owner).json()
    assert shown["drained_at"] is not None
    assert stack.cli("drain", "--resume").returncode == 0
    assert stack.client.get(f"/v1/workers/{worker_id}", headers=stack.owner).json()["drained_at"] is None

    service_files = (  # a background service installed for this user, as launchd and systemd keep it
        stack.home / "Library" / "LaunchAgents" / "io.github.maycuatroi1.evo-agents.worker.plist",
        stack.home / ".config" / "systemd" / "user" / "evo-agents-worker.service",
    )
    for path in service_files:
        path.parent.mkdir(parents=True)
        path.write_text("installed\n", encoding="utf-8")
    env = {key: value for key, value in stack.env.items() if key != "XDG_CONFIG_HOME"}
    revoked = stack.cli("revoke", env=env)
    assert revoked.returncode == 0, revoked.stderr
    assert "does not start the daemon again" in revoked.stdout
    assert "evo-agents worker service uninstall" in revoked.stdout
    assert all(path.exists() for path in service_files), "revoke leaves the service to `service uninstall`"
    assert stack.client.get(f"/v1/workers/{worker_id}", headers=stack.owner).json()["revoked_at"] is not None
    assert not (stack.state / "token").exists() and not (stack.state / "config.json").exists()
    no_worker = stack.cli("status")
    assert no_worker.returncode == 1 and "evo-agents worker join" in no_worker.stderr


def test_a_revoked_worker_stops_its_daemon_with_the_status_a_service_does_not_restart(make_stack):
    stack = make_stack()
    config = stack.register()
    proc = stack.start_daemon()
    revoked = stack.client.post(f"/v1/workers/{config['worker_id']}/revoke", headers=stack.owner)
    assert revoked.status_code == 200, revoked.text
    assert proc.wait(WAIT) == 3, stack.daemon_output()  # RestartPreventExitStatus=3 in the systemd unit
    assert "the hub no longer takes this worker" in stack.daemon_output()

    # Under the LaunchAgent, which sets EVO_WORKER_REVOKED_EXIT to 0: launchd starts any other status again.
    again = stack.cli("run", env={**stack.env, "EVO_WORKER_REVOKED_EXIT": "0"})
    assert again.returncode == 0, again.stderr
    assert (stack.state / "worker.log").read_text(encoding="utf-8").count("the hub no longer takes this worker") == 2


# What a run's commit leaves out, and the agents a dead daemon left


def test_hook_files_and_untouched_plan_copies_stay_out_of_the_runs_commit(make_stack):
    stack = make_stack()
    view = stack.client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}", headers=stack.owner).json()
    copy = render(view["body"], PROJECT, view["revision"], view["digest"])  # as `hub plan export` writes it
    edited = copy.replace("goal: Ship the run queue.", "goal: Ship the run queue, edited by the agent.")
    assert edited != copy
    learned = ".claude/skills/.learned/auto-skill/SKILL.md"
    nested = "docs/.claude/skills/.learned/nested.md"
    stack.scenarios(
        {
            "2": [
                {
                    "write": {
                        "feature.txt": "hello\n",
                        learned: "---\nname: auto-skill\n---\nlearned in the session\n",
                        nested: "learned below the root\n",
                        f"plans/active/{PLAN}.yaml": copy,
                        "plans/active/edited.yaml": edited,
                    }
                },
                {"git": ["add", "--all"]},  # staged by the agent: the daemon still keeps them out
                {"result": {"verify_commands": ["test -f feature.txt"]}},
            ]
        }
    )
    stack.register()
    proc = stack.start_daemon()
    run_id = stack.dispatch([2])[0]["id"]
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], stack.daemon_output()[-6000:])

    files = set(stack.origin_git("ls-tree", "-r", "--name-only", BRANCH).splitlines())
    assert files == {"README.md", "feature.txt", "plans/active/edited.yaml"}, "a copy the agent edited is its work"
    assert run["commit_sha"] == stack.origin_rev(f"refs/heads/{BRANCH}")
    notes = [
        event["body"] for event in stack.events(run_id) if event["kind"] == "system" and "left_out" in event["body"]
    ]
    assert len(notes) == 1, notes
    assert notes[0]["left_out"] == sorted([learned, nested, f"plans/active/{PLAN}.yaml"])
    assert notes[0]["text"].startswith("Left out of the commit, as what a hook or a plan export wrote")
    status = git("status", "--porcelain", "--untracked-files=all", cwd=stack.worktree(run_id)).splitlines()
    untracked = (learned, nested, f"plans/active/{PLAN}.yaml", ".evo-run/result.json")
    assert sorted(status) == sorted(f"?? {path}" for path in untracked), "what was left out stays, untracked"
    finished_cleanly(stack, proc)


ORPHAN_LEASE = 4  # seconds of a lease in the orphan tests, so a dead daemon's run is let go of soon
SLEEPER = [sys.executable, "-c", "import time; time.sleep(600)"]


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def agent_of(stack: Stack, run_id: int) -> dict | None:
    """runs/<run>/agent.json once it names a process group."""
    path = stack.state / "runs" / str(run_id) / "agent.json"
    try:
        agent = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return agent if agent.get("pgid") else None


def reap(stack: Stack) -> dict:
    """One pass of the hub's reaper (the job hub.recover_runs), on the hub's database."""

    async def once() -> dict:
        config = HubConfig(dsn=stack.server.config.dsn, data_dir=stack.tmp, pool_min_size=1, pool_max_size=1)
        pool = await open_pool(config)
        try:
            return await run_state.recover_runs(pool)
        finally:
            await pool.close()

    return asyncio.run(once())


def orphan(stack: Stack) -> tuple[int, int, subprocess.Popen]:
    """Run step 2 with an agent that starts a process in a session of its own and waits, then kill the daemon with
    SIGKILL: the run's id, the agent's process group (alive), and the dead daemon."""
    started, never = stack.tmp / "started", stack.tmp / "never"
    stack.scenarios({"2": [{"spawn": SLEEPER}, {"chunks": [1, 1]}, {"touch": str(started)}, {"wait_for": str(never)}]})
    stack.register()
    proc = stack.start_daemon()
    run_id = stack.dispatch([2])[0]["id"]
    wait_until(started.exists, "the agent to start", explain=stack.daemon_output)
    agent = wait_until(lambda: agent_of(stack, run_id), "agent.json to name the agent's process group")
    assert agent["pid"] == agent["pgid"] and agent["started"] and agent["runtime"] == "claude-code"
    assert group_alive(agent["pgid"])
    proc.kill()
    proc.wait(30)
    time.sleep(0.5)
    assert group_alive(agent["pgid"]), "the agent outlives a daemon killed with SIGKILL"
    return run_id, agent["pgid"], proc


def dealt_with(stack: Stack, run_id: int) -> dict:
    """The line of worker.log that says what the daemon did with the orphan of run ``run_id``, once it is there."""

    def found() -> dict | None:
        for line in (stack.state / "worker.log").read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("msg") == "the agent a previous daemon left was dealt with" and record["run_id"] == run_id:
                return record
        return None

    return wait_until(found, f"worker.log to say what became of run {run_id}'s agent", explain=stack.daemon_output)


def test_an_orphan_agent_of_a_run_the_hub_let_go_is_stopped_and_its_worktree_removed(make_stack):
    stack = make_stack(run_lease_seconds=ORPHAN_LEASE)
    git("checkout", "--quiet", "-b", BRANCH, cwd=stack.checkout, env={**os.environ, **GIT_IDENTITY})
    run_id, pgid, _ = orphan(stack)
    worktree = stack.worktree(run_id)
    assert worktree.is_dir() and git("branch", "--list", f"evo-run/{run_id}", cwd=stack.checkout)

    cancelled = stack.client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/cancel", headers=stack.owner)
    assert cancelled.status_code == 200, cancelled.text
    time.sleep(ORPHAN_LEASE)

    def let_go() -> bool:
        reap(stack)
        if stack.run(run_id)["state"] == "cancelled":
            return True
        time.sleep(0.5)
        return False

    wait_until(let_go, "the reaper to end the run whose lease ran out")
    assert group_alive(pgid), "nothing stopped the agent yet"

    proc = stack.start_daemon()
    wait_until(lambda: not group_alive(pgid), "the new daemon to stop the agent", explain=stack.daemon_output)
    wait_until(lambda: not worktree.exists(), "the new daemon to remove the worktree", explain=stack.daemon_output)
    record = dealt_with(stack, run_id)
    assert record["agent"] == f"process group {pgid} stopped on SIGTERM" and record["hub_state"] is None
    assert record["worktree"].startswith("removed with its evo-run branch")
    assert git("branch", "--list", f"evo-run/{run_id}", cwd=stack.checkout) == ""
    assert git("symbolic-ref", "--short", "HEAD", cwd=stack.checkout) == BRANCH, "the owner's branch stays"
    assert not (stack.state / "runs" / str(run_id) / "agent.json").exists()
    assert "the agent a previous daemon left was dealt with" in stack.daemon_output()
    finished_cleanly(stack, proc)


def test_an_orphan_agent_of_a_parked_run_is_stopped_and_its_worktree_kept(make_stack, hub_db):
    stack = make_stack()
    run_id, pgid, _ = orphan(stack)
    # The hub parks a plan run that waited a day for its owner; a run of one step is parked here by hand, which is
    # all the heartbeat looks at (state parked, on this worker).
    live.sql(
        hub_db,
        "UPDATE runs SET state = 'parked', parked_at = now(), lease_expires_at = NULL, counted_at = NULL WHERE id = %s",
        (run_id,),
    )

    proc = stack.start_daemon()
    wait_until(lambda: not group_alive(pgid), "the new daemon to stop the agent", explain=stack.daemon_output)
    record = dealt_with(stack, run_id)
    assert record["agent"] == f"process group {pgid} stopped on SIGTERM" and record["hub_state"] == "parked"
    assert stack.worktree(run_id).is_dir(), "the worktree stays for the run that resumes the parked one"
    saved = json.loads((stack.state / "runs" / str(run_id) / "run.json").read_text(encoding="utf-8"))
    assert saved["state"] == "parked" and saved["finished_at"]
    assert not (stack.state / "runs" / str(run_id) / "agent.json").exists()
    finished_cleanly(stack, proc)


def test_an_orphan_agent_of_a_run_the_hub_still_holds_is_stopped_and_its_lease_runs_out(make_stack):
    stack = make_stack(run_lease_seconds=ORPHAN_LEASE)
    run_id, pgid, _ = orphan(stack)
    proc = stack.start_daemon()  # at once: the hub still holds the run for this worker
    wait_until(lambda: not group_alive(pgid), "the new daemon to stop the agent", explain=stack.daemon_output)
    record = dealt_with(stack, run_id)
    assert record["hub_state"] == "running" and record["worktree"].startswith("kept: the hub still holds the run")
    assert stack.worktree(run_id).is_dir()

    def lost() -> bool:
        reap(stack)
        if stack.run(run_id)["state"] == "lost":
            return True
        time.sleep(0.5)
        return False

    stack.scenarios({})  # the next attempt, which this daemon may claim, ends at once
    wait_until(lost, "the run's lease to run out: no heartbeat after the first names it", explain=stack.daemon_output)
    finished_cleanly(stack, proc)
