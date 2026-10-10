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

The checks step 12 of the plan-runs-and-decisions plan names, for plan runs: a plan of three steps over two repos, each
cloned from a bare origin of its own, runs in one plan run dispatched with ``hub run plan``, and the hub holds the
evidence of each step; the agent asks a decision, the test answers it through the API and the run goes on in the same
session; a step dispatched on its own while the plan run is active gets 409; a push to a default branch the plan names
comes with a notice; with EVO_HUB_DECISION_WAIT_SECONDS short for the job worker, a run nobody answers is parked, and
the answer resumes it on the same worker in the same session and worktrees; a daemon killed with SIGKILL in the middle
of a plan run leaves its agent, which the next daemon to start stops, and the run, lost once its lease ran out, is
tried again from the branches the first attempt pushed.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

import httpx
from sqlalchemy import func, update

from evo_agents.hub import jobs, runs, tables
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import open_pool
from evo_agents.hub.jobs import JobQueue
from evo_agents.worker import interactive
from tests.hub import live
from tests.hub.live import ADMIN, bearer
from tests.hub.test_plans import registration
from tests.hub.test_runs import OWNER, PLAN, PROJECT, WRITER, plan_body
from tests.worker import fake_adapter
from tests.worker.test_daemon import GIT_IDENTITY, SLEEPER, TOKEN_LEAK, git, group_alive, wait_until
from tests.worker.test_interactive import screen, tmux, tmux_socket  # noqa: F401 (a fixture, through request)

ROOT = Path(__file__).parents[2]
REPO = "evo-agents"
SECOND_REPO = "evo-cli"  # another repo of the project as registration() lists them, for the plan runs
BRANCH = "feat/queue"  # the branch plan_body() gives the repo
TWO_REPOS = "two-repos"  # the plan of the plan runs: three steps over REPO and SECOND_REPO
PLAN_BRANCH = "feat/plan-run"  # its branch for REPO; it names main, the default branch, for SECOND_REPO
DECISION_WAIT_SECONDS = 2  # EVO_HUB_DECISION_WAIT_SECONDS of the job worker in the test of a parked run
ORPHAN_DEALT_WITH = "the agent a previous daemon left was dealt with"  # the line of worker.log on an orphan agent
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
    """A machine of the owner's: a HOME with the owner signed in to the hub, a checkout of each of ``repos`` (evo-agents
    alone by default) cloned from its origin, the harness registry pointing at them, and, once registered, the worker
    ``name`` in ~/.evo/worker. The fake runtime follows ``scenarios`` (``tests.worker.fake_adapter``), and writes what
    each ``cli`` and ``sh`` action did to commands.jsonl."""

    def __init__(
        self, world: World, name: str, scenarios: dict, socket: str | None = None, repos: tuple[str, ...] = (REPO,)
    ):
        self.world = world
        self.name = name
        self.tmp = world.tmp / name
        self.home = self.tmp / "home"
        self.state = self.home / ".evo" / "worker"
        self.checkouts = {repo: self.home / "ws" / repo for repo in repos}
        self.checkout = self.checkouts[REPO]
        self.messages_path = self.tmp / "messages.txt"
        self.starts_path = self.tmp / "starts.jsonl"
        self.commands_path = self.tmp / "commands.jsonl"
        self.scenarios_path = self.tmp / "scenarios.json"
        self.daemon_log = self.tmp / "daemon.out"
        self.daemons: list[subprocess.Popen] = []
        bin_dir = self.tmp / "bin"  # git (and tmux) alone on PATH: the runtimes of this machine stay out of the test
        bin_dir.mkdir(parents=True)
        os.symlink(shutil.which("git"), bin_dir / "git")
        self.scenarios(scenarios)
        self.env = pg.clean_env(
            HOME=str(self.home),
            PATH=f"{bin_dir}:/usr/bin:/bin",
            PYTHONPATH=str(ROOT),
            EVO_WORKER_HEARTBEAT_SECONDS="1",
            **GIT_IDENTITY,
            **fake_adapter.environment(self.scenarios_path, self.messages_path, self.starts_path, self.commands_path),
        )
        if socket is not None:
            os.symlink(TMUX, bin_dir / "tmux")
            self.env[interactive.TMUX_SOCKET_VARIABLE] = socket
        self.checkout.parent.mkdir(parents=True)
        for repo, checkout in self.checkouts.items():
            git("clone", "--quiet", str(world.origins[repo]), str(checkout), env={**os.environ, **GIT_IDENTITY})
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
            "repos": [str(checkout) for checkout in self.checkouts.values()],
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

    def dispatch_plan(self, plan_id: str) -> dict:
        """``hub run plan PLAN``: one plan run of every step not done yet."""
        run = json.loads(self.run_cli("plan", plan_id, "--runtime", "claude-code", "--json").stdout)
        assert (run["kind"], run["state"], run["step_key"], run["dispatched_by"]) == ("plan", "queued", None, OWNER)
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

    def scenarios(self, scenarios: dict) -> None:
        """What the fake runtime does from the next start of an agent on: it reads the file at each start."""
        self.scenarios_path.write_text(json.dumps(scenarios), encoding="utf-8")

    def commands(self) -> list[dict]:
        """The ``cli`` and ``sh`` actions the fake agents ran, with their exit status and output."""
        if not self.commands_path.exists():
            return []
        return [json.loads(line) for line in self.commands_path.read_text(encoding="utf-8").splitlines()]

    def starts(self) -> list[dict]:
        return [json.loads(line) for line in self.starts_path.read_text(encoding="utf-8").splitlines()]

    def agent(self, run_id: int) -> dict | None:
        """runs/<run>/agent.json, once it names the process group of the run's agent."""
        try:
            agent = json.loads((self.state / "runs" / str(run_id) / "agent.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return agent if agent.get("pgid") else None

    def orphan_dealt_with(self, run_id: int) -> dict:
        """The line of worker.log that says what a daemon did with the agent of run ``run_id`` a killed one left."""

        def found() -> dict | None:
            for line in (self.state / "worker.log").read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("msg") == ORPHAN_DEALT_WITH and record["run_id"] == run_id:
                    return record
            return None

        return wait_until(found, f"worker.log to say what became of the agent of run {run_id}", WAIT, self.output)

    def finished_cleanly(self, proc: subprocess.Popen) -> None:
        """The daemon stops on SIGTERM with exit status 0, and nothing it wrote holds a token."""
        assert self.stop_daemon(proc) == 0, self.output()
        log_text = (self.state / "worker.log").read_text(encoding="utf-8")
        for line in log_text.splitlines():
            json.loads(line)
        self.world.assert_no_token(log_text, f"the worker.log of {self.name}")
        self.world.assert_no_token(self.output(), f"the daemon output of {self.name}")


class World:
    """The hub with project evo-agents, owner a writer and the plan rollout pushed; a bare origin of evo-agents and of
    evo-cli, each with one commit on main; the machines."""

    def __init__(self, tmp: Path, hub: Hub, client: httpx.Client, owner_token: str):
        self.tmp = tmp
        self.hub = hub
        self.client = client
        self.owner_token = owner_token
        self.owner = bearer(owner_token)
        self.tokens = [owner_token]
        self.machines: list[Machine] = []
        self.origins: dict[str, Path] = {}
        self.seeds: dict[str, str] = {}
        env = {**os.environ, **GIT_IDENTITY}
        for repo in (REPO, SECOND_REPO):
            seed = tmp / "seed" / repo
            seed.mkdir(parents=True)
            git("init", "--quiet", "--initial-branch", "main", cwd=seed, env=env)
            (seed / "README.md").write_text(f"# {repo}\n", encoding="utf-8")
            git("add", "README.md", cwd=seed, env=env)
            git("commit", "--quiet", "-m", "first commit", cwd=seed, env=env)
            self.origins[repo] = tmp / f"{repo}.git"
            git("clone", "--quiet", "--bare", str(seed), str(self.origins[repo]), env=env)
            self.seeds[repo] = git("rev-parse", "HEAD", cwd=seed)
        self.origin = self.origins[REPO]
        self.seed_sha = self.seeds[REPO]

    def machine(
        self, name: str, scenarios: dict, socket: str | None = None, repos: tuple[str, ...] = (REPO,)
    ) -> Machine:
        made = Machine(self, name, scenarios, socket, repos)
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

    def runs_of_plan(self, plan_id: str) -> list[dict]:
        response = self.client.get(f"/v1/projects/{PROJECT}/runs", params={"plan_id": plan_id}, headers=self.owner)
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

    def step(self, key: int, plan_id: str = PLAN) -> dict:
        response = self.client.get(f"/v1/projects/{PROJECT}/plans/{plan_id}", headers=self.owner)
        assert response.status_code == 200, response.text
        return next(item for item in response.json()["body"]["steps"] if item["id"] == key)

    def steady(self, worker: str) -> None:
        """As if the heartbeats of ``worker`` had come for longer than runs.STEADY_SECONDS, none late: the clock of the
        hub's steady rule, moved in its database rather than waited for. Its daemon's heartbeats, every second, keep
        it so."""
        w = tables.workers
        since = func.now() - timedelta(seconds=runs.STEADY_SECONDS + 1)
        live.sql(self.hub.db, update(w).values(steady_since=since).where(w.c.name == worker))

    def push_plan(self, body: dict) -> None:
        pushed = self.client.put(f"/v1/projects/{PROJECT}/plans/{body['id']}", json={"body": body}, headers=self.owner)
        assert pushed.status_code == 200, pushed.text

    def texts(self, run_id: int) -> list[str]:
        """The text of each event of the run that has one: what the daemon and the hub said of it."""
        return [e["body"]["text"] for e in self.events(run_id) if isinstance(e["body"].get("text"), str)]

    # Decisions and notifications, as the owner

    def decisions(self, run_id: int) -> list[dict]:
        path = f"/v1/projects/{PROJECT}/decisions"
        response = self.client.get(path, params={"run_id": run_id}, headers=self.owner)
        assert response.status_code == 200, response.text
        return response.json()["decisions"]

    def answer(self, decision_id: int, **body) -> dict:
        path = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
        response = self.client.post(path, json=body, headers=self.owner)
        assert response.status_code == 200, response.text
        return response.json()

    def notifications(self, kind: str) -> list[dict]:
        response = self.client.get("/v1/me/notifications", params={"kind": kind}, headers=self.owner)
        assert response.status_code == 200, response.text
        return response.json()["notifications"]

    # Git and secrets

    def origin_rev(self, ref: str, repo: str = REPO) -> str | None:
        done = subprocess.run(
            ["git", "--git-dir", str(self.origins[repo]), "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
        )
        return done.stdout.strip() or None

    def origin_log(self, count: int, branch: str = BRANCH, repo: str = REPO) -> list[str]:
        return git("--git-dir", str(self.origins[repo]), "log", "--format=%s", f"-{count}", branch).splitlines()

    def origin_files(self, branch: str, repo: str = REPO) -> list[str]:
        return git("--git-dir", str(self.origins[repo]), "ls-tree", "-r", "--name-only", branch).split()

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
    return datetime.now(UTC).date().isoformat()


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


# Plan runs


def two_repos_plan() -> dict:
    """Three steps over evo-agents (on PLAN_BRANCH) and evo-cli (on main, its default branch, which the plan names)."""
    return {
        "id": TWO_REPOS,
        "title": "Two repos",
        "goal": "Three steps over two repos, in one plan run.",
        "repos": [{"repo": REPO, "branch": PLAN_BRANCH}, {"repo": SECOND_REPO, "branch": "main"}],
        "steps": [
            {"id": 1, "title": "One", "repo": REPO, "what": "write one.txt", "status": "pending"},
            {
                "id": 2,
                "title": "Two",
                "repo": SECOND_REPO,
                "what": "write two.txt on main",
                "depends_on": [1],
                "status": "pending",
            },
            {
                "id": 3,
                "title": "Three",
                "repo": REPO,
                "what": "write three.txt",
                "depends_on": [2],
                "status": "pending",
            },
        ],
    }


PLAN_FILES = {1: (REPO, "one.txt"), 2: (SECOND_REPO, "two.txt"), 3: (REPO, "three.txt")}


def doing(key: int) -> list[dict]:
    """The agent's work on step ``key`` of two_repos_plan(): write its file in the step's worktree, then report the
    step done through ``evo-agents worker step`` with a verify command, which commits and pushes it."""
    repo, name = PLAN_FILES[key]
    word = name.removesuffix(".txt")
    return [
        {"write": {f"{repo}/{name}": f"{word}\n"}},
        {
            "cli": [
                "step",
                str(key),
                "done",
                "--evidence",
                f"Wrote {name}.",
                "--verify",
                f"test -f {name}",
                "--verify",
                f"grep -q {word} {name}",
            ]
        },
    ]


def asking(step: int) -> dict:
    """``evo-agents worker ask``: a deploy decision of step ``step``, two options, no recommended."""
    return {
        "cli": [
            "ask",
            "--category",
            "deploy",
            "--question",
            "Deploy evo-cli from main now?",
            "--option",
            "yes=Deploy:to staging only",
            "--option",
            "no=Wait",
            "--recommended",
            "no",
            "--step",
            str(step),
        ]
    }


def assert_step_done(world: World, key: int, run_id: int, attempt: int = 1) -> None:
    """Step ``key`` of two_repos_plan() is done on the hub, with the evidence of run ``run_id``: its commit on origin,
    both verify commands at 0 and the agent's words."""
    repo, name = PLAN_FILES[key]
    branch = PLAN_BRANCH if repo == REPO else "main"
    step = world.step(key, TWO_REPOS)
    assert (step["status"], step["done_at"]) == ("done", today()), step
    head = f"run #{run_id} on worker mac-mini (claude-code, attempt {attempt}): {repo}@"
    assert step["evidence"].startswith(head), step["evidence"]
    assert f" on {branch}." in step["evidence"].splitlines()[0]
    assert f"`test -f {name}` exit 0" in step["evidence"] and step["evidence"].endswith(f"Wrote {name}.")


def test_a_plan_run_does_three_steps_over_two_repos_and_goes_on_once_the_owner_answers_its_decision(world):
    world.push_plan(two_repos_plan())
    scenarios = {
        f"plan:{TWO_REPOS}": [
            {"sh": "pwd -P; echo $EVO_RUN_ID $EVO_RUN_KIND; ls"},
            {"cli": ["step", "1", "in_progress"]},
            *doing(1),
            asking(2),
        ],
        f"plan:{TWO_REPOS}/2": [*doing(2), *doing(3), {"result": {"summary": "Three steps over two repos."}}],
    }
    mac = world.machine("mac-mini", scenarios, repos=(REPO, SECOND_REPO))
    mac.register()
    proc = mac.start_daemon()
    run_id = mac.dispatch_plan(TWO_REPOS)["id"]

    # The agent did step 1, asked, and ended its turn: the run waits, its worker keeps it and its lease.
    run = world.wait_state(run_id, "waiting", "done", "failed")
    assert run["state"] == "waiting", (run["error"], world.explain())
    assert run["waiting_since"] is not None and run["lease_expires_at"] is not None and run["worker"] == "mac-mini"
    assert [item["repo"] for item in run["repos"]] == [REPO, SECOND_REPO]
    looked = mac.commands()[0]["stdout"].splitlines()
    directory = Path(looked[0])
    assert looked[1] == f"{run_id} plan" and {REPO, SECOND_REPO} <= set(looked[2:]), looked
    assert_step_done(world, 1, run_id)
    assert world.origin_log(2, PLAN_BRANCH) == [f"run #{run_id} step 1: One", "first commit"]
    assert world.step(2, TWO_REPOS)["status"] == "pending"

    # A step dispatched on its own while the plan run is active gets 409, and nothing is queued.
    alone = {"plan_id": TWO_REPOS, "steps": [2], "runtime": "claude-code"}
    refused = world.client.post(f"/v1/projects/{PROJECT}/runs", json=alone, headers=world.owner)
    assert refused.status_code == 409, refused.text
    assert f"plan {TWO_REPOS} has plan run #{run_id}, waiting" in refused.json()["message"]
    assert [item["id"] for item in world.runs_of_plan(TWO_REPOS)] == [run_id]

    # The owner has the decision in the notifications, and answers it through the API.
    (decision,) = world.decisions(run_id)
    assert (decision["state"], decision["category"], decision["step_key"]) == ("open", "deploy", "2")
    assert [option["key"] for option in decision["options"]] == ["yes", "no"] and decision["recommended"] == "no"
    asked = [item for item in world.notifications("decision") if item["decision_id"] == decision["id"]]
    assert len(asked) == 1 and asked[0]["run_id"] == run_id and asked[0]["decision_state"] == "open"
    time.sleep(2)  # a few heartbeats: the run keeps waiting, and no agent starts
    assert world.run(run_id)["state"] == "waiting" and len(mac.starts()) == 1
    answered = world.answer(decision["id"], option="yes", text="Staging only, as the option says.")
    assert (answered["state"], answered["answer_option"], answered["answer_run_id"]) == ("answered", "yes", run_id)

    run = world.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], world.explain())
    assert world.moves(run_id) == [
        ("leased", "worker"),
        ("running", "worker"),
        ("waiting", "worker"),
        ("running", "worker"),
        ("verifying", "worker"),
        ("done", "worker"),
    ]

    # The answer went to the agent in the same session and directory.
    first, again = mac.starts()
    assert again["run"] == run_id and again["resume"] == first["session"]
    assert Path(again["cwd"]).resolve() == Path(first["cwd"]).resolve() == directory
    assert f"Answer to decision #{decision['id']} (deploy)" in again["prompt"]
    assert "Chosen option: yes, Deploy." in again["prompt"] and "Staging only, as the option says." in again["prompt"]
    assert world.decisions(run_id)[0]["delivered_at"] is not None, "acknowledged once the agent had it"

    # Every step is done with the evidence of the run, each repo's branch on origin holds its steps, in order.
    for key in (1, 2, 3):
        assert_step_done(world, key, run_id)
    assert world.origin_log(3, PLAN_BRANCH) == [
        f"run #{run_id} step 3: Three",
        f"run #{run_id} step 1: One",
        "first commit",
    ]
    assert world.origin_log(2, "main", SECOND_REPO) == [f"run #{run_id} step 2: Two", "first commit"]
    assert world.origin_rev("refs/heads/main") == world.seeds[REPO], "main of evo-agents is never pushed"
    assert {"one.txt", "three.txt"} <= set(world.origin_files(PLAN_BRANCH))
    assert "two.txt" in world.origin_files("main", SECOND_REPO)
    for command in mac.commands()[1:]:
        assert command["exit"] == 0, command

    # The push to main of evo-cli came with a notice, and the end of the plan with another.
    notices = {item["notice_kind"]: item for item in world.notifications("notice") if item["run_id"] == run_id}
    assert set(notices) == {"push_default_branch", "plan_finished"}, notices
    pushed = notices["push_default_branch"]
    main = world.origin_rev("refs/heads/main", SECOND_REPO)
    assert pushed["details"] == {"repo": SECOND_REPO, "branch": "main", "commits": [main]}
    assert "Notified the run's owner of the push to main" in mac.commands()[4]["stdout"]
    assert notices["plan_finished"]["title"] == f"Plan {TWO_REPOS} finished"
    mac.finished_cleanly(proc)


def test_a_plan_run_nobody_answers_in_time_is_parked_and_resumed_on_the_same_worker_in_its_session_and_worktrees(
    world,
):
    world.hub.env["EVO_HUB_DECISION_WAIT_SECONDS"] = str(DECISION_WAIT_SECONDS)  # the reaper's, in the job worker
    world.hub.start_jobs()
    world.push_plan(two_repos_plan())
    scenarios = {
        f"plan:{TWO_REPOS}": [
            *doing(1),
            {"write": {f"{REPO}/draft.txt": "left in the worktree while parked\n"}},
            asking(2),
        ],
        f"plan:{TWO_REPOS}/resume": [
            {"sh": f"pwd -P; echo $EVO_RUN_ID; cat {REPO}/draft.txt"},
            *doing(2),
            *doing(3),
            {"result": {"summary": "Went on after the park."}},
        ],
    }
    mac = world.machine("mac-mini", scenarios, repos=(REPO, SECOND_REPO))
    mac.register()
    proc = mac.start_daemon()
    run_id = mac.dispatch_plan(TWO_REPOS)["id"]
    run = world.wait_state(run_id, "waiting", "done", "failed")
    assert run["state"] == "waiting", (run["error"], world.explain())
    session = run["session_id"]
    assert session

    # Nobody answers within EVO_HUB_DECISION_WAIT_SECONDS: the job worker's reaper parks the run, and the worker lets
    # go of it at its next heartbeat, with the session and the worktrees kept.
    parked = world.wait_state(run_id, "parked", poke=world.hub.reap_soon)
    assert (parked["lease_expires_at"], parked["waiting_since"]) == (None, None) and parked["parked_at"] is not None
    assert world.moves(run_id) == [
        ("leased", "worker"),
        ("running", "worker"),
        ("waiting", "worker"),
        ("parked", "reaper"),
    ]
    reasons = [e["body"].get("reason") for e in world.events(run_id) if e["kind"] == "state"]
    assert reasons[-1] == f"nobody answered its decision within {DECISION_WAIT_SECONDS} seconds"
    wait_until(lambda: mac.record(run_id)["state"] == "parked", "the worker to park the run", WAIT, world.explain)
    record = mac.record(run_id)
    directory = Path(record["dir"])
    assert record["session_id"] == session and record["parked_at"]
    assert (directory / REPO / "draft.txt").is_file(), "the worktrees stay as the agent left them"
    assert git("symbolic-ref", "--short", "HEAD", cwd=directory / REPO) == PLAN_BRANCH
    assert len(mac.starts()) == 1
    wait_until(
        lambda: any("The hub parked the run" in text for text in world.texts(run_id)),
        "the worker to say it parked the run",
        WAIT,
        world.explain,
    )

    # The answer resumes it: a new plan run pinned to the same worker, which goes on in the session and the worktrees.
    (decision,) = world.decisions(run_id)
    answered = world.answer(decision["id"], option="yes")
    resumed_id = answered["answer_run_id"]
    assert answered["state"] == "answered" and resumed_id not in (None, run_id)
    resumed = world.wait_state(resumed_id, "done", "failed")
    assert resumed["state"] == "done", (resumed["error"], world.explain())
    assert (resumed["kind"], resumed["resume_of_run_id"], resumed["worker"]) == ("plan", run_id, "mac-mini")
    assert resumed["pinned_worker_id"] == parked["worker_id"] == resumed["worker_id"]
    assert resumed["session_id"] == session
    assert world.moves(resumed_id) == [
        ("leased", "worker"),
        ("running", "worker"),
        ("verifying", "worker"),
        ("done", "worker"),
    ]
    old = world.run(run_id)
    assert old["state"] == "done" and world.moves(run_id)[-1] == ("done", "owner")

    first, again = mac.starts()
    assert (again["run"], again["session"], again["resume"]) == (resumed_id, session, session)
    assert Path(again["cwd"]).resolve() == Path(first["cwd"]).resolve() == directory.resolve()
    assert f"goes on now as run #{resumed_id}" in again["prompt"]
    assert f"Answer to decision #{decision['id']} (deploy)" in again["prompt"]
    looked = next(item for item in mac.commands() if item["run"] == resumed_id)
    assert looked["stdout"].splitlines() == [
        str(directory.resolve()),
        str(resumed_id),
        "left in the worktree while parked",
    ]
    assert any(f"Goes on from parked run #{run_id}" in text for text in world.texts(resumed_id))

    # Step 1 is the parked run's, steps 2 and 3 the resumed run's; the draft went out with step 3.
    assert_step_done(world, 1, run_id)
    assert_step_done(world, 2, resumed_id)
    assert_step_done(world, 3, resumed_id)
    assert world.origin_log(3, PLAN_BRANCH) == [
        f"run #{resumed_id} step 3: Three",
        f"run #{run_id} step 1: One",
        "first commit",
    ]
    assert {"one.txt", "draft.txt", "three.txt"} <= set(world.origin_files(PLAN_BRANCH))
    assert world.origin_log(2, "main", SECOND_REPO) == [f"run #{resumed_id} step 2: Two", "first commit"]
    mac.finished_cleanly(proc)


def test_a_daemon_killed_mid_plan_run_leaves_an_agent_the_next_start_stops_and_the_lost_run_goes_on_from_its_pushes(
    world,
):
    world.hub.start_jobs()
    world.push_plan(two_repos_plan())
    started = world.tmp / "started"
    first = [
        *doing(1),
        {"cli": ["step", "2", "in_progress"]},
        {"spawn": SLEEPER},
        {"touch": str(started)},
        {"wait_for": str(world.tmp / "never")},
    ]
    mac = world.machine("mac-mini", {f"plan:{TWO_REPOS}": first}, repos=(REPO, SECOND_REPO))
    mac.register()
    killed = mac.start_daemon()
    run_id = mac.dispatch_plan(TWO_REPOS)["id"]
    wait_until(started.exists, "the agent to start its process and wait", WAIT, world.explain)
    agent = wait_until(lambda: mac.agent(run_id), "agent.json to name the agent's process group", WAIT, world.explain)
    pgid = agent["pgid"]
    pushed = world.origin_rev(f"refs/heads/{PLAN_BRANCH}")
    assert pushed is not None and world.origin_log(1, PLAN_BRANCH) == [f"run #{run_id} step 1: One"]
    assert world.step(2, TWO_REPOS)["status"] == "in_progress"

    killed.send_signal(signal.SIGKILL)  # no report, no heartbeat; the agent's process group lives on
    assert killed.wait(30) == -signal.SIGKILL
    time.sleep(0.5)
    assert group_alive(pgid), "the agent outlives a daemon killed with SIGKILL"
    killed_at = time.monotonic()

    # The next start of the daemon stops that agent at once, while the hub still holds the run for this worker.
    mac.scenarios(
        {
            f"plan:{TWO_REPOS}": [
                {"sh": f"cat {REPO}/one.txt; git -C {REPO} log --format=%s -1; test -e {REPO}/two.txt || echo none"},
                *doing(2),
                *doing(3),
                {"result": {"summary": "The next attempt did steps 2 and 3."}},
            ]
        }
    )
    again = mac.start_daemon()
    wait_until(lambda: not group_alive(pgid), "the next daemon to stop the agent the killed one left", WAIT, mac.output)
    record = mac.orphan_dealt_with(run_id)
    assert record["agent"] == f"process group {pgid} stopped on SIGTERM", record
    assert record["hub_state"] == "running" and record["worktree"].startswith("kept: the hub still holds the run")
    # The hub gives the next attempt back to mac-mini, which lost the run, once its heartbeats are steady.
    world.steady("mac-mini")

    # Nobody extends the run's lease: the reaper finds it lost and queues the next attempt of the plan run.
    def reap_once_the_lease_may_be_over() -> None:
        if time.monotonic() - killed_at > LEASE_SECONDS:
            world.hub.reap_soon()

    lost = world.wait_state(run_id, "lost", poke=reap_once_the_lease_may_be_over)
    assert lost["error"] == "its worker mac-mini stopped extending the lease"
    assert world.moves(run_id) == [("leased", "worker"), ("running", "worker"), ("lost", "reaper")]
    (retry,) = [item for item in world.runs_of_plan(TWO_REPOS) if item["parent_run_id"] == run_id]
    retry = world.wait_state(retry["id"], "done", "failed")
    assert retry["state"] == "done", (retry["error"], world.explain())
    assert (retry["kind"], retry["attempt"], retry["worker"]) == ("plan", 2, "mac-mini")

    # The next attempt started from the branch the first one pushed: step 1's commit and file, nothing of step 2.
    looked = next(item for item in mac.commands() if item["run"] == retry["id"])
    assert looked["stdout"].splitlines() == ["one", f"run #{run_id} step 1: One", "none"]
    started_from = f" at {pushed[:12]} (refs/remotes/origin/{PLAN_BRANCH})"
    assert any(text.startswith("Worktree ") and started_from in text for text in world.texts(retry["id"]))
    assert_step_done(world, 1, run_id)
    assert_step_done(world, 2, retry["id"], attempt=2)
    assert_step_done(world, 3, retry["id"], attempt=2)
    assert world.origin_log(3, PLAN_BRANCH) == [
        f"run #{retry['id']} step 3: Three",
        f"run #{run_id} step 1: One",
        "first commit",
    ]
    assert world.origin_log(2, "main", SECOND_REPO) == [f"run #{retry['id']} step 2: Two", "first commit"]
    mac.finished_cleanly(again)
