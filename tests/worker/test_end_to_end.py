"""The worker fleet end to end: the hub as it is deployed, ``evo-agents hub serve`` and ``evo-agents hub worker``
(the job worker, whose reaper finds runs lost) in processes of their own, as web/e2e/hub_stack.py runs them, on a
database of the test's own, with the fake GitHub and the fake S3 of the hub's tests. Each machine is a HOME of its own
with the owner signed in, a checkout cloned from a bare repository on disk (the origin), and ``evo-agents worker
run``, the real daemon, whose runtime is the fake adapter of ``tests.worker.fake_adapter``. The owner works through
the CLI: ``evo-agents hub run dispatch``, ``logs --follow``, ``show``, ``send``, ``cancel``, ``takeover`` and
``handback``.

The checks step 18 of the worker-fleet plan names: a step dispatched from the CLI is claimed, its log reaches the hub
and the CLI's stream, the daemon runs the verify commands again, commits and pushes, and approval auto marks the step
done in the plan; a daemon killed with SIGKILL in the middle of a run stops extending its lease
(EVO_HUB_RUN_LEASE_SECONDS is short here), the reaper finds the run lost and a second worker, in another HOME, takes
the next attempt to done; a cancel stops the agent; a takeover moves the agent into tmux and a handback lets it go on
headless (skipped without tmux; the Linux test job of ``.github/workflows/ci.yml`` installs it); a message the owner
sends goes from the run's inbox to the runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

import httpx

from evo_agents.hub import jobs
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import open_pool
from evo_agents.hub.jobs import JobQueue
from evo_agents.worker import interactive
from tests.hub import live
from tests.hub.live import ADMIN, bearer
from tests.hub.test_plans import registration
from tests.hub.test_runs import OWNER, PLAN, PROJECT, WRITER, plan_body
from tests.worker import fake_adapter
from tests.worker.test_daemon import GIT_IDENTITY, TOKEN_LEAK, git, wait_until
from tests.worker.test_interactive import screen, tmux, tmux_socket  # noqa: F401 (a fixture, through request)

ROOT = Path(__file__).parents[2]
REPO = "evo-agents"
BRANCH = "feat/queue"  # the branch plan_body() gives the repo
LEASE_SECONDS = 8  # EVO_HUB_RUN_LEASE_SECONDS of the hub here; the daemons beat every second
WAIT = 120.0
REAPER_EVERY = 2.0  # seconds between the reaper jobs the test defers once a lease may have run out
TMUX = shutil.which("tmux")


def _spawn(command: list[str], env: dict, log_path: Path) -> subprocess.Popen:
    with open(log_path, "ab") as out:
        return subprocess.Popen(command, env=env, stdout=out, stderr=subprocess.STDOUT, cwd=ROOT)


def _stop(proc: subprocess.Popen | None, timeout: float = 30) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class Hub:
    """``hub serve`` and, once ``start_jobs`` is called, ``hub worker``, from this checkout, with a lease of
    LEASE_SECONDS."""

    def __init__(self, db: pg.Database, tmp: Path, github, s3):
        self.db = db
        self.tmp = tmp
        self.port = pg.free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.api_log = tmp / "hub-serve.log"
        self.jobs_log = tmp / "hub-worker.log"
        self.api: subprocess.Popen | None = None
        self.jobs: subprocess.Popen | None = None
        self._reaped_at = 0.0
        self.env = pg.clean_env(
            PYTHONPATH=str(ROOT),
            EVO_HUB_DSN=db.dsn,
            EVO_HUB_DATA_DIR=str(tmp / "hub-cache"),
            EVO_HUB_ADMINS=ADMIN,
            EVO_HUB_GITHUB_CLIENT_ID=github.client_id,
            EVO_HUB_GITHUB_CLIENT_SECRET=github.client_secret,
            EVO_HUB_GITHUB_URL=github.url,
            EVO_HUB_GITHUB_API_URL=github.url,
            EVO_HUB_SESSION_SECRET=live.session_secret(),
            EVO_HUB_PUBLIC_URL="https://hub.test",
            EVO_HUB_RUN_LEASE_SECONDS=str(LEASE_SECONDS),
            **s3.env(),
        )

    def start_api(self) -> None:
        command = [sys.executable, "-m", "evo_agents", "hub", "serve", "--host", "127.0.0.1", "--port", str(self.port)]
        self.api = _spawn(command, self.env, self.api_log)

        def live_now() -> bool:
            assert self.api.poll() is None, f"hub serve exited {self.api.returncode}:\n{self.logs()}"
            try:
                with urllib.request.urlopen(f"{self.url}/v1/health/live", timeout=2) as response:
                    return response.status == 200
            except OSError:
                return False

        wait_until(live_now, "hub serve to answer", 60, self.logs)

    def start_jobs(self) -> None:
        self.jobs = _spawn([sys.executable, "-m", "evo_agents", "hub", "worker"], self.env, self.jobs_log)

        def ready() -> bool:
            assert self.jobs.poll() is None, f"hub worker exited {self.jobs.returncode}:\n{self.logs()}"
            return "worker ready" in self.jobs_log.read_text(encoding="utf-8")

        wait_until(ready, "hub worker to take jobs", 60, self.logs)

    def reap_soon(self) -> None:
        """Queue the reaper's job (hub.recover_runs) now, at most every REAPER_EVERY seconds, rather than wait for
        the minute it runs at; the job worker takes it at once."""
        if time.monotonic() - self._reaped_at < REAPER_EVERY:
            return
        self._reaped_at = time.monotonic()

        async def defer() -> None:
            pool = await open_pool(HubConfig(dsn=self.db.dsn, data_dir=self.tmp, pool_min_size=1, pool_max_size=1))
            try:
                queue = await JobQueue.open(pool)
                await queue.defer(jobs.RECOVER_RUNS, queueing_lock=jobs.RECOVER_RUNS, timestamp=int(time.time()))
            finally:
                await pool.close()

        asyncio.run(defer())

    def logs(self) -> str:
        return "\n".join(
            f"--- {path.name}\n{path.read_text(encoding='utf-8', errors='replace')[-6000:]}"
            for path in (self.api_log, self.jobs_log)
            if path.exists()
        )

    def stop(self) -> None:
        _stop(self.jobs)
        _stop(self.api)


class Machine:
    """A machine of the owner's: a HOME with the owner signed in to the hub, a checkout of evo-agents cloned from the
    origin, the harness registry pointing at it, and, once registered, the worker ``name`` in ~/.evo/worker. The fake
    runtime follows ``scenarios`` (``tests.worker.fake_adapter``)."""

    def __init__(self, world: World, name: str, scenarios: dict, socket: str | None = None):
        self.world = world
        self.name = name
        self.tmp = world.tmp / name
        self.home = self.tmp / "home"
        self.state = self.home / ".evo" / "worker"
        self.checkout = self.home / "ws" / REPO
        self.messages_path = self.tmp / "messages.txt"
        self.starts_path = self.tmp / "starts.jsonl"
        self.daemon_log = self.tmp / "daemon.out"
        self.daemons: list[subprocess.Popen] = []
        bin_dir = self.tmp / "bin"  # git (and tmux) alone on PATH: the runtimes of this machine stay out of the test
        bin_dir.mkdir(parents=True)
        os.symlink(shutil.which("git"), bin_dir / "git")
        scenarios_path = self.tmp / "scenarios.json"
        scenarios_path.write_text(json.dumps(scenarios), encoding="utf-8")
        self.env = pg.clean_env(
            HOME=str(self.home),
            PATH=f"{bin_dir}:/usr/bin:/bin",
            PYTHONPATH=str(ROOT),
            EVO_WORKER_HEARTBEAT_SECONDS="1",
            **GIT_IDENTITY,
            **fake_adapter.environment(scenarios_path, self.messages_path, self.starts_path),
        )
        if socket is not None:
            os.symlink(TMUX, bin_dir / "tmux")
            self.env[interactive.TMUX_SOCKET_VARIABLE] = socket
        self.checkout.parent.mkdir(parents=True)
        git("clone", "--quiet", str(world.origin), str(self.checkout), env={**os.environ, **GIT_IDENTITY})
        hub_dir = self.home / ".evo" / "hub"
        hub_dir.mkdir(parents=True, mode=0o700)
        (hub_dir / "config.json").write_text(json.dumps({"url": world.hub.url, "login": OWNER}), encoding="utf-8")
        (hub_dir / "token").write_text(world.owner_token + "\n", encoding="utf-8")
        for path in hub_dir.iterdir():
            path.chmod(0o600)
        registry = self.home / ".claude" / "harness" / "registry.json"
        registry.parent.mkdir(parents=True)
        cluster = {
            "name": PROJECT,
            "root": str(self.home / "ws" / "evo-agents-harness"),
            "workspace": str(self.home / "ws"),
            "repos": [str(self.checkout)],
            "hub": {"url": world.hub.url, "project": PROJECT},
        }
        registry.write_text(json.dumps({"clusters": [cluster]}), encoding="utf-8")

    # The CLI, as the owner on this machine

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return pg.cli(list(args), self.env)

    def run_cli(self, command: str, *args: str) -> subprocess.CompletedProcess:
        """``evo-agents hub run COMMAND ARGS --project evo-agents``, which must succeed."""
        done = self.cli("hub", "run", command, *args, "--project", PROJECT)
        assert done.returncode == 0, f"hub run {command}: {done.stderr}"
        return done

    def dispatch(self, step: int) -> dict:
        args = (PLAN, str(step), "--runtime", "claude-code", "--approval", "auto", "--json")
        (run,) = json.loads(self.run_cli("dispatch", *args).stdout)
        assert (run["state"], run["approval"], run["dispatched_by"]) == ("queued", "auto", OWNER)
        return run

    def follow(self, run_id: int) -> subprocess.Popen:
        """``hub run logs RUN --follow``: the run's server-sent events until it ends."""
        command = [sys.executable, "-m", "evo_agents", "hub", "run", "logs", str(run_id), "--follow"]
        return subprocess.Popen(
            [*command, "--project", PROJECT], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

    # The worker

    def register(self) -> None:
        done = self.cli("worker", "register", "--name", self.name, "--project", PROJECT)
        assert done.returncode == 0, done.stderr
        self.world.tokens.append((self.state / "token").read_text(encoding="utf-8").strip())

    def start_daemon(self) -> subprocess.Popen:
        proc = _spawn([sys.executable, "-m", "evo_agents", "worker", "run"], self.env, self.daemon_log)
        self.daemons.append(proc)
        before = self.output().count("worker started")
        wait_until(
            lambda: self.output().count("worker started") > before or proc.poll() is not None,
            f"the daemon of {self.name} to start",
            WAIT,
            self.output,
        )
        assert proc.poll() is None, f"the daemon of {self.name} exited {proc.returncode}:\n{self.output()}"
        return proc

    def stop_daemon(self, proc: subprocess.Popen) -> int:
        proc.send_signal(signal.SIGTERM)
        try:
            return proc.wait(WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail(f"the daemon of {self.name} did not stop on SIGTERM:\n{self.output()}")

    def kill_daemons(self) -> None:
        for proc in self.daemons:
            if proc.poll() is None:
                proc.kill()
                proc.wait(30)

    def output(self) -> str:
        return self.daemon_log.read_text(encoding="utf-8", errors="replace") if self.daemon_log.exists() else ""

    def record(self, run_id: int) -> dict:
        return json.loads((self.state / "runs" / str(run_id) / "run.json").read_text(encoding="utf-8"))

    def messages(self) -> str:
        return self.messages_path.read_text(encoding="utf-8") if self.messages_path.exists() else ""

    def finished_cleanly(self, proc: subprocess.Popen) -> None:
        """The daemon stops on SIGTERM with exit status 0, and nothing it wrote holds a token."""
        assert self.stop_daemon(proc) == 0, self.output()
        log_text = (self.state / "worker.log").read_text(encoding="utf-8")
        for line in log_text.splitlines():
            json.loads(line)
        self.world.assert_no_token(log_text, f"the worker.log of {self.name}")
        self.world.assert_no_token(self.output(), f"the daemon output of {self.name}")


class World:
    """The hub with project evo-agents, owner a writer and the plan rollout pushed; the bare origin; the machines."""

    def __init__(self, tmp: Path, hub: Hub, client: httpx.Client, owner_token: str):
        self.tmp = tmp
        self.hub = hub
        self.client = client
        self.owner_token = owner_token
        self.owner = bearer(owner_token)
        self.tokens = [owner_token]
        self.machines: list[Machine] = []
        self.origin = tmp / "origin.git"
        seed = tmp / "seed"
        seed.mkdir()
        env = {**os.environ, **GIT_IDENTITY}
        git("init", "--quiet", "--initial-branch", "main", cwd=seed, env=env)
        (seed / "README.md").write_text("# evo-agents\n", encoding="utf-8")
        git("add", "README.md", cwd=seed, env=env)
        git("commit", "--quiet", "-m", "first commit", cwd=seed, env=env)
        git("clone", "--quiet", "--bare", str(seed), str(self.origin), env=env)
        self.seed_sha = git("rev-parse", "HEAD", cwd=seed)

    def machine(self, name: str, scenarios: dict, socket: str | None = None) -> Machine:
        made = Machine(self, name, scenarios, socket)
        self.machines.append(made)
        return made

    def kill_daemons(self) -> None:
        for machine in self.machines:
            machine.kill_daemons()

    def explain(self) -> str:
        daemons = "\n".join(f"--- daemon of {m.name}\n{m.output()[-5000:]}" for m in self.machines)
        return f"{daemons}\n{self.hub.logs()}"

    # The hub, as the owner

    def run(self, run_id: int) -> dict:
        response = self.client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=self.owner)
        assert response.status_code == 200, response.text
        return response.json()

    def runs_of_step(self, step: int) -> list[dict]:
        params = {"plan_id": PLAN, "step": str(step)}
        response = self.client.get(f"/v1/projects/{PROJECT}/runs", params=params, headers=self.owner)
        assert response.status_code == 200, response.text
        return response.json()["runs"]

    def wait_state(self, run_id: int, *states: str, timeout: float = WAIT, poke=None) -> dict:
        found: dict = {}

        def reached() -> bool:
            if poke is not None:
                poke()
            found.update(self.run(run_id))
            return found["state"] in states

        def explain() -> str:
            return f"run {run_id} is {found.get('state')} ({found.get('error')})\n{self.explain()}"

        wait_until(reached, f"run {run_id} to be {' or '.join(states)}", timeout, explain)
        return found

    def events(self, run_id: int) -> list[dict]:
        path = f"/v1/projects/{PROJECT}/runs/{run_id}/events"
        response = self.client.get(path, params={"limit": 1000}, headers=self.owner)
        assert response.status_code == 200, response.text
        return response.json()["events"]

    def moves(self, run_id: int) -> list[tuple[str, str]]:
        return [(e["body"]["to"], e["body"]["actor"]) for e in self.events(run_id) if e["kind"] == "state"]

    def step(self, key: int) -> dict:
        response = self.client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}", headers=self.owner)
        assert response.status_code == 200, response.text
        return next(item for item in response.json()["body"]["steps"] if item["id"] == key)

    # Git and secrets

    def origin_rev(self, ref: str) -> str | None:
        done = subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
        )
        return done.stdout.strip() or None

    def origin_log(self, count: int) -> list[str]:
        return git("--git-dir", str(self.origin), "log", "--format=%s", f"-{count}", BRANCH).splitlines()

    def assert_no_token(self, text: str, where: str) -> None:
        for token in self.tokens:
            assert token not in text, f"a token is in {where}"
        assert not TOKEN_LEAK.findall(text), f"{where} holds what looks like a token"


@pytest.fixture
def world(hub_db, tmp_path, github, s3):
    with ExitStack() as stack:
        hub = Hub(hub_db, tmp_path, github, s3)
        stack.callback(hub.stop)
        hub.start_api()
        client = stack.enter_context(httpx.Client(base_url=hub.url, timeout=30))
        admin = bearer(live.sign_in(client, github, ADMIN, 901)["token"])
        owner_token = live.sign_in(client, github, OWNER, 902)["token"]
        assert client.put(f"/v1/projects/{PROJECT}", json=registration(), headers=admin).status_code == 200
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=WRITER, headers=admin)
        assert granted.status_code == 200, granted.text
        plan = {"body": plan_body()}
        pushed = client.put(f"/v1/projects/{PROJECT}/plans/{PLAN}", json=plan, headers=bearer(owner_token))
        assert pushed.status_code == 200, pushed.text
        made = World(tmp_path, hub, client, owner_token)
        stack.callback(made.kill_daemons)  # before the hub stops
        yield made
        made.assert_no_token(hub.logs(), "the hub's logs")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def finishing(*, marker: str, text: str = "hello\n") -> list[dict]:
    """A turn that writes and commits feature.txt, leaves notes.txt for the worker to commit and asks for two verify
    commands."""
    return [
        {"samples": True},
        {"write": {"feature.txt": text}},
        {"commit": f"agent: add feature.txt ({marker})"},
        {"write": {"notes.txt": "left for the worker to commit\n"}},
        {
            "result": {
                "verify_commands": ["test -f feature.txt", f"grep -q {text.strip()} feature.txt"],
                "summary": f"Added feature.txt on {marker}.",
            }
        },
    ]


def test_a_step_dispatched_from_the_cli_is_claimed_logged_verified_pushed_and_marked_done(world):
    mac = world.machine("mac-mini", {"2": finishing(marker="mac-mini")})
    mac.register()
    proc = mac.start_daemon()
    run_id = mac.dispatch(2)["id"]
    follow = mac.follow(run_id)
    try:
        out, err = follow.communicate(timeout=WAIT)
    except subprocess.TimeoutExpired:
        follow.kill()
        out, err = follow.communicate()
        pytest.fail(f"hub run logs --follow did not end with the run:\n{out}\n{err}\n{world.explain()}")
    assert follow.returncode == 0, err
    run = world.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], world.explain())

    # The stream the CLI followed carried the agent's words, the verify commands and every move, then ended.
    assert f"Run #{run_id} ended done; its last event is " in out.splitlines()[-1]
    assert "SECOND" in out, "the agent's message, from the runtime's sample events"
    assert "verify: `test -f feature.txt` exited 0" in out
    for move in ("queued -> leased by worker", "leased -> running by worker", "verifying -> done by worker"):
        assert move in out, move

    # The run as `hub run show` prints it: on mac-mini, the commit on origin, both verify commands at 0.
    shown = json.loads(mac.run_cli("show", str(run_id), "--json").stdout)
    assert (shown["worker"], shown["runtime"], shown["attempt"]) == ("mac-mini", "claude-code", 1)
    assert shown["commit_sha"] == world.origin_rev(f"refs/heads/{BRANCH}")
    assert [(item["command"], item["exit_code"]) for item in shown["verify"]] == [
        ("test -f feature.txt", 0),
        ("grep -q hello feature.txt", 0),
    ]
    assert world.origin_log(2) == [f"run #{run_id}: Queue", "agent: add feature.txt (mac-mini)"]
    assert world.origin_rev("refs/heads/main") == world.seed_sha, "main is never pushed"
    uploaded = wait_until(lambda: world.run(run_id)["diff_sha256"] and world.run(run_id), "the run's log and diff")
    assert uploaded["log_sha256"], "the log goes to the blob store at the end"

    # Approval auto: the plan has the step done, with the run's evidence.
    step = world.step(2)
    assert (step["status"], step["done_at"]) == ("done", today())
    assert step["evidence"].startswith(f"run #{run_id} on worker mac-mini (claude-code, attempt 1): {REPO}@")
    assert "Added feature.txt on mac-mini." in step["evidence"]
    assert world.moves(run_id) == [
        ("leased", "worker"),
        ("running", "worker"),
        ("verifying", "worker"),
        ("done", "worker"),
    ]
    mac.finished_cleanly(proc)


def test_a_daemon_killed_mid_run_loses_the_run_to_a_second_worker_in_another_home(world):
    world.hub.start_jobs()
    started = world.tmp / "started-on-mac-mini"
    mac = world.machine("mac-mini", {"2": [{"touch": str(started)}, {"wait_for": str(world.tmp / "never")}]})
    lab = world.machine("lab-02", {"2": finishing(marker="lab-02")})
    mac.register()
    lab.register()
    first = mac.start_daemon()
    run_id = mac.dispatch(2)["id"]
    wait_until(started.exists, "the agent on mac-mini to start", WAIT, world.explain)
    world.wait_state(run_id, "running")
    wait_until(lambda: mac.record(run_id)["state"] == "running", "mac-mini to record the run running", WAIT)

    first.send_signal(signal.SIGKILL)  # no report, no heartbeat: the lease runs out
    assert first.wait(30) == -signal.SIGKILL
    killed_at = time.monotonic()
    second = lab.start_daemon()  # a claim waiting on the hub while the run is held by the dead daemon

    def reap_once_the_lease_may_be_over() -> None:
        if time.monotonic() - killed_at > LEASE_SECONDS:
            world.hub.reap_soon()

    lost = world.wait_state(run_id, "lost", poke=reap_once_the_lease_may_be_over)
    assert time.monotonic() - killed_at > LEASE_SECONDS / 2, "not before the lease ran out"
    assert lost["error"] == "its worker mac-mini stopped extending the lease"
    assert world.moves(run_id) == [("leased", "worker"), ("running", "worker"), ("lost", "reaper")]
    reaped = [line for line in pg.log_lines(world.hub.jobs_log.read_text(encoding="utf-8")) if line.get("lost")]
    assert reaped and reaped[0]["msg"] == "runs recovered", "the job worker's reaper found it"

    # The reaper queued the next attempt of the step, and lab-02's waiting claim took it.
    retries = [run for run in world.runs_of_step(2) if run["parent_run_id"] == run_id]
    assert len(retries) == 1, world.runs_of_step(2)
    retry = world.wait_state(retries[0]["id"], "done", "failed")
    assert retry["state"] == "done", (retry["error"], world.explain())
    assert (retry["worker"], retry["attempt"], retry["approval"]) == ("lab-02", 2, "auto")
    assert retry["commit_sha"] == world.origin_rev(f"refs/heads/{BRANCH}")
    assert world.origin_log(2) == [f"run #{retry['id']}: Queue", "agent: add feature.txt (lab-02)"]
    step = world.step(2)
    assert step["status"] == "done"
    assert step["evidence"].startswith(f"run #{retry['id']} on worker lab-02 (claude-code, attempt 2)")

    # mac-mini comes back: it closes what the killed daemon left, and the run stays lost.
    again = mac.start_daemon()
    wait_until(
        lambda: mac.record(run_id).get("finished_at") is not None,
        "the new daemon to close the run the killed one left",
        WAIT,
        world.explain,
    )
    assert mac.record(run_id)["state"] == "running when the worker stopped"
    assert world.run(run_id)["state"] == "lost"
    mac.finished_cleanly(again)
    lab.finished_cleanly(second)


def test_the_owners_message_goes_from_the_inbox_to_the_runtime_and_a_cancel_stops_it(world):
    started = world.tmp / "started"
    mac = world.machine("mac-mini", {"2": [{"touch": str(started)}, {"wait_for": str(world.tmp / "never")}]})
    mac.register()
    proc = mac.start_daemon()
    run_id = mac.dispatch(2)["id"]
    wait_until(started.exists, "the agent to start", WAIT, world.explain)
    world.wait_state(run_id, "running")

    sent = mac.run_cli("send", str(run_id), "also update the docs")
    assert f"to run #{run_id}" in sent.stdout
    reached = lambda: "also update the docs" in mac.messages()  # noqa: E731
    wait_until(reached, "the message to reach the runtime", WAIT, world.explain)
    events = world.events(run_id)
    asked = next(e for e in events if e["kind"] == "user_message")
    assert (asked["body"]["text"], asked["body"]["from"]) == ("also update the docs", OWNER)
    wait_until(
        lambda: any(
            e["kind"] == "agent_message_chunk" and e["body"]["content"]["text"] == "got: also update the docs"
            for e in world.events(run_id)
        ),
        "the agent's answer to the message in the run's log",
        WAIT,
        world.explain,
    )

    cancelled = mac.run_cli("cancel", str(run_id))
    assert f"Asked the worker of run #{run_id} (running) to stop it" in cancelled.stdout
    run = world.wait_state(run_id, "cancelled")
    assert run["cancel_requested_at"] is not None and run["finished_at"] is not None
    assert world.moves(run_id)[-1] == ("cancelled", "worker")
    step = world.step(2)
    assert step["status"] == "pending" and f"run #{run_id} was cancelled" in step["note"]
    assert world.origin_rev(f"refs/heads/{BRANCH}") is None, "nothing is pushed"
    mac.finished_cleanly(proc)


@pytest.mark.skipif(TMUX is None, reason="tmux is not on PATH")
def test_a_takeover_moves_the_agent_into_tmux_and_a_handback_lets_it_finish_headless(world, request):
    socket = request.getfixturevalue("tmux_socket")  # a tmux server of the test's own
    started = world.tmp / "started"
    scenarios = {
        "2": [{"touch": str(started)}, {"wait_for": str(world.tmp / "never")}],
        "2/resume": [{"write": {"f.txt": "after the handback\n"}}, {"result": {"verify_commands": ["test -f f.txt"]}}],
    }
    mac = world.machine("mac-mini", scenarios, socket=socket)
    mac.register()
    proc = mac.start_daemon()
    run_id = mac.dispatch(2)["id"]
    wait_until(started.exists, "the agent to start", WAIT, world.explain)
    session = world.wait_state(run_id, "running")["session_id"]
    assert session

    mac.run_cli("takeover", str(run_id))
    run = world.wait_state(run_id, "interactive")
    name = f"evo-run-{run_id}"
    assert run["session_id"] == session
    found = {}

    def on_screen() -> bool:
        found["screen"] = screen(socket, name)
        return "fake tui: session" in found["screen"]

    wait_until(on_screen, f"the terminal UI in tmux session {name}", WAIT, lambda: f"{found}\n{world.explain()}")
    assert f"fake tui: session {session} in {name}" in found["screen"], "the UI goes on with the agent's session"
    tmux(socket, "send-keys", "-t", f"={name}:", "typed in tmux", "Enter")

    def printed() -> bool:
        outputs = [e["body"].get("terminal", "") for e in world.events(run_id) if e["kind"] == "output"]
        return any("echo: typed in tmux" in text for text in outputs)

    wait_until(printed, "what the terminal printed in the run's log", WAIT, world.explain)

    mac.run_cli("handback", str(run_id))
    run = world.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], world.explain())
    assert run["session_id"] == session
    assert tmux(socket, "has-session", "-t", f"={name}").returncode != 0, "the handback closed the UI"
    starts = [json.loads(line) for line in mac.starts_path.read_text(encoding="utf-8").splitlines()]
    assert [(start["session"], start["resume"]) for start in starts] == [(session, None), (session, session)]
    assert [to for to, _ in world.moves(run_id)] == ["leased", "running", "interactive", "running", "verifying", "done"]
    assert world.step(2)["status"] == "done"
    mac.finished_cleanly(proc)
