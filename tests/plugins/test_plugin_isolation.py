"""The secret boundary of an isolated plugin's backend (criterion a4 of hub-agent-plugins):

- the environment a backend runs with is built from nothing, so none of the host's variables reach it, the hub's DSN,
  secrets key and S3 keys among them;
- a hostile backend, run with the hub's variables set in the host and a sibling plugin's secret in its config, reads
  none of them: not from its own environment, not from the host's or a sibling's /proc on Linux (every backend and the
  host are marked not dumpable), not from any file (a secret never touches disk);
- the ``initialize`` payload of one plugin carries only that plugin's config, never another's;
- when the host runs as root, each backend gets a user and group of its own (isolation ``uid``);
- a backend that crashes or hangs makes the call fail within its timeout, and the host starts a fresh one after a
  backoff.

The backends run as real subprocesses. ``tests/fixtures/plugins/{hostile,faulty,sample}`` hold them. These checks
need no hub, so they run without EVO_HUB_TEST_DSN.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from evo_agents.plugins import environment
from evo_agents.plugins.environment import backend_env, identity, isolation, placement, set_not_dumpable
from evo_agents.plugins.host import Backend, BackendUnavailable, Host
from evo_agents.plugins.manifest import read_manifest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plugins"
HUB_SECRETS = {
    "EVO_HUB_DSN": "postgresql://hub:evo-hub-secret-dsn@db/hub",
    "EVO_HUB_SECRETS_KEY": "SECRETKEY-evo-hub-secret-sealing-key-000000",
    "EVO_HUB_S3_SECRET_ACCESS_KEY": "evo-hub-secret-s3-key",
    "EVO_HUB_PLUGIN_HOST_TOKEN": "evo-hub-secret-host-token",
}
SIBLING_SECRET = "evo-sibling-secret-value"


def _manifest(name: str):
    return read_manifest((FIXTURES / name / "evo-plugin.json").read_bytes())


def _bundle(name: str) -> bytes:
    from evo_agents.plugins.bundle import pack

    return pack(FIXTURES / name).data


def _init(name: str, plugin_id: int = 1, config: dict | None = None) -> dict:
    return {
        "protocol": "plugin/1",
        "plugin": {"id": name, "version": "1.0.0"},
        "plugin_id": plugin_id,
        "hub": {"url": "http://127.0.0.1:9/", "token": "evp_test", "version": "0.0.0"},
        "config": {"instance": config or {}, "projects": {}},
        "grants": {},
    }


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


def test_plugin_hostile_backend_reads_no_hub_or_sibling_secret(tmp_path, monkeypatch):
    for name, value in HUB_SECRETS.items():
        monkeypatch.setenv(name, value)
    set_not_dumpable()  # on Linux, so the hostile backend cannot read this process's /proc either

    async def run() -> dict:
        host = Host(tmp_path)
        # A sibling plugin holds a secret in its config; the hostile one must not reach it.
        sibling = host.place(_manifest("sample"), _bundle("sample"), _init("sample", 2, {"api_key": SIBLING_SECRET}))
        await sibling.call("ping", {}, timeout=20)
        hostile = host.place(_manifest("hostile"), _bundle("hostile"), _init("hostile", 3, {"api_key": "its-own"}))
        try:
            result = await hostile.call("tool", {"tool": "probe", "arguments": {}, "caller": {"project": "p"}}, 20)
            return json.loads(result["content"][0]["text"])
        finally:
            await host.stop_all()

    report = asyncio.run(run())
    assert report["found"] == {}, report["found"]


# One plugin's config never reaches another


def test_plugin_isolation_initialize_is_per_plugin(tmp_path):
    host = Host(tmp_path)
    a = host.place(_manifest("sample"), _bundle("sample"), _init("sample", 2, {"api_key": "a-secret"}))
    b = host.place(_manifest("hostile"), _bundle("hostile"), _init("hostile", 3, {"api_key": "b-secret"}))
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


# A backend's /proc is unreadable to a same-user process (Linux)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc and PR_SET_DUMPABLE are Linux")
def test_plugin_isolation_backend_proc_is_unreadable(tmp_path):
    async def run() -> int:
        host = Host(tmp_path)
        backend = host.place(_manifest("sample"), _bundle("sample"), _init("sample"))
        await backend.call("ping", {}, timeout=20)
        pid = backend.process.pid
        with pytest.raises(PermissionError):
            Path(f"/proc/{pid}/environ").read_bytes()
        await host.stop_all()
        return pid

    assert asyncio.run(run()) > 0


# Crash and hang


def test_plugin_crash_fails_the_call_and_restarts_with_backoff(tmp_path):
    async def run() -> None:
        host = Host(tmp_path)
        backend = host.place(_manifest("faulty"), _bundle("faulty"), _init("faulty"))
        call = {"arguments": {}, "caller": {"project": "p"}}
        assert (await backend.call("tool", {"tool": "ok", **call}, 20))["content"][0]["text"] == "ok"
        first_pid = backend.process.pid
        with pytest.raises(BackendUnavailable):
            await backend.call("tool", {"tool": "crash", **call}, 20)
        assert backend.status.state == "backoff"
        assert backend.status.restarts == 1
        assert backend.status.retry_at is not None
        with pytest.raises(BackendUnavailable):  # still in backoff
            await backend.call("tool", {"tool": "ok", **call}, 20)
        backend.status.retry_at = time.time() - 1  # the backoff has passed
        assert (await backend.call("tool", {"tool": "ok", **call}, 20))["content"][0]["text"] == "ok"
        assert backend.process.pid != first_pid  # a fresh process after the crash
        await host.stop_all()

    asyncio.run(run())


def test_plugin_crash_hang_times_out_within_the_timeout(tmp_path):
    async def run() -> float:
        host = Host(tmp_path)
        backend = host.place(_manifest("faulty"), _bundle("faulty"), _init("faulty"))
        call = {"tool": "hang", "arguments": {}, "caller": {"project": "p"}}
        start = time.monotonic()
        with pytest.raises(BackendUnavailable):
            await backend.call("tool", call, timeout=1.0)
        elapsed = time.monotonic() - start
        assert backend.status.state == "backoff"
        await host.stop_all()
        return elapsed

    assert asyncio.run(run()) < 5.0


def test_plugin_isolation_a_backend_that_does_not_start_backs_off(tmp_path):
    async def run() -> None:
        host = Host(tmp_path)
        manifest = _manifest("sample")
        backend = Backend(tmp_path, manifest, b"not a bundle", _init("sample"))
        host.backends["sample"] = backend
        with pytest.raises(BackendUnavailable):
            await backend.call("ping", {}, 20)
        assert backend.status.state == "backoff"
        assert backend.status.error and backend.status.retry_at is not None

    asyncio.run(run())
