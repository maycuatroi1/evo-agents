"""The worker's background service: the LaunchAgent plist and the systemd user unit it writes, and install, status and
uninstall against a fake launchctl and a fake systemctl. No test runs launchctl or systemctl, so the file runs on any
POSIX system."""

import json
import logging
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.worker import logs, service
from evo_agents.worker.home import PidLock, WorkerConfig, WorkerHome

EVO = "/opt/tools/bin/evo-agents"
SYSTEM = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake HOME, so ~/Library/LaunchAgents, ~/.config/systemd/user and ~/.evo/worker point into tmp_path."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("EVO_WORKER_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(sys, "argv", [EVO])
    monkeypatch.setattr(service, "missing_extra", lambda: None)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    return path


@pytest.fixture
def joined(home):
    worker_home = WorkerHome()
    config = WorkerConfig(url="https://hub.example.org", worker_id=7, name="mac-mini", projects=["demo"])
    worker_home.save(config, "evw_" + "s" * 43)
    return worker_home


def forbid_real_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a test must never run launchctl or systemctl")

    monkeypatch.setattr(subprocess, "run", forbidden)


def print_output(state: str, pid: int | None, runs: int, last_exit: str) -> str:
    """``launchctl print`` of the job, in the shape macOS prints it."""
    pid_line = f"\tpid = {pid}\n" if pid else ""
    return (
        f"gui/{os.getuid()}/{service.LABEL} = {{\n"
        "\tactive count = 1\n"
        f"\tpath = /Users/octo/Library/LaunchAgents/{service.LABEL}.plist\n"
        "\ttype = LaunchAgent\n"
        f"\tstate = {state}\n"
        "\n"
        f"\tprogram = {EVO}\n"
        "\targuments = {\n"
        f"\t\t{EVO}\n"
        "\t\tworker\n"
        "\t\trun\n"
        "\t\t--quiet\n"
        "\t}\n"
        "\n"
        "\tenvironment = {\n"
        "\t\tPATH => /usr/bin:/bin\n"
        "\t}\n"
        "\n"
        f"\tdomain = gui/{os.getuid()} [100023]\n"
        f"\truns = {runs}\n"
        f"{pid_line}"
        f"\tlast exit code = {last_exit}\n"
        "\n"
        "\tresource coalition = {\n"
        "\t\tID = 407837\n"
        "\t\ttype = resource\n"
        "\t\tstate = active\n"
        "\t}\n"
        "}\n"
    )


class FakeLaunchctl:
    """launchctl for one job: bootstrap loads it in ``state``, bootout removes it, print shows it or exits 113."""

    def __init__(self, state="running", last_exit="(never exited)", fail=None):
        self.state = state
        self.last_exit = last_exit
        self.fail = fail
        self.loaded = False
        self.runs = 0
        self.calls = []

    def __call__(self, argv, **kwargs):
        assert argv[0] == "launchctl" and kwargs.get("capture_output") and kwargs.get("timeout")
        verb = argv[1]
        self.calls.append(argv[1:])
        if verb == self.fail:
            return subprocess.CompletedProcess(argv, 5, "", "Bootstrap failed: 5: Input/output error")
        if verb == "print":
            if not self.loaded:
                text = f'Could not find service "{service.LABEL}" in domain for user gui: {os.getuid()}\n'
                return subprocess.CompletedProcess(argv, 113, "", text)
            pid = 4242 if self.state == "running" else None
            return subprocess.CompletedProcess(argv, 0, print_output(self.state, pid, self.runs, self.last_exit), "")
        if verb == "bootstrap":
            plist = plistlib.loads(Path(argv[3]).read_bytes())
            assert plist["Label"] == service.LABEL
            self.loaded, self.runs = True, 1
        elif verb == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeSystemctl:
    """systemctl --user for one unit, which runs once it was restarted and stops when disabled with --now."""

    def __init__(self, missing=False):
        self.missing = missing
        self.unit_file = None
        self.loaded = False
        self.active = False
        self.calls = []

    def __call__(self, argv, **kwargs):
        if self.missing:
            raise FileNotFoundError(argv[0])
        assert argv[:2] == ["systemctl", "--user"] and kwargs.get("capture_output") and kwargs.get("timeout")
        verb, rest = argv[2], argv[3:]
        self.calls.append(argv[2:])
        if verb == "daemon-reload":
            self.loaded = self.unit_file is not None and self.unit_file.exists()
        elif verb == "restart":
            assert self.loaded, "restart before daemon-reload"
            self.active = True
        elif verb == "disable":
            assert rest == ["--now", service.UNIT]
            self.active = False
        elif verb == "show":
            facts = {
                "LoadState": "loaded" if self.loaded else "not-found",
                "ActiveState": "active" if self.active else "inactive",
                "SubState": "running" if self.active else "dead",
                "MainPID": "5151" if self.active else "0",
                "ExecMainStatus": "0",
                "NRestarts": "0",
            }
            assert rest == [service.UNIT, "--property=" + ",".join(facts)]
            return subprocess.CompletedProcess(argv, 0, "".join(f"{k}={v}\n" for k, v in facts.items()), "")
        return subprocess.CompletedProcess(argv, 0, "", "")


# What the service runs


def test_the_launch_agent_runs_the_daemon_with_the_path_of_the_install_and_again_after_an_error(home):
    path = "/Users/octo/.local/bin:relative/bin::/opt/homebrew/bin:/usr/bin/:/usr/bin:/Users/octo/.bun/bin"
    spec = service.build_spec(WorkerHome(), argv0=EVO, path=path)
    plist = service.build_plist(spec)
    state = home / ".evo" / "worker"
    assert plist == {
        "Label": "io.github.maycuatroi1.evo-agents.worker",
        "ProgramArguments": [EVO, "worker", "run", "--quiet"],
        "EnvironmentVariables": {
            "PATH": ":".join(
                ["/Users/octo/.local/bin", "/opt/homebrew/bin", "/usr/bin", "/Users/octo/.bun/bin", "/usr/local/bin"]
                + ["/bin", "/usr/sbin", "/sbin"]
            ),
            "EVO_WORKER_HOME": str(state),
            "EVO_WORKER_REVOKED_EXIT": "0",  # launchd starts again a job that exits other than 0
        },
        "WorkingDirectory": str(state),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ExitTimeOut": 60,
        "StandardOutPath": str(state / "service.log"),
        "StandardErrorPath": str(state / "service.log"),
    }
    assert plistlib.loads(service.Launchd().render(spec).encode()) == plist
    assert service.Launchd().file() == home / "Library" / "LaunchAgents" / f"{service.LABEL}.plist"


def test_the_service_path_is_the_path_of_the_install_without_relative_entries(monkeypatch):
    monkeypatch.setenv("PATH", "/opt/bin:.:bin:/opt/bin/")
    assert service.service_path() == ":".join(["/opt/bin", *SYSTEM])
    assert service.service_path("") == ":".join(SYSTEM)


def test_the_state_directory_follows_evo_worker_home(home, tmp_path, monkeypatch):
    assert WorkerHome().root == home / ".evo" / "worker"
    monkeypatch.setenv("EVO_WORKER_HOME", str(tmp_path / "elsewhere"))
    assert WorkerHome().root == tmp_path / "elsewhere"
    spec = service.build_spec(argv0=EVO, path="/usr/bin")
    assert spec.environment["EVO_WORKER_HOME"] == str(tmp_path / "elsewhere")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVO_WORKER_HOME", "relative-home")
    assert service.build_spec(argv0=EVO, path="/usr/bin").home == tmp_path / "relative-home"


def test_the_systemd_unit_restarts_on_failure_and_quotes_what_systemd_would_read_otherwise(home, monkeypatch):
    spec = service.Spec(
        program=("/opt/my tools/evo-agents", "worker", "run", "--quiet", "$HOME", "50%"),
        path='/opt/100%/bin:/home/octo/My "Tools"/bin:/usr/bin',
        home=Path("/home/octo/.evo/worker"),
    )
    unit = service.build_unit(spec)
    lines = unit.splitlines()
    assert 'ExecStart="/opt/my tools/evo-agents" worker run --quiet $$HOME 50%%' in lines
    environment = 'Environment="PATH=/opt/100%%/bin:/home/octo/My \\"Tools\\"/bin:/usr/bin"'
    assert f"{environment} EVO_WORKER_HOME=/home/octo/.evo/worker" in lines
    for line in ("Type=simple", "Restart=on-failure", "RestartSec=10", "KillMode=mixed", "TimeoutStopSec=60"):
        assert line in lines
    assert "RestartPreventExitStatus=3" in lines, "a daemon that is no longer a worker is not started again"
    assert not any("EVO_WORKER_REVOKED_EXIT" in line for line in lines), "systemd tells the exit statuses apart"
    assert lines[lines.index("[Install]") + 1] == "WantedBy=default.target"
    assert unit.endswith("\n")

    systemd = service.Systemd()
    assert systemd.file() == home / ".config" / "systemd" / "user" / "evo-agents-worker.service"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert systemd.file() == home / "xdg" / "systemd" / "user" / "evo-agents-worker.service"
    systemd.file().parent.mkdir(parents=True)
    systemd.file().write_text(unit, encoding="utf-8")
    assert systemd.program() == '"/opt/my tools/evo-agents" worker run --quiet $HOME 50%'
    assert systemd.installed_home() == Path("/home/octo/.evo/worker")

    with pytest.raises(service.ServiceError, match="line break"):
        service.build_unit(service.Spec(program=("/bin/evo\n",), path="/usr/bin", home=Path("/h")))


def test_launchctl_print_is_read_at_its_top_level_only():
    facts = service.parse_launchctl_print(print_output("running", 4242, 3, "1"))
    assert facts["state"] == "running" and facts["pid"] == "4242" and facts["runs"] == "3"
    assert facts["last exit code"] == "1"
    assert facts["type"] == "LaunchAgent", "the coalition's nested type and state are not the job's"


# Install, status and uninstall


def test_install_status_and_uninstall_against_a_fake_launchctl(home, joined, monkeypatch, capsys):
    forbid_real_commands(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeLaunchctl()
    monkeypatch.setattr(subprocess, "run", fake)
    target = f"gui/{os.getuid()}/{service.LABEL}"
    plist_path = home / "Library" / "LaunchAgents" / f"{service.LABEL}.plist"

    assert main(["worker", "service", "install"]) == 0
    out = capsys.readouterr().out
    bootstrap = ["bootstrap", f"gui/{os.getuid()}", str(plist_path)]
    assert fake.calls == [["print", target], bootstrap, ["print", target], ["print", target]]
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["ProgramArguments"] == [EVO, "worker", "run", "--quiet"]
    assert plist["EnvironmentVariables"]["EVO_WORKER_HOME"] == str(joined.root)
    assert plist["EnvironmentVariables"]["PATH"] == service.service_path()
    assert stat.S_IMODE(plist_path.stat().st_mode) == 0o644
    assert f"Installed {service.LABEL} (launchd): {plist_path}" in out
    assert f"runs: {EVO} worker run --quiet, now and at each login, and again 10 s after it exits" in out
    assert "daemon: running, pid 4242" in out
    assert str(joined.root / "service.log") in out

    assert main(["worker", "service", "status"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"{service.LABEL} (launchd): running, pid 4242"
    assert f"runs: {EVO} worker run --quiet" in out
    assert main(["worker", "service", "status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["running"] is True and report["pid"] == 4242 and report["installed"] is True
    assert report["home"] == str(joined.root) and report["log"] == str(joined.root / "worker.log")
    assert report["program"] == f"{EVO} worker run --quiet" and report["last_exit"] is None

    fake.calls.clear()
    assert main(["worker", "service", "install"]) == 0, "installing again replaces the loaded job"
    assert fake.calls[:3] == [["print", target], ["bootout", target], ["print", target]]
    assert fake.calls[3][0] == "bootstrap"
    capsys.readouterr()

    fake.calls.clear()
    assert main(["worker", "service", "uninstall"]) == 0
    assert fake.calls == [["print", target], ["bootout", target], ["print", target]]
    assert not plist_path.exists()
    assert "Uninstalled io.github.maycuatroi1.evo-agents.worker (launchd)" in capsys.readouterr().out
    assert main(["worker", "service", "uninstall"]) == 0
    assert "is not installed" in capsys.readouterr().out
    assert main(["worker", "service", "status"]) == 0
    assert "is not installed: `evo-agents worker service install` starts it" in capsys.readouterr().out
    assert joined.joined(), "uninstall leaves the worker's token and configuration alone"


def test_install_status_and_uninstall_against_a_fake_systemctl(home, joined, monkeypatch, capsys):
    forbid_real_commands(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    fake = FakeSystemctl()
    monkeypatch.setattr(subprocess, "run", fake)
    unit_path = home / ".config" / "systemd" / "user" / service.UNIT
    fake.unit_file = unit_path

    assert main(["worker", "service", "install"]) == 0
    out = capsys.readouterr().out
    verbs = [call[0] for call in fake.calls]
    assert verbs == ["daemon-reload", "enable", "restart", "show", "show"]
    unit = unit_path.read_text(encoding="utf-8")
    assert f"ExecStart={EVO} worker run --quiet" in unit.splitlines()
    assert f"EVO_WORKER_HOME={joined.root}" in unit
    assert f"Installed {service.UNIT} (systemd): {unit_path}" in out
    assert "daemon: running, pid 5151" in out
    assert f"journalctl --user -u {service.UNIT}" in out

    assert main(["worker", "service", "status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["manager"] == "systemd" and report["running"] is True and report["pid"] == 5151
    assert report["state"] == "active/running" and report["home"] == str(joined.root)

    fake.calls.clear()
    assert main(["worker", "service", "uninstall"]) == 0
    assert [call[0] for call in fake.calls] == ["show", "disable", "daemon-reload", "reset-failed"]
    assert not unit_path.exists()
    assert main(["worker", "service", "status"]) == 0
    assert "evo-agents-worker.service (systemd) is not installed" in capsys.readouterr().out


def test_install_refuses_a_machine_that_is_not_a_worker_or_lacks_the_extra(home, monkeypatch, capsys):
    forbid_real_commands(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeLaunchctl()
    monkeypatch.setattr(subprocess, "run", fake)
    assert main(["worker", "service", "install"]) == 1
    assert "this machine is not a worker yet" in capsys.readouterr().err
    assert fake.calls == [] and not service.Launchd().file().exists()

    WorkerHome().save(WorkerConfig(url="https://hub.example.org", worker_id=7, name="mac", projects=["demo"]), "evw_x")
    monkeypatch.setattr(service, "missing_extra", lambda: "aiohttp")
    assert main(["worker", "service", "install"]) == 2
    err = capsys.readouterr().err
    assert "needs the worker extra (missing module aiohttp)" in err and "evo-ak[worker]" in err
    assert fake.calls == [] and not service.Launchd().file().exists()


def test_run_on_a_machine_that_is_no_longer_a_worker_exits_so_the_service_leaves_it_stopped(home, monkeypatch, capsys):
    forbid_real_commands(monkeypatch)
    monkeypatch.delenv("EVO_WORKER_REVOKED_EXIT", raising=False)
    assert main(["worker", "run", "--quiet"]) == 3, "RestartPreventExitStatus of the systemd unit"
    assert "this machine is not a worker yet" in capsys.readouterr().err
    WorkerHome().save(WorkerConfig(url="https://hub.example.org", worker_id=7, name="mac", projects=["demo"]), "evw_x")
    WorkerHome().forget()  # what `worker revoke` leaves
    monkeypatch.setenv("EVO_WORKER_REVOKED_EXIT", "0")  # as the LaunchAgent sets it
    assert main(["worker", "run", "--quiet"]) == 0
    assert "this machine is not a worker yet" in capsys.readouterr().err
    for value in ("three", "256", "-1"):
        monkeypatch.setenv("EVO_WORKER_REVOKED_EXIT", value)
        assert main(["worker", "run", "--quiet"]) == 3, value


def test_install_refuses_while_a_daemon_runs_outside_the_service(home, joined, monkeypatch, capsys):
    forbid_real_commands(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeLaunchctl()
    monkeypatch.setattr(subprocess, "run", fake)
    joined.ensure()
    lock = PidLock(joined.pid_path)
    assert lock.acquire()
    try:
        assert main(["worker", "service", "install"]) == 1
    finally:
        lock.release()
    err = capsys.readouterr().err
    assert f"a worker daemon runs here already outside the service (pid {os.getpid()})" in err
    assert not service.Launchd().file().exists()


def test_install_fails_when_the_daemon_does_not_keep_running(home, joined, monkeypatch, capsys):
    forbid_real_commands(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", FakeLaunchctl(state="spawn scheduled", last_exit="1"))
    assert main(["worker", "service", "install"]) == 1
    err = capsys.readouterr().err
    assert "the service is installed, but the daemon is not running (spawn scheduled, last exit 1)" in err
    assert str(joined.root / "service.log") in err
    assert service.Launchd().file().exists(), "it stays installed, so launchd keeps trying"


def test_status_says_why_the_daemon_does_not_run(home, monkeypatch, capsys):
    forbid_real_commands(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = FakeLaunchctl(state="not running", last_exit="0")
    fake.loaded, fake.runs = True, 3
    monkeypatch.setattr(subprocess, "run", fake)
    status = service.Launchd().status()
    assert (status.loaded, status.running, status.pid, status.last_exit, status.restarts) == (True, False, None, "0", 2)
    assert main(["worker", "service", "status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"{service.LABEL} (launchd): not running (last exit 0)"
    assert "missing; uninstall, then install again" in out[1], "launchd has the job, the plist is gone"


def test_a_launchctl_that_fails_is_one_error_line(home, joined, monkeypatch, capsys):
    forbid_real_commands(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", FakeLaunchctl(fail="bootstrap"))
    assert main(["worker", "service", "install"]) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("error: launchctl bootstrap gui/") and err.endswith("Input/output error")
    assert len(err.splitlines()) == 1


def test_the_service_needs_launchd_or_systemd(home, joined, monkeypatch, capsys):
    forbid_real_commands(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    assert main(["worker", "service", "install"]) == 1
    err = capsys.readouterr().err
    assert "needs launchd (macOS) or systemd (Linux), not win32" in err and "evo-agents worker run" in err

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", FakeSystemctl(missing=True))
    assert main(["worker", "service", "status"]) == 1
    assert "systemctl is not on PATH" in capsys.readouterr().err


# The log


def test_the_worker_log_rotates_at_10_mib_and_keeps_five_old_files(tmp_path):
    path = tmp_path / "worker.log"
    logs.configure(path, stderr=False)
    root = logging.getLogger()
    handler = next(h for h in root.handlers if isinstance(h, logs._WorkerFile))
    try:
        assert handler.maxBytes == 10 * 1024 * 1024 and handler.backupCount == 5
        handler.maxBytes = 512
        for number in range(40):
            logging.getLogger("evo_agents.worker").info("line %d %s", number, "x" * 200)
    finally:
        root.removeHandler(handler)
        handler.close()
    kept = sorted(p.name for p in tmp_path.iterdir())
    assert kept == ["worker.log", *(f"worker.log.{n}" for n in range(1, 6))]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in tmp_path.iterdir())
