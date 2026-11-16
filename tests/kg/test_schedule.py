"""The LaunchAgent plist. launchctl is never called: every command goes to a recording fake."""

import json
import os
import plistlib
import subprocess
import sys

import pytest

from evo_agents.cli import main
from evo_agents.kg import schedule
from evo_agents.kg.project import load_project_at, remember
from tests.kg.test_due import harness, source

SYNC = ["kg", "sync", "--due", "--all"]
POSIX_PATHS = pytest.mark.skipif(os.name == "nt", reason="the plist names POSIX paths and the gui/<uid> domain")


@pytest.fixture
def home(tmp_path, monkeypatch, kg_env):
    """A fake HOME, so ~/.local/bin and ~/Library/LaunchAgents point into tmp_path."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


@POSIX_PATHS
def test_schedule_plist_content(home, kg_env):
    plist = schedule.build_plist("/opt/tools/bin/evo-agents")
    assert plist["Label"] == "io.github.maycuatroi1.evo-agents.kg-sync"
    assert plist["StartInterval"] == 3600
    assert plist["ProgramArguments"] == ["/opt/tools/bin/evo-agents", *SYNC]
    env = plist["EnvironmentVariables"]
    assert env["PATH"].split(":") == [
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    assert env["EVO_KG_TRIGGER"] == "schedule"
    assert env["EVO_KG_HOME"] == str(kg_env)
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(kg_env / "schedule.log")
    assert plistlib.loads(schedule.render(plist).encode()) == plist


@POSIX_PATHS
def test_the_schedule_only_syncs_signed_in_or_not(home, kg_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")  # install needs launchd; launchctl is the fake below
    monkeypatch.delenv("EVO_HUB_HOME")  # the login of the test's HOME
    hub = home / ".evo" / "hub"
    hub.mkdir(parents=True)
    (hub / "token").write_text("evh_" + "x" * 43 + "\n")
    (hub / "config.json").write_text(json.dumps({"url": "https://hub.test", "login": "octo"}))
    plist = schedule.build_plist("/opt/tools/bin/evo-agents")
    assert plist["ProgramArguments"] == ["/opt/tools/bin/evo-agents", *SYNC]  # no --build: the hub builds
    assert main(["kg", "schedule", "print"]) == 0
    assert plistlib.loads(capsys.readouterr().out.encode())["ProgramArguments"][-4:] == SYNC
    fake = FakeLaunchctl()
    path = schedule.install("/opt/tools/bin/evo-agents", run=fake)
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][-4:] == SYNC


@POSIX_PATHS
def test_schedule_names_the_running_executable(tmp_path, monkeypatch):
    python_m = [os.path.abspath(sys.executable), "-m", "evo_agents"]
    assert schedule.executable("/opt/tools/bin/evo-agents") == ["/opt/tools/bin/evo-agents"]
    monkeypatch.chdir(tmp_path)
    assert schedule.executable("venv/bin/evo-agents") == [str(tmp_path / "venv/bin/evo-agents")]
    monkeypatch.setattr(schedule.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert schedule.executable("evo-agents") == ["/usr/local/bin/evo-agents"]
    monkeypatch.setattr(schedule.shutil, "which", lambda name: None)
    assert schedule.executable("evo-agents") == python_m
    assert schedule.executable("/src/evo_agents/__main__.py") == python_m
    assert schedule.executable("") == python_m


def test_schedule_print_command(home, kg_env, capsys):
    assert main(["kg", "schedule", "print"]) == 0
    plist = plistlib.loads(capsys.readouterr().out.encode())
    assert plist["Label"] == schedule.LABEL
    assert plist["ProgramArguments"][-4:] == SYNC
    assert os.path.isabs(plist["ProgramArguments"][0])
    assert plist["StandardOutPath"] == str(kg_env / "schedule.log")


class FakeLaunchctl:
    def __init__(self, loaded=False, fail=None):
        self.loaded = loaded
        self.fail = fail
        self.calls = []

    def __call__(self, argv, **kwargs):
        assert argv[0] == "launchctl"
        verb = argv[1]
        self.calls.append(argv[1:])
        if verb == self.fail:
            return subprocess.CompletedProcess(argv, 5, "", "Input/output error")
        if verb == "print":
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 113, "", "")
        self.loaded = verb == "bootstrap"
        return subprocess.CompletedProcess(argv, 0, "", "")


@POSIX_PATHS
def test_schedule_install_and_uninstall_against_a_fake_launchctl(home, kg_env, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a test must never run launchctl")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(sys, "platform", "darwin")
    path = schedule.plist_path()
    assert home in path.parents
    domain, service = f"gui/{os.getuid()}", f"gui/{os.getuid()}/{schedule.LABEL}"

    fake = FakeLaunchctl()
    assert schedule.install("/opt/tools/bin/evo-agents", run=fake) == path
    assert fake.calls == [["print", service], ["bootstrap", domain, str(path)]]
    assert plistlib.loads(path.read_bytes()) == schedule.build_plist("/opt/tools/bin/evo-agents")
    assert kg_env.is_dir()

    fake.calls.clear()
    schedule.install("/opt/tools/bin/evo-agents", run=fake)
    assert fake.calls == [["print", service], ["bootout", service], ["bootstrap", domain, str(path)]]

    fake.calls.clear()
    assert schedule.uninstall(run=fake) is True
    assert fake.calls == [["print", service], ["bootout", service]]
    assert not path.exists()
    assert schedule.uninstall(run=fake) is False

    with pytest.raises(schedule.ScheduleError, match="bootstrap.*Input/output error"):
        schedule.install("/opt/tools/bin/evo-agents", run=FakeLaunchctl(fail="bootstrap"))


def test_schedule_install_needs_launchd(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(schedule.ScheduleError, match="kg sync --due --all$"):
        schedule.install(run=FakeLaunchctl())
    assert not schedule.plist_path().exists()


def test_schedule_install_on_windows_names_the_command_for_task_scheduler(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(schedule.ScheduleError, match="Task Scheduler.*kg sync --due --all$"):
        schedule.install(run=FakeLaunchctl())
    assert not schedule.plist_path().exists()


# The scheduled sync


def test_the_scheduled_sync_pushes_what_is_due_and_asks_the_hub_for_no_build(tmp_path, kg_env, hub_for, capsys):
    hub = hub_for("alpha").hubs["alpha"]
    remember(load_project_at(harness(tmp_path, "alpha", [source(tmp_path, "docs", "1h")]), kg_env))
    assert main([*schedule.SYNC_ARGS, "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    (outcome,) = body["projects"]
    assert "prune" not in body  # the machine keeps nothing to prune: the hub prunes what it holds
    assert outcome["due"] == ["docs"] and [run["status"] for run in outcome["runs"]] == ["ok"]
    assert len(hub.ingested) == 1 and hub.builds == 0  # the hub queues its build with the run it ingests
    assert not list(kg_env.rglob("*.sqlite"))  # no corpus, graph or memo on this machine
