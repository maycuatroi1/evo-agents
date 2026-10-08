"""The leases of a run on the worker (``evo_agents.worker.credentials``): held in the daemon's memory, handed to git
through the run's socket and helper, to the agent through its environment, and given back.

The checks step 8 of the worker-credentials plan names: ``git credential fill`` with the run's environment answers the
lease's token and never calls the machine's own helper (a fake one writes a file when it is called); a push to a local
``git http-backend`` behind Basic auth works with the lease and fails once the lease is given back; no file under the
temporary HOME holds a lease value; the script of an interactive pane holds none either (it evaluates ``evo-agents
worker env``); an SSH origin is rewritten to https in the run's environment only. Around them: the git configuration
of the leased origins, the socket answering its own uid alone, a GitHub token near its end asked for again, the values
masked in logs and events, the notes of origins git reaches with the machine's own, and a hub that refuses or does not
answer.

Every HOME is a temporary one, with ``GIT_CONFIG_NOSYSTEM``: nothing reads or writes this machine's git configuration
or ``~/.evo/worker``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import secrets
import socket
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evo_agents.hub.credentials import Lease
from evo_agents.worker import credentials, interactive
from evo_agents.worker.credentials import MISSING_NOTE, RunCredentials
from evo_agents.worker.home import WorkerHome
from tests.worker.git_http import GitHttp

ROOT = Path(__file__).parents[2]
WORKER_EXTRA = importlib.util.find_spec("aiohttp") is not None  # RunCredentials.take calls the hub with hubapi
needs_worker = pytest.mark.skipif(not WORKER_EXTRA, reason="the daemon needs the worker extra, evo-ak[worker]")
RUN = 7
GITLAB = "https://git.example.org/group"
IDENTITY = {
    "GIT_AUTHOR_NAME": "Plan Owner",
    "GIT_AUTHOR_EMAIL": "owner@example.org",
    "GIT_COMMITTER_NAME": "Plan Owner",
    "GIT_COMMITTER_EMAIL": "owner@example.org",
}


def sample(prefix: str) -> str:
    """A value drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


def git_lease(lease_id: int, url_prefix: str, value: str) -> dict:
    return Lease(lease_id, "git", "secret", "gitlab", url_prefix=url_prefix, username="oauth2", value=value).to_json()


def env_lease(lease_id: int, env_var: str, value: str) -> dict:
    return Lease(lease_id, "env", "secret", env_var.lower(), env_var=env_var, value=value).to_json()


def app_lease(lease_id: int, owner: str, value: str, minutes: float) -> dict:
    expires = datetime.now(UTC) + timedelta(minutes=minutes)
    lease = Lease(
        lease_id,
        "git",
        "github-app",
        f"github-app:{owner}",
        url_prefix=f"https://github.com/{owner}",
        username="x-access-token",
        value=value,
        expires_at=expires,
    )
    return lease.to_json()


class StubHub:
    """The two calls of ``hubapi.WorkerHub`` the leases make: each ask answers the next of ``answers`` (the last one
    again once they run out); an exception among them is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asks = 0
        self.given_back: list[int] = []

    async def credentials(self, run_id: int) -> dict:
        self.asks += 1
        answer = self.answers[min(self.asks, len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def release_credentials(self, run_id: int) -> dict:
        self.given_back.append(run_id)
        return {"revoked": 1}


class Machine:
    """A temporary HOME whose git configuration names a helper of the machine's own for every URL and for
    git.example.org, which notes each call in ``called``; and the worker's state directory under it."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.state = self.home / ".evo" / "worker"
        self.called = tmp / "machine-helper-called"
        helper = tmp / "machine-helper.sh"
        helper.write_text(f'#!/bin/sh\necho "$1" >> {self.called}\ncat > /dev/null\n', encoding="utf-8")
        helper.chmod(0o755)
        self.home.mkdir()
        self.gitconfig = self.home / ".gitconfig"
        self.gitconfig.write_text(
            f'[credential]\n\thelper = !{helper}\n[credential "https://git.example.org"]\n\thelper = !{helper}\n',
            encoding="utf-8",
        )
        self.worker = WorkerHome(self.state)
        self.worker.ensure()
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "PYTHONPATH": str(ROOT),
            "EVO_WORKER_HOME": str(self.state),
            **IDENTITY,
        }
        self.notes: list[tuple[str, dict]] = []

    def note(self, text: str, **extra) -> None:
        self.notes.append((text, extra))

    def leases(self, run_id: int = RUN) -> RunCredentials:
        return RunCredentials(run_id, self.worker, self.env, self.note)

    def files_holding(self, *values: str) -> list[Path]:
        """The files under HOME that hold one of ``values``."""
        found = []
        for path in self.home.rglob("*"):
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                if any(value.encode() in data for value in values):
                    found.append(path)
        return found


@pytest.fixture
def machine(tmp_path) -> Machine:
    return Machine(tmp_path)


async def run(argv, env, cwd=None, stdin: str = "") -> subprocess.CompletedProcess:
    """A command in a process of its own, while this event loop goes on serving the run's socket."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=str(cwd) if cwd else None,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), 120)
    return subprocess.CompletedProcess(argv, proc.returncode, out.decode(), err.decode())


def git(*args, cwd: Path | None = None, env=None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


def fill(url: str) -> str:
    return f"url={url}\n\n"


def answered(stdout: str) -> dict:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


# The git configuration, without a hub


def test_the_git_configuration_resets_the_helpers_of_leased_origins_and_rewrites_an_ssh_one():
    assert credentials.https_url("https://github.com/maycuatroi1/evo-agents.git") == (
        "https://github.com/maycuatroi1/evo-agents.git"
    )
    assert (
        credentials.https_url("https://me@GitLab.example.org:8443/g/r.git") == "https://gitlab.example.org:8443/g/r.git"
    )
    assert credentials.https_url("git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git") == (
        "https://gitlab.m1ops.com/fis-gb-m1/m1-kb-docs.git"
    )
    assert credentials.https_url("ssh://git@gitlab.m1ops.com:2222/fis-gb-m1/m1-kb-docs") == (
        "https://gitlab.m1ops.com/fis-gb-m1/m1-kb-docs"
    )
    assert credentials.https_url("http://me@Git.example.org:8080/group/repo.git") == (
        "https://git.example.org:8080/group/repo.git"
    ), "a lease never goes over plain http: an http origin is reached over https"
    leases = [
        Lease.from_json(git_lease(1, "https://gitlab.m1ops.com/fis-gb-m1", "a" * 20)),
        Lease.from_json(git_lease(2, "https://gitlab.m1ops.com/fis-gb-m1/m1-kb-docs", "b" * 20)),
        Lease.from_json(app_lease(3, "maycuatroi1", "c" * 20, 60)),
        Lease.from_json(env_lease(4, "CLAUDE_CODE_OAUTH_TOKEN", "d" * 20)),
    ]
    assert credentials.covering(leases, "git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git").id == 2, "the longest prefix"
    assert credentials.covering(leases, "https://gitlab.m1ops.com/fis-gb-m1/other.git").id == 1
    assert credentials.covering(leases, "https://github.com/hawkteam404/m1-identity.git") is None
    origins = [
        "git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git",
        "https://github.com/maycuatroi1/evo-agents.git",
        "https://github.com/hawkteam404/m1-identity.git",  # no lease: the machine's helpers stay
        "https://github.com/maycuatroi1/evo-agents.git",
    ]
    helper = "!evo-agents worker git-credential --run 7"
    kb, evo = "https://gitlab.m1ops.com/fis-gb-m1/m1-kb-docs.git", "https://github.com/maycuatroi1/evo-agents.git"
    assert credentials.git_config(origins, leases, helper) == [
        (f"credential.{kb}.helper", ""),
        (f"credential.{kb}.helper", helper),
        (f"credential.{kb}.useHttpPath", "true"),
        (f"url.{kb}.insteadOf", "git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git"),
        (f"credential.{evo}.helper", ""),
        (f"credential.{evo}.helper", helper),
        (f"credential.{evo}.useHttpPath", "true"),
    ]
    entries = [("a.b", "1"), ("c.d", "2")]
    assert credentials.config_env({"GIT_CONFIG_COUNT": "1"}, entries) == {
        "GIT_CONFIG_KEY_1": "a.b",
        "GIT_CONFIG_VALUE_1": "1",
        "GIT_CONFIG_KEY_2": "c.d",
        "GIT_CONFIG_VALUE_2": "2",
        "GIT_CONFIG_COUNT": "3",
    }, "after the entries the daemon's own environment sets"
    assert credentials.config_env({}, []) == {}


def test_an_http_origin_a_lease_covers_is_rewritten_to_https_and_its_helper_keyed_for_https_alone():
    lease = Lease.from_json(git_lease(1, "https://git.example.org/group", "a" * 20))
    http, https = "http://git.example.org/group/repo.git", "https://git.example.org/group/repo.git"
    helper = "!evo-agents worker git-credential --run 7"
    assert credentials.git_config([http], [lease], helper) == [
        (f"credential.{https}.helper", ""),
        (f"credential.{https}.helper", helper),
        (f"credential.{https}.useHttpPath", "true"),
        (f"url.{https}.insteadOf", http),
    ]
    assert not any(key.startswith("credential.http://") for key, _ in credentials.git_config([http], [lease], helper))
    # an https origin is not rewritten
    assert credentials.git_config([https], [lease], helper) == credentials.git_config([http], [lease], helper)[:3]


def test_git_reads_the_attributes_its_helper_gets_and_the_helper_is_this_evo_agents():
    lines = ["protocol=https", "host=github.com", "path=maycuatroi1/evo-agents.git", "", "ignored=after"]
    assert credentials.read_attributes(line + "\n" for line in lines) == {
        "protocol": "https",
        "host": "github.com",
        "path": "maycuatroi1/evo-agents.git",
    }
    found = credentials.read_attributes(["url=http://127.0.0.1:8080/origin.git\n"])
    assert (found["protocol"], found["host"], found["path"]) == ("http", "127.0.0.1:8080", "origin.git")
    helper = credentials.helper_command(9)
    assert helper.startswith("!") and helper.endswith(" worker git-credential --run 9")
    assert sys.executable in helper or "evo-agents" in helper, "by absolute path, never what PATH finds first"
    assert credentials.env_command(9).endswith(" worker env --run 9")


# With the run's socket


@needs_worker
def test_git_credential_fill_answers_the_lease_and_never_calls_the_machines_helper(machine):
    token, other = sample("glpat-"), sample("oauth-")
    hub = StubHub(
        {
            "leases": [git_lease(1, GITLAB, token), env_lease(2, "CLAUDE_CODE_OAUTH_TOKEN", other)],
            "missing": [],
        }
    )
    origins = {
        "repo": ["https://git.example.org/group/repo.git"],
        "kb": ["git@git.example.org:group/kb.git"],
        "elsewhere": ["https://elsewhere.example.org/x/y.git"],
    }
    config_before = machine.gitconfig.read_text(encoding="utf-8")

    async def go():
        leases = machine.leases()
        await leases.take(hub, origins)
        env = {**machine.env, **leases.git_vars}
        repo = await run(
            ["git", "credential", "fill"], env, machine.tmp, fill("https://git.example.org/group/repo.git")
        )
        kb = await run(["git", "credential", "fill"], env, machine.tmp, fill("https://git.example.org/group/kb.git"))
        rewritten = await run(["git", "ls-remote", "--get-url", "git@git.example.org:group/kb.git"], env, machine.tmp)
        plain = await run(["git", "ls-remote", "--get-url", "git@git.example.org:group/kb.git"], machine.env)
        called_before_other = machine.called.exists()
        elsewhere = await run(
            ["git", "credential", "fill"], env, machine.tmp, fill("https://elsewhere.example.org/x/y.git")
        )
        agent = {**machine.env, **leases.agent_vars}
        await leases.release()
        return repo, kb, rewritten, plain, called_before_other, elsewhere, agent

    repo, kb, rewritten, plain, called_before_other, elsewhere, agent = asyncio.run(go())
    assert repo.returncode == 0, repo.stderr
    assert answered(repo.stdout)["username"] == "oauth2" and answered(repo.stdout)["password"] == token
    assert kb.returncode == 0 and answered(kb.stdout)["password"] == token, "the SSH origin, as https, gets it too"
    assert not called_before_other, "git never asked the machine's own helper for a leased origin"
    assert rewritten.stdout.strip() == "https://git.example.org/group/kb.git", "the run's git reaches SSH over https"
    assert plain.stdout.strip() == "git@git.example.org:group/kb.git", "and only the run's git"
    assert elsewhere.returncode != 0 and machine.called.exists(), "an origin with no lease keeps the machine's helper"
    assert machine.gitconfig.read_text(encoding="utf-8") == config_before, "the machine's git config is not touched"
    assert agent["CLAUDE_CODE_OAUTH_TOKEN"] == other, "an env lease is in the agent's environment"
    assert agent["GIT_CONFIG_COUNT"] == "7", "and so is git's configuration: three entries per origin, and insteadOf"
    assert hub.given_back == [RUN]
    assert machine.files_holding(token, other) == [], "no file under HOME holds a lease value"


def served_checkout(machine: Machine) -> tuple[Path, Path]:
    """A bare origin.git under ``served`` (for ``GitHttp``) and a checkout of it under HOME with a first commit."""
    served = machine.tmp / "served"
    git("init", "--quiet", "--bare", str(served / "origin.git"), env=machine.env)
    checkout = machine.home / "ws" / "repo"
    git("clone", "--quiet", str(served / "origin.git"), str(checkout), env=machine.env)
    (checkout / "README.md").write_text("# repo\n", encoding="utf-8")
    git("add", "README.md", cwd=checkout, env=machine.env)
    git("commit", "--quiet", "-m", "first", cwd=checkout, env=machine.env)
    return served, checkout


@needs_worker
def test_a_push_of_an_http_origin_goes_over_https_with_the_lease_and_fails_once_it_is_given_back(machine):
    token = sample("glpat-")
    served, checkout = served_checkout(machine)

    with GitHttp(served, "oauth2", token) as server:
        machine.env.update(server.env)
        origin = server.repo_url("origin.git", "http")  # the server answers https alone
        git("remote", "set-url", "origin", origin, cwd=checkout, env=machine.env)
        hub = StubHub({"leases": [git_lease(1, server.url, token)], "missing": []})  # an https prefix covers it

        async def go():
            from evo_agents.worker import gitops

            leases = machine.leases()
            await leases.take(hub, {"repo": await gitops.remote_urls(checkout)})
            env = {**machine.env, **leases.git_vars}
            first = await gitops.push(checkout, "feat/leased", env=env)
            await leases.release()
            (checkout / "more.txt").write_text("more\n", encoding="utf-8")
            await gitops.git(checkout, "add", "more.txt", env=machine.env)
            await gitops.git(checkout, "commit", "--quiet", "-m", "more", env=machine.env)
            try:
                await gitops.push(checkout, "feat/leased", env=env)
            except gitops.GitError as exc:
                return first, str(exc)
            return first, None

        first, refused = asyncio.run(go())
        statuses = server.statuses()

    assert first.changed and git("--git-dir", str(served / "origin.git"), "rev-parse", "feat/leased") == first.head
    assert 200 in statuses and 401 in statuses, "git authenticated after the challenge, with the lease, over https"
    assert refused is not None, "once the lease is given back, the push has no credential"
    assert "could not read Username" in refused or "Authentication failed" in refused, refused
    assert git("--git-dir", str(served / "origin.git"), "rev-parse", "feat/leased") == first.head
    assert not machine.called.exists(), "the machine's own helper was never asked for the leased origin"
    assert hub.given_back == [RUN]
    assert machine.files_holding(token) == [], "no file under HOME holds the token"


@needs_worker
def test_the_socket_answers_processes_of_its_own_uid_only_and_lives_as_long_as_the_leases(machine, monkeypatch):
    token = sample("glpat-")
    hub = StubHub({"leases": [git_lease(1, GITLAB, token), env_lease(2, "OPENAI_API_KEY", sample("sk-"))]})

    async def go():
        leases = machine.leases()
        await leases.take(hub, {"repo": ["https://git.example.org/group/repo.git"]})
        path = credentials.socket_path(machine.worker, RUN)
        modes = (
            stat.S_IMODE(path.stat().st_mode),
            stat.S_IMODE(path.parent.stat().st_mode),
            stat.S_ISSOCK(path.stat().st_mode),
        )
        request = {"op": "git", "protocol": "https", "host": "git.example.org", "path": "group/repo.git"}
        own = await asyncio.to_thread(credentials.ask, machine.worker, RUN, request)
        plain = await asyncio.to_thread(credentials.ask, machine.worker, RUN, {**request, "protocol": "http"})
        monkeypatch.setattr(credentials, "peer_uid", lambda sock: os.getuid() + 1)
        stranger = await asyncio.to_thread(credentials.ask, machine.worker, RUN, request)
        monkeypatch.undo()
        env = await asyncio.to_thread(credentials.ask, machine.worker, RUN, {"op": "env"})
        unknown = await asyncio.to_thread(credentials.ask, machine.worker, RUN, {"op": "nothing"})
        await leases.release()
        after = await asyncio.to_thread(credentials.ask, machine.worker, RUN, request)
        return modes, own, plain, stranger, env, unknown, path.exists(), after, leases

    modes, own, plain, stranger, env, unknown, exists, after, leases = asyncio.run(go())
    assert modes == (0o600, 0o700, True)
    assert own == {"username": "oauth2", "password": token}
    assert plain == {}, "the lease covers the URL's https form, and never goes over plain http"
    assert stranger is None, "a process of another uid gets nothing"
    assert set(env["env"]) >= {"OPENAI_API_KEY", "GIT_CONFIG_COUNT"}
    assert "error" in unknown
    assert not exists and after is None, "the socket goes with the leases"
    assert (leases.leases, leases.agent_vars, leases.git_vars) == ([], {}, {}), "and the values are forgotten"
    left, right = socket.socketpair()
    assert credentials.peer_uid(left) == os.getuid()
    left.close()
    right.close()


@needs_worker
@pytest.mark.parametrize("minutes_left", [5, -1], ids=["near-its-end", "past-its-end"])
def test_git_credential_fill_through_the_runs_helper_gets_a_new_token_once_the_held_one_nears_its_end(
    machine, minutes_left
):
    held, fresh = sample("ghs_held"), sample("ghs_fresh")
    hub = StubHub(
        {"leases": [app_lease(1, "maycuatroi1", held, minutes_left)], "missing": []},
        {"leases": [app_lease(2, "maycuatroi1", fresh, 60)], "missing": []},
    )
    url = "https://github.com/maycuatroi1/evo-agents.git"

    async def go():
        leases = machine.leases()
        await leases.take(hub, {"evo-agents": [url]})
        env = {**machine.env, **leases.git_vars}
        asks_after_take = hub.asks
        first = await run(["git", "credential", "fill"], env, machine.tmp, fill(url))
        second = await run(["git", "credential", "fill"], env, machine.tmp, fill(url))
        await leases.release()
        return asks_after_take, first, second

    asks_after_take, first, second = asyncio.run(go())
    assert asks_after_take == 1
    assert first.returncode == 0, first.stderr
    got = answered(first.stdout)
    assert (got["username"], got["password"]) == ("x-access-token", fresh), (
        "git got the token the hub made when asked again, not the one with less than 10 minutes left"
    )
    assert hub.asks == 2, "the run asked the hub once more, before it answered git"
    assert answered(second.stdout)["password"] == fresh and hub.asks == 2, "the new token has an hour: no third ask"
    assert not machine.called.exists()
    assert machine.files_holding(held, fresh) == []


@needs_worker
def test_a_push_whose_token_the_origin_refuses_takes_the_leases_again_once_and_pushes_with_the_new_one(machine):
    from evo_agents.worker import gitops

    refused, taken = sample("glpat-revoked"), sample("glpat-good")
    served, checkout = served_checkout(machine)
    with GitHttp(served, "oauth2", taken) as server:
        machine.env.update(server.env)
        origin = server.repo_url("origin.git")
        git("remote", "set-url", "origin", origin, cwd=checkout, env=machine.env)
        prefix = server.url
        hub = StubHub(
            {"leases": [git_lease(1, prefix, refused)], "missing": []},
            {"leases": [git_lease(2, prefix, taken)], "missing": []},
        )

        async def go():
            leases = machine.leases()
            await leases.take(hub, {"repo": [origin]})
            env = {**machine.env, **leases.git_vars}
            pushed = await leases.with_renewal("repo", lambda: gitops.push(checkout, "feat/renewed", env=env))
            await leases.release()
            return pushed

        pushed = asyncio.run(go())
        requests = list(server.requests)

    assert pushed.changed and git("--git-dir", str(served / "origin.git"), "rev-parse", "feat/renewed") == pushed.head
    assert hub.asks == 2 and hub.given_back == [RUN, RUN], "given back and taken again once, then given back at the end"
    assert [user for _, _, user, status in requests if status == 200 and user] and requests[-1][3] == 200
    texts = [text for text, _ in machine.notes]
    assert any(text.startswith("The push of repo failed to authenticate") for text in texts), texts
    assert all(refused not in text and taken not in text for text in texts), "no value in the run's notes"
    assert not machine.called.exists()


@needs_worker
def test_a_second_refusal_is_not_tried_again_and_an_origin_without_a_lease_is_not_tried_twice(machine):
    from evo_agents.worker import gitops

    served, checkout = served_checkout(machine)
    with GitHttp(served, "oauth2", sample("glpat-good")) as server:
        machine.env.update(server.env)
        origin = server.repo_url("origin.git")
        git("remote", "set-url", "origin", origin, cwd=checkout, env=machine.env)
        prefix = server.url
        hub = StubHub(
            {"leases": [git_lease(1, prefix, sample("glpat-bad"))], "missing": []},
            {"leases": [git_lease(2, prefix, sample("glpat-worse"))], "missing": []},
            {"leases": [git_lease(3, prefix, sample("glpat-never-asked"))], "missing": []},
        )
        elsewhere = StubHub({"leases": [git_lease(4, "https://git.example.org/group", sample("glpat-"))]})

        async def attempt(leases, hub_used):
            await leases.take(hub_used, {"repo": [origin]})
            env = {**machine.env, **leases.git_vars}
            try:
                await leases.with_renewal("repo", lambda: gitops.push(checkout, "feat/refused", env=env))
            except gitops.GitError as exc:
                return exc
            finally:
                await leases.release()
            return None

        error = asyncio.run(attempt(machine.leases(), hub))
        refused_gets = [item for item in server.requests if item[3] == 401]
        uncovered = asyncio.run(attempt(machine.leases(RUN + 1), elsewhere))
        uncovered_gets = [item for item in server.requests if item[3] == 401][len(refused_gets) :]

    assert isinstance(error, gitops.GitAuthError), error
    assert hub.asks == 2, "the leases were taken again once, not twice"
    assert len(refused_gets) == 4, "two pushes, each refused with and without the token: no third push"
    assert git("--git-dir", str(served / "origin.git"), "for-each-ref", "refs/heads/feat/refused") == ""
    assert isinstance(uncovered, gitops.GitAuthError)
    assert elsewhere.asks == 1 and len(uncovered_gets) <= 2, "no lease covers the origin: no renewal, one push"


@needs_worker
def test_a_github_token_near_its_end_is_asked_for_again_before_git_gets_it(machine):
    old, new = sample("ghs_old"), sample("ghs_new")
    hub = StubHub(
        {"leases": [app_lease(1, "maycuatroi1", old, 5)], "missing": []},
        {"leases": [app_lease(2, "maycuatroi1", new, 60)], "missing": []},
    )
    request = {"op": "git", "protocol": "https", "host": "github.com", "path": "maycuatroi1/evo-agents.git"}

    async def go():
        leases = machine.leases()
        await leases.take(hub, {"evo-agents": ["git@github.com:maycuatroi1/evo-agents.git"]})
        first = await leases.answer(request)
        second = await leases.answer(request)
        other_owner = await leases.answer({**request, "path": "hawkteam404/m1-identity.git"})
        await leases.release()
        return first, second, other_owner

    first, second, other_owner = asyncio.run(go())
    assert first["password"] == new and first["username"] == "x-access-token", "asked again with 5 minutes left"
    assert second["password"] == new and hub.asks == 2, "a token with an hour left is not asked for again"
    assert other_owner == {}, "the token covers its owner's repos alone"

    from evo_agents.worker.hubapi import Unreachable

    stale = sample("ghs_stale")
    down = StubHub({"leases": [app_lease(3, "maycuatroi1", stale, 5)]}, Unreachable("no answer"))

    async def offline():
        leases = machine.leases(RUN + 1)
        await leases.take(down, {"evo-agents": ["https://github.com/maycuatroi1/evo-agents.git"]})
        answer = await leases.answer(request)
        await leases.release()
        return answer

    assert asyncio.run(offline())["password"] == stale, "the hub down, git gets the token it has, which still works"


@needs_worker
def test_lease_values_are_masked_in_the_log_and_in_events_while_a_run_holds_them(machine):
    from evo_agents.worker import logs

    token, oauth, shared = sample("glpat-"), sample("oauth-"), sample("shared-")
    first = StubHub({"leases": [git_lease(1, GITLAB, token), env_lease(2, "CLAUDE_CODE_OAUTH_TOKEN", shared)]})
    second = StubHub({"leases": [env_lease(3, "CLAUDE_CODE_OAUTH_TOKEN", shared), env_lease(4, "OPENAI_KEY", oauth)]})
    log_path = machine.tmp / "worker.log"
    logs.configure(log_path, stderr=False)

    def logged(text: str, **extra) -> str:
        """The line worker.log got for ``text``."""
        logging.getLogger("evo_agents.worker").warning(text, extra=extra)
        return log_path.read_text(encoding="utf-8").splitlines()[-1]

    async def go():
        one, two = machine.leases(7), machine.leases(8)
        await one.take(first, {})
        await two.take(second, {})
        line = logged(f"the agent printed {token}", seen=[oauth, shared])
        assert token not in line and oauth not in line and shared not in line and line.count("***") == 3
        body = {"text": f"echo {token}", "output": {"lines": [f"x{oauth}y", 3]}, "n": 1}
        assert credentials.scrub(body) == {"text": "echo ***", "output": {"lines": ["x***y", 3]}, "n": 1}

        # Run 7 gave its leases back: its own values are masked no more, the one run 8 holds too still is.
        await one.release()
        line = logged(f"after run 7: {token} {shared} {oauth}")
        assert token in line and shared not in line and oauth not in line
        assert credentials.scrub(f"{token} {shared} {oauth}") == f"{token} *** ***"

        # Run 8 too: the daemon holds no value any more, and masks none of them.
        await two.release()
        line = logged(f"after run 8: {shared} {oauth}")
        assert shared in line and oauth in line
        assert credentials.scrub(f"{token} {shared} {oauth}") == f"{token} {shared} {oauth}"

    asyncio.run(go())
    assert all(value not in credentials._held for value in (token, oauth, shared))
    assert repr(RunCredentials(RUN, machine.worker, machine.env, machine.note)).startswith("RunCredentials(run=7")


@needs_worker
def test_a_value_replaced_stays_masked_until_the_run_gives_its_leases_back(machine):
    from evo_agents.hub.log import scrub as scrub_log
    from evo_agents.worker import logs

    old, new, token = sample("app-old-"), sample("app-new-"), sample("worker-token-")
    hub = StubHub(
        {"leases": [app_lease(1, "maycuatroi1", old, 60), env_lease(2, "WORKER_ECHO", token)]},
        {"leases": [app_lease(3, "maycuatroi1", new, 60)]},
    )
    logs.mask(token)  # masked for good, as the daemon masks its worker token

    async def go():
        leases = machine.leases()
        await leases.take(hub, {})
        assert await leases.renew()
        # the token renewed away still works until its end: it stays masked while the run lasts
        assert credentials.scrub(f"{old} {new}") == "*** ***"
        assert scrub_log(f"git said {old} {new}") == "git said *** ***"
        await leases.release()
        assert credentials.scrub(f"{old} {new}") == f"{old} {new}"
        assert scrub_log(f"git said {old} {new} {token}") == f"git said {old} {new} ***", "the token stays masked"

    asyncio.run(go())
    logs.unmask(token)


@needs_worker
def test_the_commands_of_git_and_of_the_pane_ask_the_runs_socket(machine):
    token, oauth = sample("glpat-"), sample('oauth-it\'s "quoted" $HOME')
    hub = StubHub({"leases": [git_lease(1, GITLAB, token), env_lease(2, "CLAUDE_CODE_OAUTH_TOKEN", oauth)]})
    command = [sys.executable, "-m", "evo_agents", "worker"]
    out = machine.tmp / "pane-saw"
    worktree = machine.home / "ws" / "repo"
    worktree.mkdir(parents=True)

    async def go():
        leases = machine.leases()
        await leases.take(hub, {"repo": ["https://git.example.org/group/repo.git"]})
        attributes = "protocol=https\nhost=git.example.org\npath=group/repo.git\n\n"
        get = await run([*command, "git-credential", "--run", str(RUN), "get"], machine.env, stdin=attributes)
        store = await run([*command, "git-credential", "--run", str(RUN), "store"], machine.env, stdin=attributes)
        printed = await run([*command, "env", "--run", str(RUN)], machine.env)
        agent = {**machine.env, "EVO_RUN_ID": str(RUN), **leases.agent_vars}
        script = interactive.write_script(
            machine.state / "runs" / str(RUN) / "evo-run-7.sh",
            ["/bin/sh", "-c", f'printf %s "$CLAUDE_CODE_OAUTH_TOKEN" > {out}'],
            agent,
            worktree,
            withheld=leases.withheld,
            env_command=leases.pane_command(),
        )
        text = script.read_text(encoding="utf-8")
        pane = await run(["/bin/sh", str(script)], {"PATH": os.environ["PATH"], "HOME": str(machine.home)})
        await leases.release()
        gone = await run([*command, "git-credential", "--run", str(RUN), "get"], machine.env, stdin=attributes)
        gone_env = await run([*command, "env", "--run", str(RUN)], machine.env)
        return get, store, printed, text, pane, gone, gone_env

    get, store, printed, text, pane, gone, gone_env = asyncio.run(go())
    assert get.returncode == 0 and answered(get.stdout) == {"username": "oauth2", "password": token}, get.stderr
    assert store.returncode == 0 and store.stdout == "", "store does nothing"
    assert printed.returncode == 0 and "export CLAUDE_CODE_OAUTH_TOKEN=" in printed.stdout
    assert "export GIT_CONFIG_COUNT=" in printed.stdout
    assert oauth not in text and token not in text, "the pane's script holds no lease value"
    assert 'eval "$(' in text and " worker env --run 7)" in text
    assert "export CLAUDE_CODE_OAUTH_TOKEN" not in text and "export GIT_CONFIG_KEY" not in text, "withheld variables"
    assert pane.returncode == 0, pane.stderr
    assert out.read_text(encoding="utf-8") == oauth, "the pane got the value through the socket, quotes and all"
    out.unlink()
    assert gone.returncode == 0 and gone.stdout == "" and "holds no credentials" in gone.stderr
    assert gone_env.returncode == 1 and gone_env.stdout == ""
    assert machine.files_holding(token, oauth) == []


# What the run says, and a hub that does not lease


@needs_worker
def test_origins_without_a_lease_are_noted_and_keep_the_machines_credentials(machine):
    hub = StubHub(
        {
            "leases": [],
            "missing": [
                {"repo": "kb", "origin": "git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git", "reason": "no git secret"},
                {"repo": "notes", "origin": None, "reason": "the project registered no origin for it"},
            ],
        }
    )

    async def go():
        leases = machine.leases()
        await leases.take(hub, {"kb": ["git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git"], "app": ["/srv/app.git"]})
        exists = credentials.socket_path(machine.worker, RUN).exists()
        await leases.release()
        return leases, exists

    leases, exists = asyncio.run(go())
    texts = [text for text, _ in machine.notes]
    assert texts == [
        MISSING_NOTE.format(origin="git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git", reason="no git secret"),
        MISSING_NOTE.format(origin="notes", reason="the project registered no origin for it"),
        MISSING_NOTE.format(origin="/srv/app.git", reason=credentials.NOT_COVERED),
    ]
    assert texts[0].endswith("; git uses this machine's own")
    assert not exists and leases.withheld == frozenset() and leases.pane_command() is None
    assert hub.given_back == [RUN], "asked, so given back: the hub ends what it may have leased"


@needs_worker
def test_a_hub_that_refuses_or_does_not_answer_leaves_the_run_to_the_machines_credentials(machine, monkeypatch):
    from evo_agents.worker.hubapi import Refused, Unreachable

    old_hub = StubHub(Refused("the hub at http://hub answered HTTP 404 to POST /v1/worker/runs/7/credentials", 404))
    monkeypatch.setattr(credentials, "TAKE_TRIES", 2)
    no_answer = Unreachable("no answer")
    no_answer.retry_after = 0.0
    down = StubHub(no_answer)

    async def go():
        refused = machine.leases()
        await refused.take(old_hub, {"repo": ["https://git.example.org/group/repo.git"]})
        await refused.release()
        unanswered = machine.leases(RUN + 1)
        await unanswered.take(down, {"repo": ["https://git.example.org/group/repo.git"]})
        await unanswered.release(tries=1)
        return refused, unanswered

    refused, unanswered = asyncio.run(go())
    texts = [text for text, _ in machine.notes]
    assert texts[0].startswith("The hub leased this run no credentials (") and texts[0].endswith("this machine's own.")
    assert texts[1].startswith("The hub did not answer for the run's credentials")
    assert down.asks == 2
    assert old_hub.given_back == [], "nothing was leased by a hub that refused"
    assert refused.git_vars == {} and unanswered.agent_vars == {}
