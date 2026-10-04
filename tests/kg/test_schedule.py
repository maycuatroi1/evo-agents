"""The LaunchAgent plist. launchctl is never called: every command goes to a recording fake."""

import json
import os
import plistlib
import subprocess
import sys

import pytest

from evo_agents.cli import main
from evo_agents.kg import schedule

SYNC = ["kg", "sync", "--due", "--all", "--build"]


@pytest.fixture
def home(tmp_path, monkeypatch, kg_env):
    """A fake HOME, so ~/.local/bin and ~/Library/LaunchAgents point into tmp_path."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


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


def test_schedule_pushes_to_the_hub_when_the_machine_is_signed_in(home, kg_env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")  # install needs launchd; launchctl is the fake below
    hub = home / ".evo" / "hub"
    hub.mkdir(parents=True)
    (hub / "token").write_text("evh_" + "x" * 43 + "\n")
    (hub / "config.json").write_text(json.dumps({"url": "https://hub.test", "login": "octo"}))
    plist = schedule.build_plist("/opt/tools/bin/evo-agents")
    assert plist["ProgramArguments"] == ["/opt/tools/bin/evo-agents", *SYNC, "--push"]
    assert schedule.build_plist("/opt/tools/bin/evo-agents", push=False)["ProgramArguments"][-1] == "--build"
    assert main(["kg", "schedule", "print"]) == 0
    assert plistlib.loads(capsys.readouterr().out.encode())["ProgramArguments"][-6:] == [*SYNC, "--push"]
    fake = FakeLaunchctl()
    path = schedule.install("/opt/tools/bin/evo-agents", run=fake)
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][-1] == "--push"


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
    assert plist["ProgramArguments"][-5:] == SYNC
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
    with pytest.raises(schedule.ScheduleError, match="kg sync --due --all --build"):
        schedule.install(run=FakeLaunchctl())
    assert not schedule.plist_path().exists()
