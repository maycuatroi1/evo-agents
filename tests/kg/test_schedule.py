"""The LaunchAgent plist. launchctl is never called: every command goes to a recording fake."""

import datetime as dt
import json
import os
import plistlib
import subprocess
import sys

import pytest

from evo_agents.cli import main
from evo_agents.kg import retention, schedule
from evo_agents.kg.scrub import holding_sources
from evo_agents.kg.sync import sync_project
from tests.kg.conftest import doc

SYNC = ["kg", "sync", "--due", "--all", "--build"]
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
    with pytest.raises(schedule.ScheduleError, match="kg sync --due --all --build"):
        schedule.install(run=FakeLaunchctl())
    assert not schedule.plist_path().exists()


def test_schedule_install_on_windows_names_the_command_for_task_scheduler(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(schedule.ScheduleError, match="Task Scheduler.*kg sync --due --all --build"):
        schedule.install(run=FakeLaunchctl())
    assert not schedule.plist_path().exists()


# Pruning with the scheduled sync (evo_agents.kg.retention)


def sync_due_all(capsys, *extra: str) -> tuple[int, dict]:
    code = main(["kg", "sync", "--due", "--all", "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


def superseded(project, fake) -> list[str]:
    """Two syncs of the same document: the first run's log is old as soon as a period of 0 says so."""
    runs = []
    for text in ("one", "two"):
        fake.set(items=[doc("a", f"# a\n\n{text}\n")])
        (result,) = sync_project(project)
        runs.append(result.run_id)
    return runs


def test_scheduled_pruning_is_off_until_the_owner_turns_it_on_after_a_count(fake_project, kg_env, capsys):
    project, fake = fake_project
    runs = superseded(project, fake)
    assert retention.load_config(kg_env) == retention.Config()  # off, with the default periods
    assert retention.load_config(kg_env).periods == {"logs": "1d", "history": "1d", "memo": "14d"}
    code, body = sync_due_all(capsys)
    assert code == 0 and "prune" not in body  # the first automatic deletion waits for the owner
    assert len(project.corpus().log_files()) == 2

    zero = ["--logs", "0", "--history", "0", "--memo", "0"]
    assert main(["kg", "prune", "--schedule", "on", *zero]) == 2  # no terminal to ask, no --yes
    assert main(["kg", "prune", "--schedule", "on", "--delete", "--yes"]) == 2
    capsys.readouterr()
    assert main(["kg", "prune", "--schedule", "on", "--yes", *zero]) == 0
    out = capsys.readouterr().out
    assert "run logs: delete 1" in out and "Scheduled pruning is on" in out  # the count comes first
    config = retention.load_config(kg_env)
    assert config.scheduled and config.periods == {"logs": "0", "history": "0", "memo": "0"} and config.changed_at
    assert len(project.corpus().log_files()) == 2  # turning it on deletes nothing yet

    with holding_sources(project.root):  # a sync of the project is running: it waits for the next pass
        code, body = sync_due_all(capsys)
    assert code == 0 and body["prune"]["projects"][0]["busy"] and body["prune"]["ok"]
    code, body = sync_due_all(capsys)
    (pruned,) = body["prune"]["projects"]
    assert code == 0 and pruned["project"] == project.name and pruned["deleted"]["logs"] == 1
    assert [p.name for p in project.corpus().log_files()] == [f"{runs[1]}.jsonl.gz"]
    code, body = sync_due_all(capsys)
    assert body["prune"]["projects"] == []  # pruned less than a day ago

    later = dt.datetime.now(dt.UTC) + retention.EVERY
    (again,) = retention.prune_scheduled(kg_env, now=later)
    assert again.deleted is not None and again.deleted["logs"] == 0

    assert main(["kg", "prune", "--schedule", "off"]) == 0
    assert not retention.load_config(kg_env).scheduled
    assert retention.prune_scheduled(kg_env, now=later + retention.EVERY) is None


def test_a_config_the_schedule_cannot_read_fails_the_scheduled_run(kg_env, capsys):
    kg_env.mkdir(parents=True, exist_ok=True)
    retention.config_path(kg_env).write_text('{"scheduled": true, "periods": {"logs": "3 days"}}')
    assert main(["kg", "sync", "--due", "--all"]) == 1
    assert "FAIL prune: period '3 days' is not 0 or a number" in capsys.readouterr().out
    assert main(["kg", "prune", "--all"]) == 2
    assert "period '3 days'" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("value", "seconds"), [("0", 0), ("30m", 1800), ("12h", 43200), ("3d", 259200), ("2w", 1209600), (" 1d ", 86400)]
)
def test_periods_are_zero_or_a_number_with_a_unit(value, seconds):
    assert retention.parse_period(value).total_seconds() == seconds


@pytest.mark.parametrize("value", ["3", "-1d", "1y", "", "d", None, 3])
def test_a_period_without_a_unit_or_with_another_is_refused(value):
    with pytest.raises(retention.RetentionError):
        retention.parse_period(value)
