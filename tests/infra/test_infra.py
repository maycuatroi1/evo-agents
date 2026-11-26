"""evo_agents.infra: Git, the atomic JSON file and RetryPolicy."""

import asyncio
import os
import subprocess
import sys

import pytest

from evo_agents.infra import (
    Backoff,
    Exponential,
    Fixed,
    Git,
    GitFailed,
    GitTimeout,
    GitUnavailable,
    JsonFile,
    RetryPolicy,
    Steps,
    write_atomic,
)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    return tmp_path


def test_git_runs_in_its_directory_and_unpacks_as_a_tuple(repo):
    git = Git(repo, timeout=30)
    code, out, err = git.run("rev-parse", "--show-toplevel")
    assert code == 0 and os.path.samefile(out.strip(), repo) and err == ""
    assert git.value("symbolic-ref", "--short", "HEAD") == "main"
    assert git.read("rev-parse", "--verify", "--quiet", "HEAD") is None
    assert not git.ok("rev-parse", "--verify", "--quiet", "HEAD")


def test_a_failed_git_is_typed_and_names_the_command(repo):
    with pytest.raises(GitFailed) as caught:
        Git(repo).run("no-such-command")
    assert caught.value.command == "no-such-command" and caught.value.returncode != 0
    assert str(caught.value).startswith("git no-such-command failed")
    assert Git(repo).run("no-such-command", check=False).returncode != 0


def test_git_that_takes_too_long_or_cannot_run(repo, monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(GitTimeout) as caught:
        Git(repo, timeout=1).run("status")
    assert caught.value.timeout == 1

    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "no git")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(GitUnavailable):
        Git(repo).run("status")


def test_a_subclass_raises_its_own_error_through_fail(repo):
    class Mine(Git):
        def fail(self, error):
            return ValueError(f"mine: {error.command}")

    with pytest.raises(ValueError, match="mine: no-such-command"):
        Mine(repo).run("no-such-command")


def test_async_git(repo):
    async def go():
        git = Git(repo, timeout=30)
        code, out, _ = await git.arun("symbolic-ref", "--short", "HEAD")
        with pytest.raises(GitFailed):
            await git.arun("no-such-command")
        return code, out

    assert asyncio.run(go()) == (0, "main\n")


def test_json_file_reads_a_default_and_writes_atomically(tmp_path):
    path = tmp_path / "deep" / "state.json"
    file = JsonFile(path, newline=True, indent=1, sort_keys=True)
    assert file.read() is None and file.read_object() == {}
    file.write({"b": 1, "a": "é"})
    assert path.read_text(encoding="utf-8") == '{\n "a": "\\u00e9",\n "b": 1\n}\n'
    assert file.read_object() == {"a": "é", "b": 1}
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("[1]")
    assert file.read_object() == {} and file.read(accept=lambda data: isinstance(data, list)) == [1]
    path.write_text("not json")
    assert file.read("default") == "default"
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_write_atomic_keeps_the_old_file_when_the_rename_fails(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_bytes(b"old")

    def refuse(src, dst):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PermissionError):
        write_atomic(path, b"new", 0o644)
    assert path.read_bytes() == b"old" and [p.name for p in tmp_path.iterdir()] == ["a.txt"]


def test_the_delays():
    assert [Exponential(1, 5)(n) for n in range(5)] == [1, 2, 4, 5, 5]
    assert Exponential(1, 10, 0.5, lambda low, high: high)(1) == 3
    assert Exponential(1, float("inf"))(10_000) == 2.0**32
    assert [Steps([1, 2])(n) for n in range(4)] == [1, 2, 2, 2]
    assert Steps([1, 2], 9)(5) == 9 and Fixed(3)(7) == 3
    backoff = Backoff(Exponential(1, 8))
    assert [backoff.next() for _ in range(3)] == [1, 2, 4] and backoff.failures == 3
    backoff.reset()
    assert backoff.next() == 1


def test_retry_policy_tries_again_only_what_it_takes():
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OSError("busy")
        return "done"

    policy = RetryPolicy(Exponential(1, 60), tries=3, retry_on=(OSError,))
    assert policy.call(flaky, sleep=waits.append) == "done" and waits == [1, 2]

    calls.clear()
    with pytest.raises(OSError):
        RetryPolicy(Fixed(0), tries=2, retry_on=(OSError,)).call(flaky, sleep=waits.append)
    assert len(calls) == 2

    def wrong():
        calls.append(1)
        raise ValueError("no")

    calls.clear()
    with pytest.raises(ValueError):
        RetryPolicy(tries=5, retry_on=lambda exc: isinstance(exc, OSError)).call(wrong, sleep=waits.append)
    assert len(calls) == 1


def test_retry_policy_async_never_retries_a_cancellation():
    calls = []

    async def cancelled():
        calls.append(1)
        raise asyncio.CancelledError

    async def nap(seconds):
        pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(RetryPolicy(tries=5, retry_on=(BaseException,)).acall(cancelled, sleep=nap))
    assert calls == [1]
