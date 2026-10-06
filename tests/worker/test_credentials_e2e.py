"""A worker that holds no credential of its owner, end to end: ``evo-agents hub serve`` and ``evo-agents hub worker``
(whose reaper finds runs lost) in processes of their own on a database of the test's own, with the fake GitHub of the
hub's tests playing the hub's GitHub App, an ``EVO_HUB_SECRETS_KEY`` drawn for the test, and ``git http-backend`` on
127.0.0.1 behind Basic auth (``tests.worker.git_http``) as the origin of the run's repo.

The worker is a temporary HOME with no ``~/.evo/hub`` (no machine token), no gh configuration and no ``~/.ssh``: it
joins with a pairing code its owner made, so the only credential it keeps is its worker token. Its environment is
built from nothing, so no SSH agent, GH_TOKEN or API key of the machine running pytest reaches it; its git
configuration names a credential helper of the machine's own that notes each call, and the system's is not read
(GIT_CONFIG_NOSYSTEM). The daemon is the real ``evo-agents worker run``, whose runtime is the fake adapter of
``tests.worker.fake_adapter``. The owner works through the hub's API with a machine token this test holds and the
machine never sees.

The checks step 14 of the worker-credentials plan names. The owner sets an env secret (a CLAUDE_CODE_OAUTH_TOKEN of
their own drawing) and a git secret for the git http-backend, both bound to the worker, and dispatches a step whose
agent writes the environment it got and runs ``git push``: the env lease is in the agent's environment, the push and
the daemon's own fetch and push reach the origin with the git lease, and the machine's helper is never called. The
agent reaches the hub's MCP through ``evo-agents hub mcp`` with the worker's token: kg_search answers from the graph
of the run's project, and another project the owner writes in is refused, as is a session opened on it. Once the run
is over the daemon gave its leases back (DELETE on the lease route), every lease is revoked and the audit holds
credential.lease and credential.revoke for the run. A second step, on a repo whose origin is on github.com, gets a
token of the hub's App, and the fake GitHub receives DELETE /installation/token for it when the run ends. Throughout,
no file under the temporary HOME holds a secret's value, a GitHub token or the owner's token, and neither do the
daemon's output nor the hub's logs. A daemon killed with SIGKILL in the middle of a run cannot give its leases back:
the reaper finds the run lost, revokes its leases and revokes its GitHub token at the fake.

A run of one step gets the leases of its own repo only, so the git secret and the App's token come in two runs:
evo-agents, whose origin is the git http-backend, and evo-cli, whose origin on the hub is on github.com, where the
fake's App is installed. No test reaches github.com, so the checkout of evo-cli on the worker fetches from and pushes
to a bare repository on disk; its run gets a token it never uses, which is what the hub must revoke all the same.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.hub import live
from tests.hub.live import ADMIN, bearer
from tests.hub.test_plans import registration as plans_registration
from tests.hub.test_runs import OWNER, PROJECT, WRITER
from tests.worker import fake_adapter
from tests.worker.git_http import GitHttp
from tests.worker.test_daemon import GIT_IDENTITY, TOKEN_LEAK, git, wait_until
from tests.worker.test_end_to_end import LEASE_SECONDS, Hub

ROOT = Path(__file__).parents[2]
PLAN = "leased-credentials"
REPO = "evo-agents"  # its origin is the git http-backend, which a git secret of the owner covers
APP_REPO = "evo-cli"  # its origin on the hub is on github.com, where the fake GitHub's App is installed
GITHUB_OWNER = "maycuatroi1"
BRANCHES = {REPO: "feat/leased", APP_REPO: "feat/app-token"}
STEPS = {REPO: 1, APP_REPO: 2}
OTHER_PROJECT = "evo-lms"  # a project the owner writes in too; the agent of a run of PROJECT reaches none of it
OTHER_PLAN = "lms-rollout"
WORKER = "mac-mini"
GIT_USER = "oauth2"  # the user the git secret goes with, as GitLab takes a project access token
AGENT_PUSH = "agent/leased"  # the branch the agent pushes itself
KG_QUERY = "leases"
WAIT = 120.0
MCP_HEADERS = {"Accept": "application/json", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
HELLO = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fake-agent", "version": "1"}}


def sample(prefix: str) -> str:
    """A value drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


def setup_env(home: Path) -> dict[str, str]:
    """The environment of the test's own git: a HOME of its own and no system configuration, so nothing reads the
    configuration of the machine running pytest."""
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        **GIT_IDENTITY,
    }


def app_key() -> tuple[str, str]:
    """A key pair for the GitHub App: the private PEM the hub signs with, the public one the fake checks."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public.decode()


def registration(project: str, origins: dict[str, str | None]) -> dict:
    body = plans_registration()
    body["harness"] = {**body["harness"], "name": project, "path": f"{project}-harness"}
    body["repos"] = [
        {"name": name, "path": name, **({"origin": origin} if origin else {})} for name, origin in origins.items()
    ]
    return body


def plan_body() -> dict:
    return {
        "id": PLAN,
        "goal": "Push with what the hub leases, and nothing the machine holds.",
        "repos": [{"repo": repo, "branch": branch, "status": "pending"} for repo, branch in BRANCHES.items()],
        "steps": [
            {"id": key, "title": f"Push {repo}", "repo": repo, "what": f"a commit on {repo}", "status": "pending"}
            for repo, key in STEPS.items()
        ],
    }


def other_plan() -> dict:
    return {
        "id": OTHER_PLAN,
        "goal": "A plan of the other project.",
        "repos": [{"repo": OTHER_PROJECT, "branch": "feat/other", "status": "pending"}],
        "steps": [{"id": 1, "title": "Other", "repo": OTHER_PROJECT, "what": "other work", "status": "pending"}],
    }


# The graph of the run's project, built on a laptop and installed as the hub's latest build

KNOWLEDGE = """\
version: 1
project: {project}
policy:
  levels: [public, internal, customer, secret]
  sinks:
    - {{id: claude-code@anthropic, kind: agent-session, clearance: {{level: internal}}}}
    - {{id: hub, kind: hub, clearance: {{level: internal}}}}
sources:
  - id: docs
    connector: "python:tests.kg.fakes:run"
    config: {{file: "{docs}"}}
    label: {{level: internal, integrity: U}}
"""


def install_graph(db: pg.Database, cache: Path, root: Path, monkeypatch) -> None:
    """A graph of PROJECT built on a laptop under ``root``, put where ``hub serve`` keeps the graphs it fetched
    (``cache``, its EVO_HUB_DATA_DIR) and recorded as the project's latest successful build."""
    import hashlib

    from evo_agents.hub.kg_build import build_graph
    from evo_agents.kg.project import load_project_at
    from evo_agents.kg.sync import sync_project

    harness, home = root / f"{PROJECT}-harness", root / "kg-home"
    harness.mkdir(parents=True)
    docs = root / "docs.json"
    items = [
        {"key": KG_QUERY, "text": f"# {KG_QUERY}\n\nHow a run gets its credentials from the hub.\n", "rev": "1"},
        {"key": "daemon", "text": "# daemon\n\nWhat the worker daemon does with a run.\n", "rev": "1"},
    ]
    docs.write_text(json.dumps({"items": items}), encoding="utf-8")
    (harness / "harness.yaml").write_text(f"name: {PROJECT}\nrepos: []\nknowledge_file: knowledge.yaml\n")
    (harness / "knowledge.yaml").write_text(KNOWLEDGE.format(project=PROJECT, docs=docs))
    monkeypatch.setenv("EVO_KG_HOME", str(home))
    project = load_project_at(harness, home)
    assert all(result.ok for result in sync_project(project, ["docs"]))
    path, report = build_graph(project)
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    target = cache / "kg" / "graphs" / PROJECT / f"{sha256}.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    live.sql(
        db,
        "INSERT INTO kg_builds (project_id, status, artifact_sha256, artifact_size, content_hash, nodes, edges, "
        "started_at, finished_at) SELECT id, 'succeeded', %s, %s, %s, %s, %s, now(), now() FROM projects "
        "WHERE name = %s",
        (sha256, len(data), report.content_hash, report.nodes, report.edges, PROJECT),
    )


# The worker machine


class Machine:
    """The worker: a HOME with no machine token, no gh and no SSH key, a checkout of each repo, the harness registry
    pointing at them, and a git configuration whose credential helper is the machine's own and notes each call in
    ``helper_calls``. The fake runtime follows ``scenarios``; what each ``cli`` and ``sh`` action did goes to
    commands.jsonl. Nothing under ``tmp`` but ``home`` is the machine's: the test's files go there."""

    def __init__(self, world: World, name: str, scenarios: dict):
        self.world = world
        self.name = name
        self.tmp = world.tmp / name
        self.home = self.tmp / "home"
        self.state = self.home / ".evo" / "worker"
        self.checkouts = {repo: self.home / "ws" / repo for repo in (REPO, APP_REPO)}
        self.scenarios_path = self.tmp / "scenarios.json"
        self.commands_path = self.tmp / "commands.jsonl"
        self.starts_path = self.tmp / "starts.jsonl"
        self.daemon_log = self.tmp / "daemon.out"
        self.helper_calls = self.tmp / "machine-helper-calls"
        self.daemons: list[subprocess.Popen] = []
        self.token = ""
        bin_dir = self.tmp / "bin"  # git alone on PATH: no gh, no ssh, no runtime of this machine
        bin_dir.mkdir(parents=True)
        os.symlink(shutil.which("git"), bin_dir / "git")
        self.home.mkdir()
        helper = self.tmp / "machine-helper.sh"
        helper.write_text(f'#!/bin/sh\necho "$1" >> {self.helper_calls}\ncat > /dev/null\n', encoding="utf-8")
        helper.chmod(0o755)
        (self.home / ".gitconfig").write_text(f"[credential]\n\thelper = !{helper}\n", encoding="utf-8")
        self.scenarios_path.write_text(json.dumps(scenarios), encoding="utf-8")
        self.env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(self.home),
            "PYTHONPATH": str(ROOT),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "EVO_WORKER_HEARTBEAT_SECONDS": "1",
            **GIT_IDENTITY,
            **fake_adapter.environment(self.scenarios_path, starts=self.starts_path, cli=self.commands_path),
        }
        local = setup_env(self.home)
        for repo, checkout in self.checkouts.items():
            git("clone", "--quiet", str(world.bare[repo]), str(checkout), env=local)
        # the origin of evo-agents is the git http-backend; it was cloned from its directory, with no credential
        git("remote", "set-url", "origin", world.origins[REPO], cwd=self.checkouts[REPO], env=local)
        registry = self.home / ".claude" / "harness" / "registry.json"
        registry.parent.mkdir(parents=True)
        cluster = {
            "name": PROJECT,
            "root": str(self.home / "ws" / f"{PROJECT}-harness"),
            "workspace": str(self.home / "ws"),
            "repos": [str(checkout) for checkout in self.checkouts.values()],
            "hub": {"url": world.hub.url, "project": PROJECT},
        }
        registry.write_text(json.dumps({"clusters": [cluster]}), encoding="utf-8")

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return pg.cli(list(args), self.env)

    def join(self) -> None:
        """Join with a pairing code the owner made: no sign-in on this machine."""
        body = {"name": self.name, "projects": [PROJECT], "slots": 1}
        pairing = self.world.client.post("/v1/workers/pairings", json=body, headers=self.world.owner)
        assert pairing.status_code == 201, pairing.text
        joined = self.cli("worker", "join", "--url", self.world.hub.url, "--code", pairing.json()["code"])
        assert joined.returncode == 0, joined.stderr
        self.token = (self.state / "token").read_text(encoding="utf-8").strip()
        assert self.token.startswith("evw_")

    def start_daemon(self) -> subprocess.Popen:
        with open(self.daemon_log, "ab") as out:
            command = [sys.executable, "-m", "evo_agents", "worker", "run"]
            proc = subprocess.Popen(command, env=self.env, stdout=out, stderr=subprocess.STDOUT, cwd=ROOT)
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

    def commands(self) -> list[dict]:
        if not self.commands_path.exists():
            return []
        return [json.loads(line) for line in self.commands_path.read_text(encoding="utf-8").splitlines()]

    def leased(self, run_id: int) -> list[str]:
        """The variables the daemon told the runtime the leases of run ``run_id`` set, at its agent's start."""
        starts = [json.loads(line) for line in self.starts_path.read_text(encoding="utf-8").splitlines()]
        (start,) = [start for start in starts if start["run"] == run_id]
        return start["leased"]

    def files_holding(self, values: list[str]) -> list[Path]:
        """The files under HOME that hold one of ``values``; a file the daemon removes meanwhile holds none."""
        found = []
        for path in self.home.rglob("*"):
            with contextlib.suppress(OSError):
                if path.is_file() and not path.is_symlink():
                    data = path.read_bytes()
                    if any(value.encode() in data for value in values):
                        found.append(path)
        return found


# The hub and the owner


class World:
    """The hub with the secrets key and the GitHub App, the git http-backend, project evo-agents (evo-agents behind
    the git http-backend, evo-cli on github.com) and project evo-lms, the owner a writer of both, the plan of each
    pushed, a graph of evo-agents, and the App installed on maycuatroi1/evo-cli; the worker, once ``machine`` made
    it."""

    def __init__(self, tmp: Path, hub: Hub, client: httpx.Client, github, git_http: GitHttp, tokens: dict):
        self.tmp = tmp
        self.hub = hub
        self.client = client
        self.github = github
        self.git_http = git_http
        self.admin = bearer(tokens["admin"])
        self.owner = bearer(tokens["owner"])
        self.owner_tokens = list(tokens.values())
        self.oauth = sample("sk-ant-oat01-")  # the owner's CLAUDE_CODE_OAUTH_TOKEN
        self.glpat = git_http.password  # the owner's token for the git http-backend
        self.machines: list[Machine] = []
        self.bare = {REPO: git_http.root / f"{REPO}.git", APP_REPO: tmp / f"{APP_REPO}.git"}
        self.origins = {
            REPO: git_http.repo_url(f"{REPO}.git"),
            APP_REPO: f"https://github.com/{GITHUB_OWNER}/{APP_REPO}.git",
        }
        env = setup_env(tmp)
        for repo, bare in self.bare.items():
            seed = tmp / "seed" / repo
            seed.mkdir(parents=True)
            git("init", "--quiet", "--initial-branch", "main", cwd=seed, env=env)
            (seed / "README.md").write_text(f"# {repo}\n", encoding="utf-8")
            git("add", "README.md", cwd=seed, env=env)
            git("commit", "--quiet", "-m", "first commit", cwd=seed, env=env)
            git("clone", "--quiet", "--bare", str(seed), str(bare), env=env)

    def machine(self, name: str, scenarios: dict) -> Machine:
        made = Machine(self, name, scenarios)
        self.machines.append(made)
        return made

    def kill_daemons(self) -> None:
        for machine in self.machines:
            machine.kill_daemons()

    def explain(self) -> str:
        daemons = "\n".join(f"--- daemon of {m.name}\n{m.output()[-5000:]}" for m in self.machines)
        served = "\n".join(f"{verb} {path} {user} {status}" for verb, path, user, status in self.git_http.requests)
        return f"{daemons}\n--- git http-backend\n{served}\n{self.hub.logs()}"

    def values(self) -> list[str]:
        """Every value no file of the worker's HOME, no log and no event may hold: the owner's secrets, the owner's and
        the admin's tokens, and what the fake GitHub handed out or holds, the tokens its App made among them."""
        return [self.oauth, self.glpat, *self.owner_tokens, *self.github.secrets()]

    # As the owner

    def put_secrets(self, worker: str) -> None:
        """The owner's two secrets, bound to project evo-agents on ``worker``."""
        bound = {"projects": [PROJECT], "workers": [worker]}
        env = {"kind": "env", "env_var": "CLAUDE_CODE_OAUTH_TOKEN", "value": self.oauth, **bound}
        prefix = self.git_http.url.replace("http://", "https://", 1)  # the form of normalize_origin
        bodies = {"claude-oauth": env, "git-http": {"kind": "git", "url_prefix": prefix, "value": self.glpat, **bound}}
        for name, body in bodies.items():
            response = self.client.put(f"/v1/secrets/{name}", json=body, headers=self.owner)
            assert response.status_code == 200, response.text
            assert self.glpat not in response.text and self.oauth not in response.text

    def dispatch(self, repo: str) -> int:
        body = {"plan_id": PLAN, "steps": [STEPS[repo]], "runtime": "claude-code", "approval": "auto"}
        response = self.client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=self.owner)
        assert response.status_code == 201, response.text
        (run,) = response.json()
        assert (run["state"], run["dispatched_by"]) == ("queued", OWNER)
        return run["id"]

    def run(self, run_id: int) -> dict:
        response = self.client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=self.owner)
        assert response.status_code == 200, response.text
        return response.json()

    def wait_state(self, run_id: int, *states: str, poke=None) -> dict:
        found: dict = {}

        def reached() -> bool:
            if poke is not None:
                poke()
            found.update(self.run(run_id))
            return found["state"] in states

        def explain() -> str:
            return f"run {run_id} is {found.get('state')} ({found.get('error')})\n{self.explain()}"

        wait_until(reached, f"run {run_id} to be {' or '.join(states)}", WAIT, explain)
        return found

    def leases(self, run_id: int) -> dict[str, dict]:
        """The leases of the run as its owner sees them, by name."""
        response = self.client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/credentials", headers=self.owner)
        assert response.status_code == 200, response.text
        assert all(value not in response.text for value in self.values())
        return {lease["name"]: lease for lease in response.json()}

    def texts(self, run_id: int) -> list[str]:
        path = f"/v1/projects/{PROJECT}/runs/{run_id}/events"
        response = self.client.get(path, params={"limit": 1000}, headers=self.owner)
        assert response.status_code == 200, response.text
        for value in self.values():
            assert value not in response.text, "a leased value is in the run's events"
        return [e["body"]["text"] for e in response.json()["events"] if isinstance(e["body"].get("text"), str)]

    def audit(self, action: str, run_id: int) -> list[dict]:
        """The audit rows of ``action`` naming run ``run_id``, oldest first."""
        response = self.client.get("/v1/admin/audit", params={"action": action, "limit": 200}, headers=self.admin)
        assert response.status_code == 200, response.text
        assert all(value not in response.text for value in self.values())
        rows = [row for row in response.json()["items"] if f" run:{run_id} " in f"{row['target']} "]
        return sorted(rows, key=lambda row: row["id"])

    def hub_lines(self, msg: str, run_id: int) -> list[dict]:
        """The JSON lines of hub serve's log with this message about run ``run_id``."""
        found = []
        for line in self.hub.api_log.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                if isinstance(record, dict) and record.get("msg") == msg and record.get("run_id") == run_id:
                    found.append(record)
        return found

    def revoked_at_github(self) -> list[str]:
        """The tokens the fake GitHub was asked to revoke with DELETE /installation/token, in order."""
        calls = [call for call in self.github.calls("/installation/token") if call.method == "DELETE"]
        return [call.headers.get("authorization", "").partition(" ")[2] for call in calls]

    def mcp(self, token: str, message: dict, headers: dict) -> httpx.Response:
        """``message`` to /mcp with ``token`` and ``headers``, as an agent's own client would send it."""
        return self.client.post("/mcp", json=message, headers={**bearer(token), **MCP_HEADERS, **headers})

    def assert_nothing_leaked(self, machine: Machine) -> None:
        """No file under the worker's HOME holds a value, and only ~/.evo/worker/token holds the worker's token; no
        line of the daemon's output or of the hub's logs holds either."""
        values = self.values()
        assert machine.files_holding(values) == [], "a file under the worker's HOME holds a secret's value"
        assert machine.files_holding([machine.token]) == [machine.state / "token"]
        assert not (machine.home / ".evo" / "hub").exists(), "the worker's HOME got a machine token"
        logs = [path for path in (self.hub.api_log, self.hub.jobs_log) if path.exists()]
        outputs = {
            "the daemon's output": machine.output(),
            **{path.name: path.read_text(encoding="utf-8", errors="replace") for path in logs},
        }
        for where, text in outputs.items():
            assert all(value not in text for value in [*values, machine.token]), f"a secret's value is in {where}"
            assert not TOKEN_LEAK.findall(text), f"{where} holds what looks like a token"


@pytest.fixture
def world(hub_db, tmp_path, github, s3, monkeypatch):
    private, public = app_key()
    github.app_public_key = public
    github.install(GITHUB_OWNER, APP_REPO)
    with ExitStack() as stack:
        hub = Hub(hub_db, tmp_path, github, s3)
        hub.env.update(
            EVO_HUB_SECRETS_KEY=base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode(),
            EVO_HUB_GITHUB_APP_ID=github.app_id,
            EVO_HUB_GITHUB_APP_PRIVATE_KEY=private,
        )
        stack.callback(hub.stop)
        hub.start_api()
        git_root = tmp_path / "git-http"
        git_root.mkdir()
        git_http = stack.enter_context(GitHttp(git_root, GIT_USER, sample("glpat-")))
        client = stack.enter_context(httpx.Client(base_url=hub.url, timeout=30))
        tokens = {
            "admin": live.sign_in(client, github, ADMIN, 901)["token"],
            "owner": live.sign_in(client, github, OWNER, 902)["token"],
        }
        made = World(tmp_path, hub, client, github, git_http, tokens)
        stack.callback(made.kill_daemons)  # before the hub stops
        projects = {
            PROJECT: registration(PROJECT, made.origins),
            OTHER_PROJECT: registration(OTHER_PROJECT, {OTHER_PROJECT: None}),
        }
        for name, body in projects.items():
            assert client.put(f"/v1/projects/{name}", json=body, headers=made.admin).status_code == 200
            granted = client.put(f"/v1/admin/projects/{name}/grants/{OWNER}", json=WRITER, headers=made.admin)
            assert granted.status_code == 200, granted.text
        for name, body in ((PROJECT, plan_body()), (OTHER_PROJECT, other_plan())):
            pushed = client.put(f"/v1/projects/{name}/plans/{body['id']}", json={"body": body}, headers=made.owner)
            assert pushed.status_code == 200, pushed.text
        install_graph(hub_db, Path(hub.env["EVO_HUB_DATA_DIR"]), tmp_path / "laptop", monkeypatch)
        yield made


def mcp_session(path: Path) -> None:
    """What the agent says to the hub's MCP: the handshake, kg_search and the session's projects, then the plans of
    the run's project and of the other one."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": HELLO},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "kg_search", "arguments": {"query": KG_QUERY}},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "hub_projects", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "plan_list", "arguments": {}}},
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "plan_list", "arguments": {"project": OTHER_PROJECT}},
        },
    ]
    path.write_text("".join(json.dumps(message) + "\n" for message in messages), encoding="utf-8")


def answers(path: Path) -> dict[int, dict]:
    replies = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {reply["id"]: reply for reply in replies}


def config_entries(env: dict) -> list[tuple[str, str]]:
    """The GIT_CONFIG_* entries of an environment, in order."""
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    return [(env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"]) for n in range(count)]


def test_a_worker_without_credentials_of_its_own_runs_on_leases_and_gives_them_back(world):
    tmp = world.tmp
    started, proceed = tmp / "agent-started", tmp / "agent-proceed"
    env_seen = {repo: tmp / f"agent-env-{repo}.json" for repo in STEPS}
    mcp_in, mcp_out, mcp_err = tmp / "mcp-in.jsonl", tmp / "mcp-out.jsonl", tmp / "mcp-err.txt"
    mcp_session(mcp_in)
    quoted = {name: shlex.quote(str(path)) for name, path in (("in", mcp_in), ("out", mcp_out), ("err", mcp_err))}
    proxy = f"{shlex.quote(sys.executable)} -m evo_agents hub mcp < {quoted['in']} > {quoted['out']} 2> {quoted['err']}"
    scenarios = {
        str(STEPS[REPO]): [
            {"env": str(env_seen[REPO])},
            {"sh": proxy},
            {"write": {"leased.txt": "pushed with a lease\n"}},
            {"commit": "agent: add leased.txt"},
            {"sh": f"git push --quiet origin HEAD:refs/heads/{AGENT_PUSH}"},
            {"touch": str(started)},
            {"wait_for": str(proceed)},
            {"result": {"verify_commands": ["test -f leased.txt"], "summary": "Pushed with a lease."}},
        ],
        str(STEPS[APP_REPO]): [
            {"env": str(env_seen[APP_REPO])},
            {"write": {"app.txt": "on a token of the App\n"}},
            {"commit": "agent: add app.txt"},
            {"result": {"verify_commands": ["test -f app.txt"], "summary": "Ran with a token of the App."}},
        ],
    }
    mac = world.machine(WORKER, scenarios)
    for held in (".evo/hub", ".config/gh", ".ssh"):
        assert not (mac.home / held).exists(), f"the worker's HOME has ~/{held}"
    mac.join()
    assert not (mac.home / ".evo" / "hub").exists(), "joining signs nobody in"
    world.put_secrets(WORKER)
    proc = mac.start_daemon()

    # A step on evo-agents: the env secret and the git secret, while the agent waits.
    run_id = world.dispatch(REPO)
    wait_until(started.exists, "the agent of the first run to push", WAIT, world.explain)
    leases = world.leases(run_id)
    assert set(leases) == {"claude-oauth", "git-http"}, leases
    env_lease, git_lease = leases["claude-oauth"], leases["git-http"]
    assert (env_lease["kind"], env_lease["target"], env_lease["worker"]) == ("env", "CLAUDE_CODE_OAUTH_TOKEN", WORKER)
    origin = world.origins[REPO].replace("http://", "https://", 1).removesuffix(".git")
    assert (git_lease["kind"], git_lease["target"], git_lease["worker"]) == ("git", origin, WORKER)
    assert env_lease["revoked_at"] is None and git_lease["revoked_at"] is None, "out while the run holds them"
    assert mac.files_holding(world.values()) == [], "a file under the worker's HOME holds a value while the run runs"
    # The worker's token opens /mcp for its run alone, and only in the run's project.
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert world.mcp(mac.token, listing, {"X-Evo-Run": str(run_id)}).status_code == 200
    other = world.mcp(mac.token, listing, {"X-Evo-Run": str(run_id), "X-Evo-Project": OTHER_PROJECT})
    assert other.status_code == 403, other.text
    assert f"run {run_id} is of project {PROJECT}" in other.json()["message"]
    assert world.mcp(mac.token, listing, {}).status_code == 403, "a worker token without X-Evo-Run"
    assert world.client.get("/v1/projects", headers=bearer(mac.token)).status_code == 403
    proceed.write_text("go", encoding="utf-8")
    run = world.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], world.explain())

    # The agent's environment had the env lease, and git's configuration for the leased origin alone.
    seen = json.loads(env_seen[REPO].read_text(encoding="utf-8"))
    assert seen.get("CLAUDE_CODE_OAUTH_TOKEN") == world.oauth, "the env lease is in the agent's environment"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in mac.leased(run_id), "and the runtime is told it comes from a lease"
    assert (seen["EVO_RUN_ID"], seen["EVO_RUN_PROJECT"]) == (str(run_id), PROJECT)
    assert world.glpat not in json.dumps(seen), "a git lease goes to git through the helper, never into the env"
    entries = config_entries(seen)
    helpers = [value for key, value in entries if key == f"credential.{world.origins[REPO]}.helper"]
    assert helpers[0] == "" and helpers[1].endswith(f" worker git-credential --run {run_id}"), entries
    assert {key for key, _ in entries} <= {
        f"credential.{world.origins[REPO]}.helper",
        f"credential.{world.origins[REPO]}.useHttpPath",
    }, "the origin of evo-cli is no leased one"
    assert not {"SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY"} & seen.keys()

    # Its push, and the daemon's fetch and push, reached the origin with the git lease alone.
    pushed = {command["cmd"]: command for command in mac.commands() if command["run"] == run_id}
    push = pushed[f"git push --quiet origin HEAD:refs/heads/{AGENT_PUSH}"]
    assert push["exit"] == 0, push["stderr"]
    origin_dir = str(world.bare[REPO])
    agent_tip = git("--git-dir", origin_dir, "rev-parse", f"refs/heads/{AGENT_PUSH}")
    assert git("--git-dir", origin_dir, "log", "--format=%s", "-1", agent_tip) == "agent: add leased.txt"
    assert run["commit_sha"] == git("--git-dir", origin_dir, "rev-parse", f"refs/heads/{BRANCHES[REPO]}")
    served = world.git_http.requests
    receive = [(user, status) for verb, path, user, status in served if path.endswith("/git-receive-pack")]
    assert receive and set(receive) == {(GIT_USER, 200)}, served
    fetched = [(user, status) for verb, path, user, status in served if path.endswith("/git-upload-pack")]
    assert fetched and set(fetched) == {(GIT_USER, 200)}, served
    assert all(user == GIT_USER for _, _, user, status in served if status != 401), served
    assert not mac.helper_calls.exists(), "the machine's own credential helper was called"
    assert not any(text.startswith("no leased credential") for text in world.texts(run_id))

    # Through `evo-agents hub mcp` with the worker's token: the graph of the run's project, and no other project.
    ran_mcp = next(command for command in mac.commands() if "hub mcp" in command["cmd"])
    assert ran_mcp["exit"] == 0, mcp_err.read_text(encoding="utf-8")
    replies = answers(mcp_out)
    assert set(replies) == {1, 2, 3, 4, 5}, replies
    found = replies[2]["result"]
    assert not found.get("isError"), found
    assert found["structuredContent"]["project"] == PROJECT
    assert KG_QUERY in json.dumps(found["structuredContent"]["results"][0])
    projects = replies[3]["result"]["structuredContent"]
    assert projects["session_project"] == PROJECT
    assert [project["name"] for project in projects["projects"]] == [PROJECT], "the owner's other project is hidden"
    assert PLAN in replies[4]["result"]["content"][0]["text"]
    refused = replies[5]["result"]
    assert refused["isError"] and f"no project {OTHER_PROJECT} that you can see" in refused["content"][0]["text"]
    plans = world.client.get(f"/v1/projects/{OTHER_PROJECT}/plans", headers=world.owner)
    assert plans.status_code == 200 and OTHER_PLAN in plans.text, "the owner reaches what the agent may not"
    gone = world.mcp(mac.token, listing, {"X-Evo-Run": str(run_id)})
    assert gone.status_code == 403, "the run ended: its worker's token no longer opens /mcp for it"

    # The daemon gave the leases back; the hub had revoked them when the run ended, and audited both.
    wait_until(lambda: world.hub_lines("credentials given back", run_id), "the daemon's DELETE", WAIT, world.explain)
    assert world.hub_lines("credentials leased", run_id)
    assert all(lease["revoked_at"] for lease in world.leases(run_id).values())
    (lease_row,) = world.audit("credential.lease", run_id)
    assert (lease_row["actor"], lease_row["project"]) == (OWNER, PROJECT)
    assert "secrets=claude-oauth,git-http github-app=- " in lease_row["target"]
    revoke_rows = world.audit("credential.revoke", run_id)
    assert revoke_rows and revoke_rows[0]["target"].endswith("by=run-done"), revoke_rows
    world.assert_nothing_leaked(mac)

    # A step on evo-cli, on github.com: a token of the hub's App, revoked at GitHub once the run ends.
    app_run = world.dispatch(APP_REPO)
    run = world.wait_state(app_run, "done", "failed")
    assert run["state"] == "done", (run["error"], world.explain())
    (token,) = world.github.app_tokens
    (asked,) = world.github.calls("/app/installations/1001/access_tokens")
    assert json.loads(asked.body)["repositories"] == [APP_REPO]
    leases = world.leases(app_run)
    assert set(leases) == {"claude-oauth", f"github-app:{GITHUB_OWNER}"}, leases
    app_lease = leases[f"github-app:{GITHUB_OWNER}"]
    assert (app_lease["provider"], app_lease["target"]) == (
        "github-app",
        f"https://github.com/{GITHUB_OWNER}/{APP_REPO}",
    )
    wait_until(lambda: token in world.revoked_at_github(), "DELETE /installation/token", WAIT, world.explain)
    assert world.revoked_at_github() == [token] and world.github.app_tokens[token].revoked
    assert not world.github.covers(token, GITHUB_OWNER, APP_REPO)
    wait_until(lambda: world.hub_lines("credentials given back", app_run), "the daemon's DELETE", WAIT, world.explain)
    assert all(lease["revoked_at"] for lease in world.leases(app_run).values())
    seen = json.loads(env_seen[APP_REPO].read_text(encoding="utf-8"))
    assert seen.get("CLAUDE_CODE_OAUTH_TOKEN") == world.oauth and token not in json.dumps(seen)
    (lease_row,) = world.audit("credential.lease", app_run)
    assert f"secrets=claude-oauth github-app={GITHUB_OWNER} repos={APP_REPO}" in lease_row["target"]
    assert world.audit("credential.revoke", app_run)
    assert not mac.helper_calls.exists(), "the machine's own credential helper was called"

    assert mac.stop_daemon(proc) == 0, mac.output()
    world.assert_nothing_leaked(mac)


def test_a_daemon_killed_mid_run_leaves_its_leases_to_the_reaper(world):
    world.hub.start_jobs()
    started = world.tmp / "agent-started"
    mac = world.machine(
        WORKER, {str(STEPS[APP_REPO]): [{"touch": str(started)}, {"wait_for": str(world.tmp / "never")}]}
    )
    mac.join()
    world.put_secrets(WORKER)
    proc = mac.start_daemon()
    run_id = world.dispatch(APP_REPO)
    wait_until(started.exists, "the agent to start", WAIT, world.explain)
    world.wait_state(run_id, "running")
    leases = world.leases(run_id)
    assert set(leases) == {"claude-oauth", f"github-app:{GITHUB_OWNER}"}, leases
    assert not any(lease["revoked_at"] for lease in leases.values())
    (token,) = world.github.app_tokens
    assert world.github.covers(token, GITHUB_OWNER, APP_REPO)

    proc.send_signal(signal.SIGKILL)  # no report, no DELETE, no heartbeat: the run's lease on the hub runs out
    assert proc.wait(30) == -signal.SIGKILL
    killed_at = time.monotonic()

    def reap_once_the_lease_may_be_over() -> None:
        if time.monotonic() - killed_at > LEASE_SECONDS:
            world.hub.reap_soon()

    lost = world.wait_state(run_id, "lost", poke=reap_once_the_lease_may_be_over)
    assert lost["error"] == f"its worker {WORKER} stopped extending the lease"
    assert all(lease["revoked_at"] for lease in world.leases(run_id).values())
    wait_until(
        lambda: token in world.revoked_at_github(), "the reaper's DELETE /installation/token", WAIT, world.explain
    )
    assert not world.github.covers(token, GITHUB_OWNER, APP_REPO)
    assert world.hub_lines("credentials given back", run_id) == [], "the killed daemon gave nothing back"
    (revoked,) = world.audit("credential.revoke", run_id)
    assert revoked["actor"] is None and revoked["target"].endswith("by=run-lost"), revoked
    assert "leases=2 secrets=claude-oauth github-app=1" in revoked["target"]
    world.assert_nothing_leaked(mac)
