"""The worker daemon updates its install to the tag nightly while idle, unless its version is locked (criteria a2,
a3 and a4 of the auto-update-nightly plan), in this process against the in-memory hub of ``tests.worker.fake_hub``
and the ``uv`` of ``tests.worker.fake_uv``, without Postgres.

- What the daemon reads of its install, of its service manager and of the tag, and the two locks of the daemons of
  one install.
- The update: staged first, then over the install, each started and its commit checked; a failure leaves the install
  as it was, or puts its copy back, with a uv that updates an environment in place (0.6.13) and with one that deletes
  and builds it again (0.12.24); when the copy cannot go back, the old version is installed again.
- The daemon: an idle, unlocked daemon under a service manager installs nightly and exits ``EXIT_UPDATED``; a locked
  one, one that has not heard the lock, one run by hand or one that holds a run does not; a failed update is reported
  and tried again later while runs go on; a hub that answers 426 gets an update first when the lock kept in
  config.json says unlocked; two daemons of one install update once, after both are idle, and both restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker.daemon import Daemon
from evo_agents.worker.home import EXIT_UPDATED, PidLock, WorkerConfig, WorkerHome
from evo_agents.worker.selfupdate import backup as backup_module
from evo_agents.worker.selfupdate import installer as installer_module
from evo_agents.worker.selfupdate.install import find_uv, read_install, repo_of
from evo_agents.worker.selfupdate.installer import Done, Installer, api_commit, nightly_commit, parse_ls_remote
from evo_agents.worker.selfupdate.peers import Peers
from evo_agents.worker.selfupdate.supervisor import BY_HAND, Supervision, detect, launchd, systemd
from evo_agents.worker.selfupdate.updater import LOCK_UNKNOWN, SelfUpdate
from tests.worker import fake_adapter
from tests.worker.fake_hub import TOKEN, FakeHub
from tests.worker.fake_uv import install_script, write_install
from tests.worker.test_plan_runs import PLAN, PROJECT, machine, plan_body, wait_for  # noqa: F401 (machine)

OLD = "a" * 40
NEW = "b" * 40
NEWER = "c" * 40
PYTHON = "/opt/python/bin/python3.12"
SUPERVISED = Supervision("systemd", "evo-agents-worker.service")
UNLOCKED = {"slots": 1, "name": "mac", "labels": [], "version_locked": False}
LOCKED = {**UNLOCKED, "version_locked": True}
NIGHTLY_REQUIREMENT = "evo-ak[worker] @ git+https://github.com/maycuatroi1/evo-agents@nightly"


class Fake:
    """A uv tool install of evo-ak at OLD, the fake uv that installs over it, and where nightly points. With
    ``recreates`` the fake uv deletes the environment and its command before each install, as uv 0.12.24 does."""

    def __init__(self, tmp: Path, *, recreates: bool = False):
        self.tmp = tmp
        self.tools, self.bin = tmp / "uv-tools", tmp / "uv-bin"
        self.uv = install_script(tmp / "uv-cmd" / "uv")
        self.scenario, self.log = tmp / "uv-scenario.json", tmp / "uv-log.jsonl"
        self.prefix = write_install(self.tools, self.bin, version="0.22.0", commit=OLD)
        self.nightly = NEW
        self.recreates = recreates
        self.lookups = 0
        self.lookup_error: str | None = None
        self.now = 0.0  # the clock of the updaters that take ``clock=fake.clock``
        self.steps([])

    def steps(self, steps: list[str]) -> None:
        scenario = {"nightly": self.nightly, "recreates": self.recreates, "steps": steps}
        self.scenario.write_text(json.dumps(scenario), encoding="utf-8")

    def point(self, commit: str) -> None:
        """Move nightly to ``commit``, for the lookup and for the fake uv."""
        scenario = json.loads(self.scenario.read_text(encoding="utf-8"))
        self.nightly = commit
        self.scenario.write_text(json.dumps({**scenario, "nightly": commit}), encoding="utf-8")

    def clock(self) -> float:
        return self.now

    def later(self) -> None:
        """Past the next read of the tag and the next try of a failed update."""
        self.now += 10_000.0

    def env(self) -> dict[str, str]:
        base = {key: value for key, value in os.environ.items() if not key.startswith("EVO_")}
        return {**base, "FAKE_UV_SCENARIO": str(self.scenario), "FAKE_UV_LOG": str(self.log)}

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def install(self):
        return read_install(self.prefix, PYTHON)

    def installer(self, **options) -> Installer:
        return Installer(self.install(), str(self.uv), self.tmp / "staging", self.env(), **options)

    async def probe(self) -> bool:
        """Whether the install starts, as the update checks it."""
        problem, _ = await self.installer().check(self.prefix, self.bin / "evo-agents")
        return problem is None

    def starts(self) -> bool:
        return asyncio.run(self.probe())

    async def lookup(self) -> str:
        self.lookups += 1
        if self.lookup_error:
            raise LookupError(self.lookup_error)
        return self.nightly

    def updater(self, home: WorkerHome, *, supervision=SUPERVISED, **options) -> SelfUpdate:
        install = self.install()
        peers = Peers(self.tmp / "installs" / "one", home.root)
        installer = Installer(install, str(self.uv), peers.staging, self.env())
        options = {"idle_wait": 10.0, **options}
        return SelfUpdate(
            install, supervision=supervision, peers=peers, installer=installer, lookup=self.lookup, **options
        )


@pytest.fixture
def fake(tmp_path) -> Fake:
    return Fake(tmp_path)


@pytest.fixture(params=[False, True], ids=["uv-in-place", "uv-recreates"])
def any_uv(request, tmp_path) -> Fake:
    """The install, under a uv that updates an environment in place (0.6.13) and under one that deletes it and builds
    it again on every install (0.12.24)."""
    return Fake(tmp_path, recreates=request.param)


def records(caplog, message: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == message]


def states(hub: FakeHub) -> list[dict]:
    return [body.get("self_update") or {} for body in hub.heartbeat_bodies]


async def until_heartbeat(hub: FakeHub, predicate, what: str) -> dict:
    await wait_for(
        lambda: hub.last_heartbeat is not None and predicate(hub.last_heartbeat.get("self_update") or {}), what
    )
    return hub.last_heartbeat["self_update"]


async def ended(task: asyncio.Task, timeout: float = 30.0) -> int:
    done, _ = await asyncio.wait({task}, timeout=timeout)
    assert done, "the daemon did not stop"
    return task.result()


def run_daemon(machine, body, *, updater, settings=UNLOCKED, stored_lock=None, outdated=False, worker_id=1):  # noqa: F811
    """``await body(hub, daemon, task)`` with the daemon of ``machine`` running ``updater(home)`` against a fake hub
    whose heartbeat answers carry ``settings``; the daemon is stopped as SIGTERM does when ``body`` leaves it
    running. Returns its exit status."""

    async def go() -> int:
        hub = FakeHub(PROJECT)
        hub.put_plan(plan_body())
        hub.settings, hub.outdated = settings, outdated
        url = await hub.start()
        config = WorkerConfig(
            url=url,
            worker_id=worker_id,
            name="mac",
            projects=[PROJECT],
            checkouts={f"{PROJECT}/{name}": str(path) for name, path in machine.checkouts.items()},
            version_locked=stored_lock,
        )
        machine.home.save(config, TOKEN)
        daemon = Daemon(
            machine.home,
            config,
            TOKEN,
            adapters={"claude-code": fake_adapter.FakeAdapter},
            env=machine.env,
            self_update=updater(machine.home),
        )
        task = asyncio.create_task(daemon.run())
        try:
            await body(hub, daemon, task)
        finally:
            if not task.done():
                daemon._signal(signal.SIGTERM)
                done, _ = await asyncio.wait({task}, timeout=20)
                if not done:
                    task.cancel()
            await hub.stop()
        return task.result()

    return asyncio.run(go())


def waiting_step(machine, tmp_path, key: str = "1") -> tuple[Path, Path]:  # noqa: F811
    """A scenario for step ``key`` whose agent touches a file, then waits for another: (started, release)."""
    started, release = tmp_path / f"started-{key}", tmp_path / f"release-{key}"
    machine.scenarios(
        {
            key: [
                {"touch": str(started)},
                {"wait_for": str(release)},
                {"write": {f"{key}.txt": "x\n"}},
                {"result": {"verify_commands": [f"test -f {key}.txt"]}},
            ]
        }
    )
    return started, release


# What the daemon reads of its install


def test_a_uv_tool_install_of_nightly_from_git_can_be_updated(fake):
    install = fake.install()
    assert (install.kind, install.reason, install.commit, install.version) == ("git", None, OLD, "0.22.0")
    assert install.extras == ("worker",) and install.entrypoint == fake.bin / "evo-agents"
    assert install.commands == (fake.bin / "evo-agents",)
    assert install.python == PYTHON and install.prefix == fake.prefix


def test_an_install_from_pypi_can_be_updated_and_names_no_commit(tmp_path):
    prefix = write_install(
        tmp_path / "tools", tmp_path / "bin", version="0.21.0", commit=None, extras=("graphify", "worker")
    )
    install = read_install(prefix, PYTHON)
    assert (install.kind, install.reason, install.commit, install.version) == ("pypi", None, None, "0.21.0")
    assert install.extras == ("graphify", "worker")


@pytest.mark.parametrize(
    ("requirement", "kind", "said"),
    [
        ('{ name = "evo-ak", editable = "/src/evo-agents" }', "editable", "an editable install of /src/evo-agents"),
        ('{ name = "evo-ak", directory = "/src/evo-agents" }', "local", "installed from the local path"),
        ('{ name = "evo-ak", git = "https://github.com/someone/evo-agents?rev=main" }', "foreign", "not https://"),
        ('{ name = "evo-ak", url = "https://example.com/evo_ak-0.22.0.whl" }', "url", "installed from the URL"),
    ],
)
def test_an_install_the_daemon_cannot_update_says_why(fake, requirement, kind, said):
    receipt = fake.prefix / "uv-receipt.toml"
    receipt.write_text(
        f'[tool]\nrequirements = [{requirement}]\nentrypoints = [{{ name = "evo-agents", install-path = "/x" }}]\n',
        encoding="utf-8",
    )
    install = read_install(fake.prefix, PYTHON)
    assert install.kind == kind and said in install.reason


def test_an_install_with_more_than_evo_ak_or_without_a_receipt_cannot_be_updated(fake, tmp_path):
    receipt = fake.prefix / "uv-receipt.toml"
    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace("requirements = [", 'requirements = [{ name = "rich" }, '),
        encoding="utf-8",
    )
    assert "installed with rich beside it" in read_install(fake.prefix, PYTHON).reason
    bare = tmp_path / "venv"
    bare.mkdir()
    install = read_install(bare, PYTHON)
    assert install.kind == "none" and "not installed with `uv tool install`" in install.reason


def test_repo_of_reads_the_git_urls_of_receipts():
    assert (
        repo_of("https://github.com/maycuatroi1/evo-agents?rev=nightly") == "https://github.com/maycuatroi1/evo-agents"
    )
    assert (
        repo_of("git+https://github.com/maycuatroi1/evo-agents.git@nightly")
        == "https://github.com/maycuatroi1/evo-agents"
    )
    assert repo_of("https://github.com/someone/evo-agents#subdirectory=x") == "https://github.com/someone/evo-agents"


def test_uv_is_found_off_the_service_path_where_installers_put_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("evo_agents.worker.selfupdate.install.UV_PLACES", ("~/.local/bin",))
    assert find_uv("/nonexistent") is None
    found = install_script(tmp_path / ".local" / "bin" / "uv")
    assert find_uv("/nonexistent") == str(found)


# What the daemon reads of its service manager


def test_a_launchd_job_that_keeps_the_daemon_alive_may_update(tmp_path):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    label = "io.github.maycuatroi1.evo-agents.worker"
    plist = agents / f"{label}.plist"
    plist.write_bytes(
        b'<?xml version="1.0"?><plist version="1.0"><dict><key>KeepAlive</key><dict><key>SuccessfulExit</key>'
        b"<false/></dict></dict></plist>"
    )
    env = {"XPC_SERVICE_NAME": label}
    assert launchd(env, 1, agents) == Supervision("launchd", label)
    assert launchd(env, 4242, agents) == Supervision(None, reason=BY_HAND), "a child of a shell, not of launchd"
    assert launchd({"XPC_SERVICE_NAME": "0"}, 1, agents).reason == BY_HAND
    plist.write_bytes(b'<?xml version="1.0"?><plist version="1.0"><dict></dict></plist>')
    assert "KeepAlive" in launchd(env, 1, agents).reason
    assert detect(env, platform="darwin", ppid=1, agents=agents).manager == "launchd"


def test_a_systemd_unit_that_restarts_on_failure_may_update(tmp_path):
    cgroup = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/evo-curator-worker.service\n"
    calls = []

    def run(answer: str):
        def call(argv, **kwargs):
            calls.append(argv)
            return type("Proc", (), {"returncode": 0, "stdout": answer, "stderr": ""})()

        return call

    env = {"INVOCATION_ID": "5f1c"}
    ok = systemd(env, cgroup, run("Restart=on-failure\nRestartPreventExitStatus=3\n"))
    assert ok == Supervision("systemd", "evo-curator-worker.service")
    assert calls[0][:4] == ["systemctl", "--user", "show", "evo-curator-worker.service"]
    assert "Restart=no" in systemd(env, cgroup, run("Restart=no\nRestartPreventExitStatus=\n")).reason
    assert (
        "RestartPreventExitStatus"
        in systemd(env, cgroup, run("Restart=always\nRestartPreventExitStatus=3 75\n")).reason
    )
    assert systemd({}, cgroup, run("Restart=always\n")).reason == BY_HAND, "no INVOCATION_ID: run by hand"
    scope = "0::/user.slice/user-1000.slice/session-3.scope\n"
    assert systemd(env, scope, run("Restart=always\n")).reason == BY_HAND
    assert detect(env, platform="linux", cgroup=cgroup, run=run("Restart=always\n")).manager == "systemd"


# Where nightly points


def test_the_tag_is_read_from_git_ls_remote_and_from_the_api_when_git_fails():
    annotated = f"{'d' * 40}\trefs/tags/nightly\n{NEW}\trefs/tags/nightly^{{}}\n"
    assert parse_ls_remote(annotated) == NEW
    assert parse_ls_remote(f"{NEW}\trefs/tags/nightly\n") == NEW
    assert parse_ls_remote("") is None

    async def git(argv, *, env, timeout):
        assert (
            argv[1:3] == ["ls-remote", "https://github.com/maycuatroi1/evo-agents"]
            and env["GIT_TERMINAL_PROMPT"] == "0"
        )
        return Done(0, annotated)

    async def broken_git(argv, *, env, timeout):
        return Done(128, "fatal: unable to access 'https://github.com/': Could not resolve host")

    pages = {
        installer_module.API_URL: {"object": {"type": "tag", "url": "https://api/tag"}},
        "https://api/tag": {"object": {"type": "commit", "sha": NEWER}},
    }
    env = {"PATH": os.environ["PATH"]}
    assert asyncio.run(nightly_commit(env, runner=git)) == NEW
    assert asyncio.run(nightly_commit(env, runner=broken_git, fetch=pages.__getitem__)) == NEWER
    assert api_commit(lambda url: {"object": {"type": "commit", "sha": NEW}}) == NEW

    def offline(url):
        raise OSError("network is unreachable")

    with pytest.raises(LookupError, match="Could not resolve host.*GitHub API: network is unreachable"):
        asyncio.run(nightly_commit(env, runner=broken_git, fetch=offline))


# The daemons of one install


def test_a_claim_and_an_update_of_one_install_never_overlap(tmp_path):
    root = tmp_path / "installs" / "x"
    one, two = Peers(root, tmp_path / "w1"), Peers(root, tmp_path / "w2")

    async def go():
        assert one.enter() and two.gate_open()
        assert two.close_gate() and not one.gate_open()
        assert not one.enter(), "the gate is closed: no claim"
        assert not await two.wait_idle(0.5), "one still holds the install for its claim"
        one.idle()
        assert await two.wait_idle(0.5)
        assert not one.enter(), "the update holds the install"
        assert not Peers(root, tmp_path / "w3").close_gate(), "one update at a time"
        two.open_gate()
        assert one.enter() and one.gate_open()
        one.close()
        two.close()

    asyncio.run(go())


def test_only_the_daemons_that_run_count_as_peers(tmp_path):
    root = tmp_path / "installs" / "x"
    homes = [WorkerHome(tmp_path / name) for name in ("w1", "w2", "w3")]
    for home in homes:
        home.ensure()
    lock = PidLock(homes[1].pid_path)
    assert lock.acquire()
    try:
        for home in homes[1:]:
            Peers(root, home.root).publish({"worker": home.root.name, "ready": False, "why": "it holds 1 run(s)"})
        seen = Peers(root, homes[0].root).others()
        assert [state["worker"] for state in seen] == ["w2"], "w3 does not run: its file goes"
        assert len(list((root / "daemons").glob("*.json"))) == 1
    finally:
        lock.release()


@pytest.mark.parametrize("worked", [True, False], ids=["update-worked", "copy-put-back"])
def test_a_daemon_whose_install_changes_during_an_update_restarts_only_once_the_gate_opens(fake, tmp_path, worked):
    """With a uv that deletes the environment first, the other daemons of the install see it change in the middle of
    the update: they wait for the updater, whose copy may yet go back, instead of restarting on half an install."""
    homes = [WorkerHome(tmp_path / name) for name in ("worker-a", "worker-b")]
    for home in homes:
        home.ensure()
    a, b = (fake.updater(home) for home in homes)
    b.hear_lock(False)
    fake.point(OLD)
    try:
        assert a.peers.close_gate()
        shutil.rmtree(fake.prefix)  # uv 0.12.24 in the middle of the update
        assert asyncio.run(b.turn(held=0, orphans=0, worker="b")) is False
        assert (b.state, b.reason) == ("waiting", "another daemon of this install is updating it")
        assert not b.may_claim()
        if worked:
            write_install(fake.tools, fake.bin, version="0.23.0", commit=NEW)
            assert asyncio.run(b.turn(held=0, orphans=0, worker="b")) is False, "the updater has not exited yet"
            a.close()  # the updater exits 75, and the kernel drops its locks
            assert asyncio.run(b.turn(held=0, orphans=0, worker="b")) is True, "b restarts on the new install"
        else:
            write_install(fake.tools, fake.bin, version="0.22.0", commit=OLD)  # the copy went back
            a.peers.open_gate()
            assert asyncio.run(b.turn(held=0, orphans=0, worker="b")) is False
            assert b.state == "current" and b.may_claim(), "b goes on with the install it started on"
    finally:
        a.close()
        b.close()


# The update


def test_an_update_installs_nightly_in_staging_then_over_the_install(any_uv):
    fake = any_uv
    (fake.prefix / "marker").write_text("of the old environment", encoding="utf-8")
    outcome = asyncio.run(fake.installer().update(fake.lookup))
    assert outcome.ok and outcome.commit == NEW
    assert fake.install().commit == NEW and fake.install().version == "0.23.0"
    assert 'rev=nightly"' in (fake.prefix / "uv-receipt.toml").read_text(encoding="utf-8")
    assert (fake.prefix / "marker").exists() is not fake.recreates, "the fake uv recreates the environment, or not"
    staged, real = fake.calls()
    for call in (staged, real):
        argv = call["argv"]
        assert argv[-1] == NIGHTLY_REQUIREMENT
        assert argv[argv.index("--python") + 1] == PYTHON, "the install's own interpreter"
        assert {"--reinstall-package", "--refresh-package", "--force"} <= set(argv)
    assert staged["tool_dir"] == str(fake.tmp / "staging" / "tools")
    assert (real["tool_dir"], real["bin_dir"]) == (str(fake.tools), str(fake.bin))
    assert not (fake.tmp / "staging").exists() and not (fake.tmp / "backup").exists()
    assert fake.starts()


@pytest.mark.parametrize(
    ("step", "said"),
    [("fail", "uv could not install"), ("broken", "the new version does not start"), ("wipe", "uv could not install")],
)
def test_an_update_that_fails_in_staging_leaves_the_install_untouched(any_uv, step, said):
    fake = any_uv
    fake.steps([step])
    before = (fake.prefix / "uv-receipt.toml").read_text(encoding="utf-8")
    outcome = asyncio.run(fake.installer().update(fake.lookup))
    assert not outcome.ok and said in outcome.error and not outcome.broken
    assert len(fake.calls()) == 1, "the install was never touched"
    assert fake.install().commit == OLD and (fake.prefix / "uv-receipt.toml").read_text(encoding="utf-8") == before
    assert not (fake.tmp / "backup").exists(), "no copy is made for an update that stops in staging"


@pytest.mark.parametrize("step", ["fail", "broken", "wipe"])
def test_an_update_that_fails_over_the_install_puts_its_copy_back_without_uv(any_uv, step):
    """Criterion a4 whatever uv does to the environment: here every uv call after the failed one fails as well (no
    network), and the install still starts, as it was, from the copy made before uv went over it."""
    fake = any_uv
    fake.steps(["ok", step, "fail", "fail"])
    (fake.prefix / "marker").write_text("of the old environment", encoding="utf-8")
    receipt = (fake.prefix / "uv-receipt.toml").read_bytes()
    outcome = asyncio.run(fake.installer().update(fake.lookup))
    assert not outcome.ok and not outcome.broken
    assert outcome.error.endswith("; put back the copy of the install"), outcome.error
    assert len(fake.calls()) == 2, "no uv call puts it back"
    assert (fake.prefix / "uv-receipt.toml").read_bytes() == receipt, "the receipt as it was: still nightly"
    assert (fake.prefix / "marker").exists(), "the very environment that was there, not one built again"
    install = fake.install()
    assert (install.commit, install.version) == (OLD, "0.22.0")
    command = fake.bin / "evo-agents"
    assert command.is_symlink() and command.resolve() == (fake.prefix / "bin" / "evo-agents").resolve()
    assert fake.starts(), "the install starts again"
    assert not (fake.tmp / "backup").exists(), "the copy is dropped once it is back"


def test_a_command_that_was_a_copy_is_put_back_as_a_copy(tmp_path):
    fake = Fake(tmp_path, recreates=True)
    write_install(fake.tools, fake.bin, version="0.22.0", commit=OLD, link=False)
    command = fake.bin / "evo-agents"
    before = command.read_bytes()
    fake.steps(["ok", "fail"])
    outcome = asyncio.run(fake.installer().update(fake.lookup))
    assert not outcome.ok and not outcome.broken and "put back the copy" in outcome.error
    assert not command.is_symlink() and command.read_bytes() == before
    assert fake.starts()


@pytest.mark.parametrize(
    ("commit", "version", "previous"),
    [
        (OLD, "0.22.0", f"evo-ak[worker] @ git+https://github.com/maycuatroi1/evo-agents@{OLD}"),
        (None, "0.21.0", "evo-ak[worker]==0.21.0"),
    ],
    ids=["git", "pypi"],
)
def test_an_update_whose_copy_cannot_go_back_installs_the_old_version_again(
    tmp_path, monkeypatch, commit, version, previous
):
    fake = Fake(tmp_path, recreates=True)
    fake.prefix = write_install(fake.tools, fake.bin, version=version, commit=commit)

    def unreadable(place, prefix):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(backup_module, "restore", unreadable)
    fake.steps(["ok", "broken"])
    outcome = asyncio.run(fake.installer().update(fake.lookup))
    assert not outcome.ok and not outcome.broken
    assert "putting back the copy of the install failed: [Errno 5] Input/output error" in outcome.error
    assert outcome.error.endswith(f"; put back {previous}"), outcome.error
    assert fake.calls()[-1]["argv"][-1] == previous
    install = fake.install()
    assert (install.commit, install.version) == (commit, version)
    if commit:
        assert f'rev={OLD}"' in (fake.prefix / "uv-receipt.toml").read_text(encoding="utf-8"), "uv names the commit"
    assert fake.starts()
    assert not (fake.tmp / "backup").exists()


def test_an_update_that_cannot_put_anything_back_keeps_the_copy_and_stops_updating(fake, monkeypatch):
    def unreadable(place, prefix):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(backup_module, "restore", unreadable)
    fake.steps(["ok", "wipe", "fail"])
    outcome = asyncio.run(fake.installer().update(fake.lookup))
    assert not outcome.ok and outcome.broken and "putting back" in outcome.error and "failed too" in outcome.error
    kept = fake.tmp / "backup"
    assert (kept / "env" / "uv-receipt.toml").is_file() and (kept / "KEPT").read_text(encoding="utf-8").strip()
    calls = len(fake.calls())
    again = asyncio.run(fake.installer().update(fake.lookup))
    assert not again.ok and again.broken and f"kept at {kept}" in again.error
    assert len(fake.calls()) == calls, "no update goes over the copy kept for a person"
    assert (kept / "env" / "uv-receipt.toml").is_file()


def test_an_install_that_cannot_be_copied_is_not_updated(any_uv):
    fake = any_uv
    (fake.tmp / "full").write_text("a file where the copy's directory would go", encoding="utf-8")
    receipt = (fake.prefix / "uv-receipt.toml").read_bytes()
    outcome = asyncio.run(fake.installer(backup_dir=fake.tmp / "full" / "backup").update(fake.lookup))
    assert not outcome.ok and not outcome.broken
    assert outcome.error.startswith("the install was not copied before the update, which did not go on")
    assert len(fake.calls()) == 1, "staged only: uv never went over the install"
    assert (fake.prefix / "uv-receipt.toml").read_bytes() == receipt and fake.install().commit == OLD
    assert fake.starts()


# The daemon


def test_an_idle_unlocked_daemon_installs_nightly_and_exits_for_its_manager_to_restart_it(machine, fake, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="evo_agents.worker")

    async def body(hub, daemon, task):
        assert await ended(task) == EXIT_UPDATED
        assert all(sent["agent_commit"] == OLD for sent in hub.heartbeat_bodies), "the commit it runs"
        assert states(hub)[0] == {"state": "waiting", "reason": LOCK_UNKNOWN}, "the first heartbeat, before any answer"

    assert run_daemon(machine, body, updater=fake.updater) == EXIT_UPDATED
    assert fake.install().commit == NEW and len(fake.calls()) == 2
    restarted = fake.updater(machine.home)
    assert restarted.heartbeat_fields()["agent_commit"] == NEW, "the next daemon's heartbeat names the new commit"
    restarted.close()
    assert json.loads(machine.home.config_path.read_text(encoding="utf-8"))["version_locked"] is False
    said = [record.getMessage() for record in caplog.records if record.getMessage().startswith("self-update")]
    assert said[-2:] == ["self-update: installing", "self-update: installed; the daemon restarts on it"]
    assert records(caplog, "self-update: updating")


def test_a_locked_worker_stays_on_its_version_until_its_owner_unlocks_it(machine, fake):  # noqa: F811
    async def body(hub, daemon, task):
        report = await until_heartbeat(hub, lambda found: found.get("state") == "locked", "the state locked")
        assert report["reason"] == "its version is locked"
        await asyncio.sleep(1.0)
        assert not task.done() and fake.calls() == [] and fake.lookups == 0
        assert json.loads(machine.home.config_path.read_text(encoding="utf-8"))["version_locked"] is True
        hub.settings = UNLOCKED
        assert await ended(task) == EXIT_UPDATED

    assert run_daemon(machine, body, updater=fake.updater, settings=LOCKED) == EXIT_UPDATED
    assert fake.install().commit == NEW


@pytest.mark.parametrize("settings", [None, {"slots": 1, "name": "mac", "labels": []}])
def test_a_daemon_that_has_not_heard_the_lock_does_not_update(machine, fake, settings):  # noqa: F811
    async def body(hub, daemon, task):
        await wait_for(lambda: hub.heartbeats >= 5, "five heartbeats")
        assert states(hub)[-1] == {"state": "waiting", "reason": LOCK_UNKNOWN}
        assert not task.done() and fake.calls() == [] and fake.lookups == 0

    assert run_daemon(machine, body, updater=fake.updater, settings=settings) == 0


def test_a_daemon_run_by_hand_or_turned_off_does_not_update(machine, fake):  # noqa: F811
    def by_hand(home):
        return fake.updater(home, supervision=Supervision(None, reason=BY_HAND))

    async def body(hub, daemon, task):
        report = await until_heartbeat(hub, lambda found: found.get("state") == "unsupported", "unsupported")
        assert report["reason"] == BY_HAND
        await asyncio.sleep(0.6)
        assert not task.done() and fake.calls() == [] and fake.lookups == 0

    assert run_daemon(machine, body, updater=by_hand) == 0

    def turned_off(home):
        return fake.updater(home, off=True)

    async def body_off(hub, daemon, task):
        report = await until_heartbeat(hub, lambda found: found.get("state") == "unsupported", "unsupported")
        assert "EVO_WORKER_SELF_UPDATE" in report["reason"]

    assert run_daemon(machine, body_off, updater=turned_off) == 0
    assert fake.calls() == []


def test_a_daemon_holding_a_run_waits_and_updates_once_the_run_ends(machine, fake, tmp_path):  # noqa: F811
    started, release = waiting_step(machine, tmp_path)
    fake.point(OLD)

    def updater(home):
        return fake.updater(home, clock=fake.clock)

    async def body(hub, daemon, task):
        await until_heartbeat(hub, lambda found: found.get("state") == "current", "current")
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        await wait_for(started.exists, "the agent to start")
        fake.point(NEW)
        fake.later()
        report = await until_heartbeat(hub, lambda found: found.get("reason") == "it holds 1 run(s)", "waiting")
        assert report["state"] == "waiting" and report["nightly_commit"] == NEW and report["checked_at"]
        await asyncio.sleep(0.6)
        assert fake.calls() == [], "no code changes under a run"
        release.write_text("go", encoding="utf-8")
        assert await hub.wait_state(run_id, "done", "failed") == "done", hub.texts(run_id)
        assert await ended(task) == EXIT_UPDATED

    assert run_daemon(machine, body, updater=updater) == EXIT_UPDATED
    assert fake.install().commit == NEW


@pytest.mark.parametrize(
    ("recreates", "steps", "said", "tried"),
    [
        (False, ["fail"], "uv could not install", 1),
        (True, ["ok", "fail"], "; put back the copy of the install", 2),
    ],
    ids=["in-staging", "over-the-install-uv-recreates"],
)
def test_a_failed_update_is_reported_runs_go_on_and_it_is_tried_again_later(
    machine,  # noqa: F811
    fake,
    tmp_path,
    recreates,
    steps,
    said,
    tried,
):
    fake.recreates = recreates
    fake.steps(steps)
    started, release = waiting_step(machine, tmp_path)

    def updater(home):
        return fake.updater(home, clock=fake.clock)

    async def body(hub, daemon, task):
        report = await until_heartbeat(hub, lambda found: "error" in found, "the error reported")
        assert said in report["error"] and report["error_at"] and report["state"] == "waiting"
        assert "failed; it is tried again later" in report["reason"]
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        await wait_for(started.exists, "a run claimed on the version it runs")
        assert not task.done() and fake.install().commit == OLD and len(fake.calls()) == tried
        assert await fake.probe(), "the install the service would start again"
        release.write_text("go", encoding="utf-8")
        assert await hub.wait_state(run_id, "done", "failed") == "done", hub.texts(run_id)
        await asyncio.sleep(0.6)
        assert not task.done() and len(fake.calls()) == tried, "not before the hour is up"
        fake.later()
        assert await ended(task) == EXIT_UPDATED, "tried again"

    assert run_daemon(machine, body, updater=updater) == EXIT_UPDATED
    assert len(fake.calls()) == tried + 2 and fake.install().commit == NEW


def test_a_tag_that_cannot_be_read_is_reported_and_read_again(machine, fake):  # noqa: F811
    fake.lookup_error = "cannot read where nightly points: git ls-remote: Could not resolve host"

    def updater(home):
        return fake.updater(home, lookup_retry=0.5)

    async def body(hub, daemon, task):
        report = await until_heartbeat(hub, lambda found: "error" in found, "the error reported")
        assert "Could not resolve host" in report["error"] and report["state"] == "waiting"
        await wait_for(lambda: fake.lookups >= 2, "a second read")
        assert not task.done() and fake.calls() == []
        fake.lookup_error = None
        assert await ended(task) == EXIT_UPDATED

    assert run_daemon(machine, body, updater=updater) == EXIT_UPDATED


def test_a_read_that_succeeds_again_clears_the_error_the_heartbeat_carries(machine, fake):  # noqa: F811
    """The web shows a failed update while self_update.error is set: a daemon that reads the tag again and runs its
    commit says current, with no error."""
    fake.point(OLD)
    fake.lookup_error = "cannot read where nightly points: GitHub API: HTTP Error 403: rate limit exceeded"

    def updater(home):
        return fake.updater(home, clock=fake.clock)

    async def body(hub, daemon, task):
        report = await until_heartbeat(hub, lambda found: "error" in found, "the error reported")
        assert report["state"] == "waiting" and "error_at" in report and "checked_at" not in report
        fake.lookup_error = None
        fake.later()
        report = await until_heartbeat(hub, lambda found: found.get("state") == "current", "current")
        assert report == {"state": "current", "nightly_commit": OLD, "checked_at": report["checked_at"]}
        assert not task.done() and fake.calls() == []

    assert run_daemon(machine, body, updater=updater) == 0


@pytest.mark.parametrize(("stored_lock", "code"), [(False, EXIT_UPDATED), (True, 1), (None, 1)])
def test_a_hub_that_answers_426_gets_an_update_first_when_the_lock_kept_says_unlocked(machine, fake, stored_lock, code):  # noqa: F811
    async def body(hub, daemon, task):
        assert await ended(task) == code

    assert run_daemon(machine, body, updater=fake.updater, stored_lock=stored_lock, outdated=True) == code
    assert fake.install().commit == (NEW if stored_lock is False else OLD)
    assert len(fake.calls()) == (2 if stored_lock is False else 0)


def test_two_daemons_of_one_install_update_it_once_both_are_idle_and_both_restart(machine, fake, tmp_path):  # noqa: F811
    started, release = waiting_step(machine, tmp_path)
    fake.point(OLD)
    other = WorkerHome(tmp_path / "worker-b")
    other.ensure()
    locks = [PidLock(machine.home.pid_path), PidLock(other.pid_path)]
    machine.home.ensure()

    async def go():
        assert all(lock.acquire() for lock in locks)
        hubs = [FakeHub(PROJECT), FakeHub(PROJECT)]
        daemons = []
        for index, (hub, home) in enumerate(zip(hubs, (machine.home, other), strict=True)):
            hub.put_plan(plan_body())
            hub.settings = {**UNLOCKED, "name": f"mac-{'ab'[index]}"}
            config = WorkerConfig(
                url=await hub.start(),
                worker_id=index + 1,
                name=f"mac-{'ab'[index]}",
                projects=[PROJECT],
                checkouts={f"{PROJECT}/{name}": str(path) for name, path in machine.checkouts.items()},
            )
            home.save(config, TOKEN)
            adapters = {"claude-code": fake_adapter.FakeAdapter}
            updater = fake.updater(home, clock=fake.clock)
            daemons.append(Daemon(home, config, TOKEN, adapters=adapters, env=machine.env, self_update=updater))
        run_b = hubs[1].queue_step_run(PLAN, "1", "alpha", "feat/alpha")  # before b starts, so b claims it
        tasks = [asyncio.create_task(daemon.run()) for daemon in daemons]
        try:
            await wait_for(started.exists, "b's agent to start")
            fake.point(NEW)
            fake.later()
            reason = "worker mac-b (id 2) on the same install: it holds 1 run(s)"
            await until_heartbeat(hubs[0], lambda found: found.get("reason") == reason, "a to wait for b")
            await asyncio.sleep(0.6)
            assert fake.calls() == [] and not any(task.done() for task in tasks)
            release.write_text("go", encoding="utf-8")
            assert await hubs[1].wait_state(run_b, "done", "failed") == "done", hubs[1].texts(run_b)
            assert [await ended(task) for task in tasks] == [EXIT_UPDATED, EXIT_UPDATED]
        finally:
            for daemon, task in zip(daemons, tasks, strict=True):
                if not task.done():
                    daemon._signal(signal.SIGTERM)
            await asyncio.wait(tasks, timeout=20)
            for hub in hubs:
                await hub.stop()

    try:
        asyncio.run(go())
    finally:
        for lock in locks:
            lock.release()
    assert len(fake.calls()) == 2, "one update, by a"
    assert fake.install().commit == NEW
