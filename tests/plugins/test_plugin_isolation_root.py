"""The secret boundary as production runs it: the plugin host as root with only the capabilities SETUID, SETGID, CHOWN
and KILL, each backend under a user of its own (isolation uid). Criterion a4 of hub-agent-plugins, and the review's
C1, H1 and M2, which the tests run as a user on macOS never reached.

These run only as root on Linux. CI's plugin-host-root job runs them in the hub's image with the plugins service's
user, capabilities, no-new-privileges and init, the hub's variables set on the container (so docker-init, pid 1, holds
them as it holds EVO_HUB_PLUGIN_HOST_TOKEN in production) and a fresh named volume at /plugins
($EVO_PLUGIN_HOST_TEST_ROOT), as deploy/hub/docker-compose.yml mounts one:

    docker run --rm --init --user 0:0 --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add CHOWN \\
      --cap-add KILL --security-opt no-new-privileges:true -e EVO_PLUGIN_HOST_TEST_ROOT=/plugins ... \\
      -v plugins:/plugins -v "$PWD:/src:ro" -w /src evo-agents-hub:ci python -m pytest tests/plugins

- the hostile backend reads no secret, writes into no other plugin's code, environment or data directory, and may
  signal no process of another user: not the host, not docker-init, not the sibling backend;
- a backend's data directory, which its user owns, is prepared again at the next start without CAP_FOWNER (H1);
- pip runs under the backend's user and installs only the bundle's wheels; the environment is root's afterwards (C1);
- stopping a backend kills every process of its user, a child that left the group with setsid() included (M2);
- the limits of a backend's user hold;
- a link another process of the backend's user plants in the environment pip fills is removed, never followed, and
  the modes there are reset; stopping a backend kills pip and waits for the placing it cancelled (the second L2).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evo_agents.plugins import environment
from evo_agents.plugins.environment import NPROC, UID_BASE
from evo_agents.plugins.host import Host
from evo_agents.plugins.process import processes_of
from tests.plugins.backends import (
    HUB_SECRETS,
    ROOT_VARIABLE,
    SIBLING_SECRET,
    as_root,
    bundle,
    init,
    manifest,
    text,
    tool,
    wheeled_plugin,
)

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and as_root()), reason="isolation uid: the host as root on Linux"
)
COMPOSE_CAPABILITIES = {0: "CHOWN", 5: "KILL", 6: "SETGID", 7: "SETUID"}  # bits of CapEff (linux/capability.h)


def _capabilities() -> set[int]:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("CapEff:"):
            value = int(line.split()[1], 16)
            return {bit for bit in range(64) if value >> bit & 1}
    return set()


def _mode(path: Path) -> tuple[int, int, int]:
    info = os.lstat(path)
    return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)


def _mode_as(user: int, path: Path) -> tuple[int, int, int]:
    """``_mode`` of a path inside a directory of ``user``, which root without CAP_DAC_READ_SEARCH cannot look into."""
    code = "import os, stat, sys; i = os.lstat(sys.argv[1]); print(i.st_uid, i.st_gid, stat.S_IMODE(i.st_mode))"
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(path)],
        user=user,
        group=user,
        extra_groups=[],
        cwd="/",
        capture_output=True,
        text=True,
        check=True,
    )
    uid, gid, mode = (int(part) for part in done.stdout.split())
    return uid, gid, mode


@pytest.mark.skipif(not os.environ.get(ROOT_VARIABLE), reason="the CI job's container, with the compose capabilities")
def test_plugin_isolation_root_runs_with_the_compose_capabilities_only():
    assert _capabilities() == set(COMPOSE_CAPABILITIES), "run as the plugins service runs: SETUID SETGID CHOWN KILL"


def test_plugin_hostile_root_backend_reads_writes_and_signals_nothing(plugin_root, monkeypatch):
    for name, value in HUB_SECRETS.items():
        monkeypatch.setenv(name, value)

    async def run() -> tuple[dict, list]:
        host = Host(plugin_root)  # what the plugins service runs: no shared user
        sibling = host.place(manifest("sample"), bundle("sample"), init("sample", 2, {"api_key": SIBLING_SECRET}))
        await sibling.call("ping", {}, timeout=30)
        hostile = host.place(manifest("hostile"), bundle("hostile"), init("hostile", 3, {"api_key": "its-own"}))
        try:
            return json.loads(text(await hostile.call("tool", tool("probe"), 60))), host.status()
        finally:
            await host.stop_all()

    report, status = asyncio.run(run())
    assert report["found"] == {} and report["wrote"] == {} and report["signalled"] == {}, report
    assert report["uid"] == UID_BASE + 3
    assert {item["isolation"] for item in status} == {"uid"}
    environ = Path("/proc/1/environ")
    if os.environ.get(ROOT_VARIABLE) and environ.exists():  # the secret was there to take, in docker-init's environ
        assert b"evo-hub-secret" in environ.read_bytes()


def test_plugin_isolation_root_prepares_its_users_data_again_without_fowner(plugin_root):
    async def run() -> Path:
        host = Host(plugin_root)
        backend = host.place(manifest("faulty"), bundle("faulty"), init("faulty", 4))
        assert text(await backend.call("tool", tool("ok"), 30)) == "ok"
        await backend.stop()  # the data directory is the plugin's user's now
        assert text(await backend.call("tool", tool("ok"), 30)) == "ok"  # and is prepared again, as root
        await host.stop_all()
        return backend

    backend = asyncio.run(run())
    user = UID_BASE + 4
    assert _mode(backend.where.data) == (user, user, 0o700)
    assert _mode_as(user, backend.where.data / "tmp") == (user, user, 0o700)
    assert _mode(backend.where.code) == (0, user, 0o750)
    assert _mode(backend.where.env) == (0, user, 0o750)


@pytest.mark.skipif(importlib.util.find_spec("pip") is None, reason="the host installs requirements with pip")
def test_plugin_isolation_root_pip_runs_as_the_backends_user_and_root_owns_what_it_wrote(
    plugin_root, tmp_path, monkeypatch
):
    directory = wheeled_plugin(tmp_path / "wheeled")
    written: dict[str, int] = {}  # who owned what pip wrote, as the host takes each path back
    entries, take_back, taking = environment._entries, environment._take_back, []

    def watched(tree):
        for path in entries(tree):
            if taking and path.name.startswith("evo_probe"):
                written[path.name] = os.lstat(path).st_uid
            yield path

    def recording(tree, owner):
        taking.append(True)
        try:
            take_back(tree, owner)
        finally:
            taking.clear()

    monkeypatch.setattr(environment, "_entries", watched)
    monkeypatch.setattr(environment, "_take_back", recording)

    async def run():
        host = Host(plugin_root)
        backend = host.place(manifest("wheeled", directory), bundle("wheeled", directory), init("wheeled", 8))
        try:
            return json.loads(text(await backend.call("tool", tool("probe"), 180))), backend
        finally:
            await host.stop_all()

    report, backend = asyncio.run(run())
    user = UID_BASE + 8
    assert report["value"] == "from-the-wheel" and report["uid"] == user
    assert report["pth_uid"] == str(user)  # the wheel's code first ran as the backend, never as root
    assert written and set(written.values()) == {user}, f"pip wrote as root: {written}"
    owners = {os.lstat(path).st_uid for path in backend.where.env.rglob("*")}
    assert owners == {0}, "pip's files are root's again, read-only to the backend"


def test_plugin_crash_root_stop_kills_every_process_of_the_plugins_user(plugin_root):
    async def run() -> tuple[int, int]:
        host = Host(plugin_root)
        backend = host.place(manifest("faulty"), bundle("faulty"), init("faulty", 5))
        orphan = int(text(await backend.call("tool", tool("orphan"), 30)))
        assert orphan in processes_of(UID_BASE + 5)
        main = backend.process.pid
        await host.stop_all()
        return orphan, main

    orphan, main = asyncio.run(run())
    assert processes_of(UID_BASE + 5) == [], "the setsid() child outlived its backend"
    assert orphan != main


def test_plugin_isolation_root_limits_count_the_plugins_user(plugin_root):
    async def run() -> dict:
        host = Host(plugin_root)
        backend = host.place(manifest("faulty"), bundle("faulty"), init("faulty", 6))
        try:
            return json.loads(text(await backend.call("tool", tool("limits"), 30)))
        finally:
            await host.stop_all()

    limits = asyncio.run(run())
    assert limits["nproc"] == NPROC and limits["core"] == 0


# What another process of a plugin's user may plant in the environment pip fills (the review's second L2)

PLANT = """
import os, sys, time
filled, victim, signal, seconds = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
os.symlink(victim, os.path.join(filled, ".evo-ready"))
wide = os.path.join(filled, "wide")
os.mkdir(wide)
os.chmod(wide, 0o777)
for name, mode in (("plain", 0o666), ("setid", 0o6777)):
    with open(os.path.join(wide, name), "w") as handle:
        handle.write("x")
    os.chmod(os.path.join(wide, name), mode)
os.symlink(victim, os.path.join(wide, "link"))
closed = os.path.join(filled, "closed")
os.mkdir(closed)
open(os.path.join(closed, "inner"), "w").close()
os.chmod(closed, 0)
open(signal, "w").close()
time.sleep(seconds)
"""


def _planting(monkeypatch, victim: Path, signal: Path, seconds: float) -> None:
    """pip's step of building an environment runs PLANT instead, under the backend's user as pip runs: what another
    process of that user, the backend that replaces this one, could do to the environment while pip fills it."""
    real = environment._run

    def run(command, doing, where, cwd, owner=None):
        if owner is not None:
            filled = Path(command[command.index("-r") + 1]).parent
            command = [sys.executable, "-I", "-S", "-c", PLANT, str(filled), str(victim), str(signal), str(seconds)]
        return real(command, doing, where, cwd, owner)

    monkeypatch.setattr(environment, "_run", run)


def _victim_and_signal(root: Path) -> tuple[Path, Path]:
    """A file root may write, which a planted link points to, and where PLANT says it has planted."""
    victim = root / "victim"
    victim.write_text("intact")
    victim.chmod(0o644)
    signals = root / "signals"
    signals.mkdir()
    signals.chmod(0o1777)
    return victim, signals / "planted"


def test_plugin_hostile_root_links_planted_in_the_environment_are_removed_never_followed(
    plugin_root, tmp_path, monkeypatch
):
    directory = wheeled_plugin(tmp_path / "wheeled")
    where = environment.placement(plugin_root, "wheeled", "1.0.0")
    user = UID_BASE + 11
    owner = environment.Identity(user, user)
    victim, signal = _victim_and_signal(plugin_root)
    _planting(monkeypatch, victim, signal, 0)
    environment.extract(bundle("wheeled", directory), where.code, owner)
    environment.prepare_data(where, owner)
    environment.build_env(where, "backend/requirements.txt", owner)

    assert signal.exists(), "PLANT did not run"
    assert victim.read_text() == "intact", "root wrote through a link the backend's user planted"
    marker = where.env / environment.READY
    assert not marker.is_symlink() and _mode(marker)[0] == 0
    assert not (where.env / "wide" / "link").exists() and not (where.env / "wide" / "link").is_symlink()
    assert _mode(where.env / "wide") == (0, 0, 0o755)
    assert _mode(where.env / "wide" / "plain") == (0, 0, 0o644)  # no write but root's
    assert not (where.env / "wide" / "setid").exists()  # pip writes no set-id file, and root may not chown one
    assert _mode(where.env / "closed") == (0, 0, 0o755)
    assert _mode(where.env / "closed" / "inner")[0] == 0  # the walk went on into what was mode 000
    assert (where.env / "bin" / "python").is_symlink()  # the environment's own links stay, root's
    assert {os.lstat(path).st_uid for path in where.env.rglob("*")} == {0}


def test_plugin_crash_root_stop_kills_pip_and_waits_for_the_placing(plugin_root, tmp_path, monkeypatch):
    directory = wheeled_plugin(tmp_path / "wheeled")
    user = UID_BASE + 14
    victim, signal = _victim_and_signal(plugin_root)
    _planting(monkeypatch, victim, signal, 120)
    taken, take_back = [], environment._take_back

    def recording(tree, owner):
        take_back(tree, owner)
        taken.append(tree)

    monkeypatch.setattr(environment, "_take_back", recording)

    async def run():
        host = Host(plugin_root)
        backend = host.place(manifest("wheeled", directory), bundle("wheeled", directory), init("wheeled", 14))
        backend.kick()
        deadline = time.monotonic() + 60
        while not signal.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert signal.exists(), "pip's step never ran"
        started = time.monotonic()
        await backend.stop()
        elapsed = time.monotonic() - started
        assert taken, "stop returned before the placing it cancelled took the environment back"
        assert backend._building.done() and processes_of(user) == []
        await host.stop_all()
        return backend, elapsed

    backend, elapsed = asyncio.run(run())
    assert elapsed < 30, "pip was not killed: stop waited for it to end on its own"
    assert victim.read_text() == "intact"
    envs = backend.where.env.parent
    assert {os.lstat(path).st_uid for path in envs.rglob("*")} == {0}
    assert not any(path.is_symlink() and path.name == environment.READY for path in envs.rglob("*"))
