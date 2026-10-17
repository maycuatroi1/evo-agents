"""The secret boundary of an isolated plugin's backend (criterion a4 of hub-agent-plugins):

- the environment a backend runs with is built from nothing, so none of the host's variables reach it, the hub's DSN,
  secrets key and S3 keys among them;
- a hostile backend, run with the hub's variables set in the host and a sibling plugin's secret in its config, reads
  none of them: not from its own environment, not from a sibling backend's /proc on Linux, not from any file; under a
  user of its own it also reads no /proc of the host or of any other process, writes into no other plugin's
  directories and signals no other user's process (under the host's shared user, the host's /proc and that of the
  user's other processes stay readable, as docs/plugins.md says);
- the ``initialize`` payload of one plugin carries only that plugin's config, never another's;
- when the host runs as root, each backend gets a user and group of its own (isolation ``uid``); a host that is not
  root refuses to run a backend under the user it shares with the others, unless told it is a developer's machine;
- the hub token of a backend is minted at its start, so pulling the same plugins again restarts nothing;
- the requirements are installed from the bundle's wheels alone (``tests/plugins/test_plugin_isolation_requirements``
  has the file's checks), and the resource limits hold before the plugin's code runs;
- a backend that crashes or hangs makes the call fail within its timeout, and so does one whose start takes long;
  the host starts a fresh one after a backoff.

The backends run as real subprocesses. ``tests/fixtures/plugins/{hostile,faulty,sample}`` hold them. These checks
need no hub, so they run without EVO_HUB_TEST_DSN. Run as root (CI's plugin-host-root job, in the hub's image), the
same tests run in isolation uid; ``test_plugin_isolation_root`` adds what only root can show.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

from evo_agents.plugins import environment
from evo_agents.plugins import host as host_module
from evo_agents.plugins.environment import NPROC, backend_env, identity, isolation, placement
from evo_agents.plugins.host import SHARED_USER_REFUSED, Backend, BackendUnavailable, Host
from tests.plugins.backends import (
    HUB_SECRETS,
    SIBLING_SECRET,
    as_root,
    bundle,
    init,
    manifest,
    text,
    tool,
    wheeled_plugin,
)

LINUX = sys.platform.startswith("linux")


def shared(root: Path, **options) -> Host:
    """A host that may run backends under its own user when it is not root, as a developer's machine does."""
    return Host(root, allow_shared_user=True, **options)


# The environment a backend runs with


def test_plugin_isolation_backend_env_carries_no_host_variable(tmp_path, monkeypatch):
    for name, value in HUB_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    where = placement(tmp_path, "sample", "1.0.0")
    env = backend_env(where)
    assert not ({name for name in HUB_SECRETS} & set(env))
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert set(env) <= {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "VIRTUAL_ENV",
        "EVO_PLUGIN",
        "EVO_PLUGIN_VERSION",
        "EVO_PLUGIN_PROTOCOL",
    }
    assert env["HOME"] == str(where.data)


# The hostile backend reads nothing


def test_plugin_hostile_backend_reads_no_hub_or_sibling_secret(plugin_root, monkeypatch):
    for name, value in HUB_SECRETS.items():
        monkeypatch.setenv(name, value)

    async def run() -> tuple[dict, set[str]]:
        host = shared(plugin_root)
        # A sibling plugin holds a secret in its config; the hostile one must not reach it.
        sibling = host.place(manifest("sample"), bundle("sample"), init("sample", 2, {"api_key": SIBLING_SECRET}))
        await sibling.call("ping", {}, timeout=20)
        hostile = host.place(manifest("hostile"), bundle("hostile"), init("hostile", 3, {"api_key": "its-own"}))
        try:
            report = json.loads(text(await hostile.call("tool", tool("probe"), 20)))
            return report, {str(sibling.process.pid), str(hostile.process.pid)}
        finally:
            await host.stop_all()

    report, backends = asyncio.run(run())
    found = report["found"]
    if isolation() == "uid":  # a user of its own: no other plugin's files, no other user's processes
        assert found == {}, found
        assert report["wrote"] == {} and report["signalled"] == {}, report
        assert report["uid"] == environment.UID_BASE + 3
        return

    # Isolation process (a developer's machine) shares the host's user, so the /proc of the host and of every other
    # process of that user is readable, as docs/plugins.md says; on CI that is the test runner and its xdist workers.
    # What it still promises: nothing in the backend's own environment, no secret in a file, no backend's /proc (each
    # is not dumpable), and a plugin's config in no process's environment or command line.
    def readable(where: str) -> bool:  # proc:<pid>/<leaf> of a process of the user that is no backend
        return where.startswith("proc:") and where.removeprefix("proc:").split("/")[0] not in backends

    assert {where: value for where, value in found.items() if not readable(where)} == {}, found
    assert not [where for where, value in found.items() if SIBLING_SECRET in value], found


# One plugin's config never reaches another


def test_plugin_isolation_initialize_is_per_plugin(tmp_path):
    host = shared(tmp_path)
    a = host.place(manifest("sample"), bundle("sample"), init("sample", 2, {"api_key": "a-secret"}))
    b = host.place(manifest("hostile"), bundle("hostile"), init("hostile", 3, {"api_key": "b-secret"}))
    assert a.init["config"]["instance"] == {"api_key": "a-secret"}
    assert "b-secret" not in json.dumps(a.init)
    assert "a-secret" not in json.dumps(b.init)


# The uid a backend runs under


def test_plugin_isolation_uid_identity_under_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    first, second = identity(5), identity(6)
    assert first is not None and second is not None
    assert (first.uid, first.gid) == (environment.UID_BASE + 5, environment.UID_BASE + 5)
    assert first.uid != second.uid
    assert isolation() == "uid"


def test_plugin_isolation_process_identity_without_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)
    assert identity(5) is None
    assert isolation() == "process"


@pytest.mark.skipif(as_root(), reason="as root every backend gets a user of its own")
def test_plugin_isolation_a_host_not_root_refuses_to_share_its_user(plugin_root):
    async def run() -> Backend:
        host = Host(plugin_root)  # no allow_shared_user: what the plugins service runs with
        backend = host.place(manifest("sample"), bundle("sample"), init("sample"))
        with pytest.raises(BackendUnavailable):
            await backend.call("ping", {}, timeout=20)
        await host.stop_all()
        return backend

    backend = asyncio.run(run())
    assert backend.status.state == "backoff" and backend.status.error == SHARED_USER_REFUSED
    assert backend.process is None and not backend.where.code.exists()  # nothing of the plugin was even placed


# A backend's /proc is unreadable to a same-user process (Linux)


@pytest.mark.skipif(not LINUX, reason="/proc and PR_SET_DUMPABLE are Linux")
def test_plugin_isolation_backend_proc_is_unreadable(plugin_root):
    async def run() -> int:
        host = shared(plugin_root)
        backend = host.place(manifest("sample"), bundle("sample"), init("sample"))
        await backend.call("ping", {}, timeout=20)
        pid = backend.process.pid
        with pytest.raises(PermissionError):
            Path(f"/proc/{pid}/environ").read_bytes()
        await host.stop_all()
        return pid

    assert asyncio.run(run()) > 0


# The token, minted at each start


def test_plugin_isolation_token_is_minted_at_each_start_and_a_pull_again_restarts_nothing(plugin_root):
    minted: list[str] = []

    async def mint(name: str) -> str:
        minted.append(name)
        return f"evp_minted-{len(minted)}"

    async def run() -> None:
        host = shared(plugin_root, mint=mint)
        pulled = init("faulty")
        del pulled["hub"]["token"]  # the hub sends no token in what the host pulls
        backend = host.place(manifest("faulty"), bundle("faulty"), pulled)
        assert text(await backend.call("tool", tool("token"), 20)) == "evp_minted-1"
        pid = backend.process.pid
        again = host.place(manifest("faulty"), bundle("faulty"), json.loads(json.dumps(pulled)))
        assert again is backend and backend.process.pid == pid and minted == ["faulty"]  # nothing restarted
        changed = {**pulled, "config": {"instance": {"greeting": "hi"}, "projects": {}}}
        replaced = host.place(manifest("faulty"), bundle("faulty"), changed)
        assert replaced is not backend
        assert text(await replaced.call("tool", tool("token"), 20)) == "evp_minted-2"
        await host.stop_all()

    asyncio.run(run())


# The limits hold before the plugin's code runs (Linux)


@pytest.mark.skipif(not LINUX, reason="the trampoline sets resource limits on Linux")
def test_plugin_isolation_limits_hold_before_the_plugin_runs(plugin_root):
    async def run() -> dict:
        host = shared(plugin_root)
        backend = host.place(manifest("faulty"), bundle("faulty"), init("faulty", 7))
        try:
            return json.loads(text(await backend.call("tool", tool("limits"), 20)))
        finally:
            await host.stop_all()

    limits = asyncio.run(run())
    assert limits["core"] == 0 and limits["nofile"] <= environment.LIMITS["nofile"]
    assert limits["as"] <= environment.LIMITS["as"]
    if isolation() == "uid":
        assert limits["nproc"] == NPROC


# Requirements from the bundle's wheels only


@pytest.mark.skipif(importlib.util.find_spec("pip") is None, reason="the host installs requirements with pip")
def test_plugin_isolation_requirements_install_from_the_bundles_wheels(plugin_root, tmp_path):
    directory = wheeled_plugin(tmp_path / "wheeled")

    async def run() -> tuple[dict, Backend]:
        host = shared(plugin_root)
        backend = host.place(manifest("wheeled", directory), bundle("wheeled", directory), init("wheeled", 8))
        try:
            return json.loads(text(await backend.call("tool", tool("probe"), 120))), backend
        finally:
            await host.stop_all()

    report, backend = asyncio.run(run())
    assert report["value"] == "from-the-wheel"
    assert report["module"].startswith(str(backend.where.env))
    assert report["pth_uid"] == str(report["uid"])  # the wheel's .pth first ran under the backend's user


# Crash, hang and a slow start


def test_plugin_crash_fails_the_call_and_restarts_with_backoff(plugin_root):
    async def run() -> None:
        host = shared(plugin_root)
        backend = host.place(manifest("faulty"), bundle("faulty"), init("faulty"))
        assert text(await backend.call("tool", tool("ok"), 20)) == "ok"
        first_pid = backend.process.pid
        with pytest.raises(BackendUnavailable):
            await backend.call("tool", tool("crash"), 20)
        assert backend.status.state == "backoff"
        assert backend.status.restarts == 1
        assert backend.status.retry_at is not None
        with pytest.raises(BackendUnavailable):  # still in backoff
            await backend.call("tool", tool("ok"), 20)
        backend.status.retry_at = time.time() - 1  # the backoff has passed
        assert text(await backend.call("tool", tool("ok"), 20)) == "ok"
        assert backend.process.pid != first_pid  # a fresh process after the crash
        await host.stop_all()

    asyncio.run(run())


def test_plugin_crash_hang_times_out_within_the_timeout(plugin_root):
    async def run() -> float:
        host = shared(plugin_root)
        backend = host.place(manifest("faulty"), bundle("faulty"), init("faulty"))
        await backend.call("ping", {}, timeout=20)
        start = time.monotonic()
        with pytest.raises(BackendUnavailable):
            await backend.call("tool", tool("hang"), timeout=1.0)
        elapsed = time.monotonic() - start
        assert backend.status.state == "backoff"
        await host.stop_all()
        return elapsed

    assert asyncio.run(run()) < 5.0


def test_plugin_crash_slow_start_never_holds_a_call_past_its_timeout(plugin_root, monkeypatch):
    real = host_module.build_env

    def slow_build(*args, **kwargs):  # a requirements install that takes its time
        time.sleep(2.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(host_module, "build_env", slow_build)

    async def run() -> float:
        host = shared(plugin_root)
        backend = host.place(manifest("faulty"), bundle("faulty"), init("faulty"))
        start = time.monotonic()
        with pytest.raises(BackendUnavailable, match="still starting"):
            await backend.call("ping", {}, timeout=0.5)
        elapsed = time.monotonic() - start
        assert backend.status.state == "placing"
        assert (await backend.call("ping", {}, timeout=20)) == {}  # the start went on and finished
        await host.stop_all()
        return elapsed

    assert asyncio.run(run()) < 1.5


def test_plugin_isolation_a_backend_that_does_not_start_backs_off(plugin_root):
    async def run() -> None:
        host = shared(plugin_root)
        backend = Backend(plugin_root, manifest("sample"), b"not a bundle", init("sample"), allow_shared_user=True)
        host.backends["sample"] = backend
        with pytest.raises(BackendUnavailable):
            await backend.call("ping", {}, 20)
        assert backend.status.state == "backoff"
        assert backend.status.error and backend.status.retry_at is not None

    asyncio.run(run())


# A placing that a stop cancels, and what another process of the plugin's user may plant meanwhile


def test_plugin_isolation_ready_marker_is_never_written_through_a_link(tmp_path):
    """The review's second L2: a link planted where the host writes a directory's marker would have made root write
    the marker into the file it points to. The marker is created, never opened through a link."""
    victim = tmp_path / "victim"
    victim.write_text("intact")
    filled = tmp_path / ".venv-planted"
    filled.mkdir()
    (filled / environment.READY).symlink_to(victim)
    with pytest.raises(environment.PlacementError, match="did not write"):
        environment._replace(tmp_path / "env", filled, "stamp")
    assert victim.read_text() == "intact" and not (tmp_path / "env").exists()


def test_plugin_crash_stop_waits_for_the_placing_it_cancels(plugin_root, monkeypatch):
    """Cancelling a start left its placing running in its thread, writing into the plugin's directories after stop
    returned and while the backend that replaces it, under the same user, started. stop now waits for it."""
    real, started, finished = host_module.build_env, [], []

    def slow_build(*args, **kwargs):
        started.append(time.monotonic())
        time.sleep(1.5)
        real(*args, **kwargs)
        finished.append(time.monotonic())

    monkeypatch.setattr(host_module, "build_env", slow_build)

    async def run() -> Backend:
        host = shared(plugin_root)
        backend = host.place(manifest("faulty"), bundle("faulty"), init("faulty", 12))
        backend.kick()
        while not started:
            await asyncio.sleep(0.05)
        await backend.stop()
        assert finished, "stop returned while the placing it cancelled still ran"
        await host.stop_all()
        return backend

    backend = asyncio.run(run())
    assert backend.process is None and backend._building.done()


def test_plugin_isolation_start_refused_while_processes_of_its_user_linger(plugin_root, monkeypatch):
    """A start killed what lingered under the plugin's user and went on whatever the kill said. It now refuses to place
    anything or start a backend beside a process of that user."""
    user = environment.UID_BASE + 13
    kills = []

    async def cannot_kill(owner) -> bool:
        kills.append(owner.uid)
        return False

    monkeypatch.setattr(host_module, "identity", lambda plugin_id: environment.Identity(user, user))
    monkeypatch.setattr(host_module, "kill_user", cannot_kill)

    async def run() -> Backend:
        host = Host(plugin_root)
        backend = host.place(manifest("sample"), bundle("sample"), init("sample", 13))
        with pytest.raises(BackendUnavailable):
            await backend.call("ping", {}, timeout=20)
        await host.stop_all()
        return backend

    backend = asyncio.run(run())
    assert backend.status.state == "backoff"
    assert backend.status.error == host_module.LINGERING.format(name="sample", uid=user)
    assert kills and set(kills) == {user}
    assert backend.process is None and not backend.where.code.exists()  # nothing was placed
