"""The worker's guard of private names (``evo_agents.worker.guard``, ``docs/guard.md``): the run's git hooks refuse a
commit message or a push to a guarded repo that holds a name and still run the checkout's own hook with what git gave
it, the daemon's own push is refused the same way, and the daemon keeps the hub's policy by its digest. The names are
fictional (northwind, lumen).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from evo_agents import guard_check
from evo_agents.private_names import Policy, entries
from evo_agents.worker import gitops, guard
from evo_agents.worker.runner import authorship

TARGET = "github.com/acme/public"


def policy_data(*targets: str) -> dict:
    return Policy(terms=entries("Northwind"), targets=[TARGET, *targets]).to_json()


def git(cwd, *args, env=None, check=True, **kw) -> subprocess.CompletedProcess:
    base = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t"}
    base["GIT_COMMITTER_EMAIL"] = "t@x"
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env={**base, **(env or {})}, check=check, **kw
    )


@pytest.fixture
def setup(tmp_path):
    """A bare remote the policy guards (as a path, the way git hands it to the hook), a checkout of it, the run's hooks
    with the guard, and the environment the run's agent gets."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(remote), str(work))
    git(work, "checkout", "-q", "-b", "main")
    policy = tmp_path / "home" / "guard" / "policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps(policy_data(str(remote).lower())))
    hooks = authorship.trailer_hooks({}, tmp_path / "hooks", work, guard=sys.executable)
    env = {
        guard_check.ENV: str(policy),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(hooks),
    }
    return work, env, policy


def test_a_run_without_trailers_nor_guard_gets_no_hooks_and_one_without_guard_the_hook_of_before(tmp_path):
    assert authorship.trailer_hooks({}, tmp_path / "none", tmp_path) is None
    plain = authorship.hook_script(["Evo-Agent: a"], tmp_path)
    assert "guard_check" not in plain and "input=$(cat)" not in plain


def test_the_run_hooks_refuse_a_name_in_a_commit_message_and_in_what_a_push_sends(setup):
    work, env, _ = setup
    (work / "a.txt").write_text("a\n")
    git(work, "add", ".")
    refused = git(work, "commit", "-q", "-m", "Brief of Northwind", env=env, check=False)
    assert refused.returncode != 0 and "Northwind" in refused.stderr
    git(work, "commit", "-q", "-m", "Brief", env=env)
    pushed = git(work, "push", "-q", "origin", "main", env=env, check=False)
    assert pushed.returncode == 0, pushed.stderr
    (work / "b.txt").write_text("for northwind\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "more", env=env)
    blocked = git(work, "push", "-q", "origin", "main", env=env, check=False)
    assert blocked.returncode != 0 and "northwind" in blocked.stderr


def test_the_run_hooks_hand_the_checkouts_own_pre_push_what_git_gave_it(setup):
    work, env, _ = setup
    own = work / ".git" / "hooks" / "pre-push"
    own.write_text(f'#!/bin/sh\ncat > "{work}/../seen"\nexit 0\n')
    own.chmod(0o755)
    (work / "a.txt").write_text("a\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "clean", env=env)
    git(work, "push", "-q", "origin", "main", env=env)
    seen = (work.parent / "seen").read_text()
    assert seen.startswith("refs/heads/main ") and len(seen.split()) == 4


def test_the_daemons_own_push_is_refused_for_a_name_and_goes_elsewhere(setup):
    work, env, policy = setup
    (work / "c.txt").write_text("northwind\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "c", env={guard_check.ENV: ""})
    with pytest.raises(gitops.PushRefused, match="northwind"):
        guard.check_push(work, "HEAD", "main", "origin", {guard_check.ENV: str(policy)})
    guard.check_push(work, "HEAD", "main", "origin", {})  # no policy: nothing to check
    git(work, "remote", "set-url", "--push", "origin", str(work.parent / "elsewhere.git"))
    guard.check_push(work, "HEAD", "main", "origin", {guard_check.ENV: str(policy)})


def test_the_agent_gets_the_gh_shim_first_on_path_only_while_the_policy_guards_something(tmp_path):
    policy = tmp_path / "policy.json"
    env = {guard_check.ENV: str(policy), "PATH": "/usr/bin"}
    assert guard.agent_env(env, tmp_path) == {}
    policy.write_text(json.dumps(policy_data()))
    given = guard.agent_env(env, tmp_path)
    shim = guard.bin_dir(tmp_path)
    assert given["PATH"] == f"{shim}{os.pathsep}/usr/bin" and given[guard_check.SHIM_ENV] == str(shim)
    assert "-I -m evo_agents.guard_check gh" in (shim / "gh").read_text()


class FakeHub:
    def __init__(self, data):
        self.data, self.calls = data, []

    async def call(self, method, path):
        self.calls.append((method, path))
        return self.data


def test_the_daemon_keeps_the_hubs_policy_by_its_digest_and_drops_it_for_an_empty_one(tmp_path):
    data = {**policy_data(), "digest": Policy.from_json(policy_data()).digest()}
    hub = FakeHub(data)
    assert asyncio.run(guard.take(hub, tmp_path, data["digest"])) is True
    assert hub.calls == [("GET", "/v1/worker/guard/policy")]
    assert guard.held_digest(tmp_path) == data["digest"]
    assert asyncio.run(guard.take(hub, tmp_path, data["digest"])) is False and len(hub.calls) == 1
    assert asyncio.run(guard.take(hub, tmp_path, "")) is True
    assert not guard.policy_path(tmp_path).exists()
    with pytest.raises(ValueError):
        asyncio.run(guard.take(FakeHub({"version": 9}), tmp_path, "other"))
    assert not guard.policy_path(tmp_path).exists()
