"""The browser of a run on the worker (plan agent-browser, criterion a3), with the Chromium `evo-agents worker browser
install` put in Playwright's cache: these tests start it for real, and skip only on a machine where that command never
ran. The daemon runs in this process with the fake runtime and the in-memory hub of ``tests.worker.test_plan_runs``.

- The install writes what it found to the worker's home, and again changes nothing (``browser.install``).
- Chromium starts with the same flags every time, --headless=new, no --enable-automation, the password store basic,
  and its DevTools on 127.0.0.1 alone; navigator.webdriver is not true there and its user agent says no HeadlessChrome.
- A run dispatched with browser gets EVO_BROWSER_CDP, its Chromium in ``runs/<run>/browser`` while the run is held,
  and nothing of it once the run ends or is parked: no process, no directory.
- One more browser than the max fails with cause environment, no Chromium installed with cause missing_tool, which the
  preflight says first, with node missing from the run's PATH.
- A daemon names the feature browser and its Chromium in its heartbeat once the install ran, and at its start stops
  the Chromium a daemon before it left and removes the browser directories of the runs it does not hold.
- A run without browser is as before (criterion a10): no Chromium, no directory, no EVO_BROWSER_CDP even when the
  daemon's own environment has one."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

import aiohttp

from evo_agents.cli import main as cli_main
from evo_agents.hub import runs
from evo_agents.worker.browser import chromium, install
from evo_agents.worker.browser import session as browser_session
from evo_agents.worker.home import WorkerHome
from evo_agents.worker.runner.common import RunFailed
from tests.worker.test_plan_runs import (
    PLAN,
    ask,
    machine,  # noqa: F401 (a fixture)
    plan_body,
    run_repos,
    wait_for,
    with_daemon,
)
from tests.worker.test_preflight import stopped

GONE_WITHIN = 10.0  # seconds what is left of a closed Chromium has to vanish from the process table


@pytest.fixture(scope="module")
def installed(tmp_path_factory) -> install.Installed:
    """The Chromium of Playwright's cache, installed once for the module into a worker home of its own."""
    if install.locate(os.environ) is None:
        pytest.skip(f"no Chromium of {install.PLAYWRIGHT} in Playwright's cache: run `{install.COMMAND}` first")
    home = WorkerHome(tmp_path_factory.mktemp("browser-home") / "worker")
    found, changed = install.install(home, os.environ, say=lambda _: None)
    assert changed
    return found


def holder(home: WorkerHome) -> types.SimpleNamespace:
    """What a BrowserSession reads of its daemon."""
    return types.SimpleNamespace(home=home, env=dict(os.environ), browsers={})


def naming(text: str) -> list[str]:
    """The processes whose command line names ``text``."""
    table = subprocess.run(["ps", "-A", "-o", "pid=,command="], capture_output=True, text=True, check=True).stdout
    return [line.strip() for line in table.splitlines() if text in line]


def none_left(directory: Path) -> list[str]:
    """Wait for no process to name ``directory``; those left after GONE_WITHIN seconds."""
    deadline = time.monotonic() + GONE_WITHIN
    while (left := naming(str(directory))) and time.monotonic() < deadline:
        time.sleep(0.1)
    return left


def group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def page_says(cdp: str) -> tuple[object, str]:
    """navigator.webdriver and navigator.userAgent of a new page of the browser at ``cdp``."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.put(f"{cdp}/json/new?about:blank") as answered:
            target = await answered.json(content_type=None)
        async with session.ws_connect(target["webSocketDebuggerUrl"]) as socket:
            expression = "JSON.stringify([navigator.webdriver, navigator.userAgent])"
            await socket.send_json(
                {"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True}}
            )
            while True:
                message = await socket.receive_json(timeout=20)
                if message.get("id") == 1:
                    webdriver, agent = json.loads(message["result"]["result"]["value"])
                    return webdriver, agent


async def version_of(cdp: str) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(f"{cdp}/json/version") as answered:
            return await answered.json(content_type=None)


# The install and the flags


def test_browser_session_install_again_changes_nothing_and_max_changes_the_max_alone(installed, tmp_path):
    home = WorkerHome(tmp_path / "worker")
    install.write(home, installed)
    assert install.install(home, os.environ, say=lambda _: None) == (installed, False)
    assert install.read(home) == installed and installed.executable == str(install.locate(os.environ))
    assert install.report(home) == {
        "chromium": installed.version,
        "display": "headless",
        "max": installed.max,
        "sandbox": installed.sandbox,
    }
    assert 1 <= installed.max <= runs.MAX_BROWSERS
    assert mode(install.directory(home)) == 0o700 and mode(install.directory(home) / install.STATE) == 0o600

    two, changed = install.install(home, os.environ, max_browsers=2, say=lambda _: None)
    assert changed and two == install.read(home) and two.max == 2
    assert (two.executable, two.version, two.sandbox) == (installed.executable, installed.version, installed.sandbox)
    assert install.install(home, os.environ, max_browsers=2, say=lambda _: None) == (two, False)
    with pytest.raises(install.InstallError, match="--max takes 1 to"):
        install.install(home, os.environ, max_browsers=0, say=lambda _: None)
    assert install.default_max(cores=8, memory=6 * install.GIB) == 4  # by memory: 1.5 GiB each
    assert install.default_max(cores=4, memory=64 * install.GIB) == 3  # a core left for the agents
    assert install.default_max(cores=1, memory=1) == 1


def test_browser_session_install_command_says_what_it_installed_and_again_that_nothing_changed(
    installed, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("EVO_WORKER_HOME", str(tmp_path / "worker"))
    assert cli_main(["worker", "browser", "install", "--max", "2"]) == 0
    first = capsys.readouterr().out
    assert f"Chromium {installed.version} at {installed.executable}: headless" in first
    assert "2 browser(s) at once." in first and "Restart the worker's daemon" in first
    assert cli_main(["worker", "browser", "install"]) == 0
    assert "Already installed: nothing changed." in capsys.readouterr().out
    assert install.read(WorkerHome(tmp_path / "worker")).max == 2, "again without --max keeps the max"
    with pytest.raises(SystemExit):
        cli_main(["worker", "browser", "install", "--max", str(runs.MAX_BROWSERS + 1)])
    assert "takes 1 to" in capsys.readouterr().err


def test_browser_session_flags_are_the_same_every_start_and_turn_automation_off(tmp_path):
    profile = tmp_path / "profile"
    first = chromium.flags(profile, "153.0.8010.12", sandbox=True)
    assert first == chromium.flags(profile, "153.0.8010.12", sandbox=True)
    assert "--headless=new" in first and f"--user-data-dir={profile}" in first
    assert "--remote-debugging-port=0" in first and "--password-store=basic" in first
    assert "--disable-blink-features=AutomationControlled" in first
    assert not any(flag.startswith("--enable-automation") for flag in first)
    assert "--no-sandbox" not in first
    assert "--no-sandbox" in chromium.flags(profile, "153.0.8010.12", sandbox=False)
    (agent,) = [flag.removeprefix("--user-agent=") for flag in first if flag.startswith("--user-agent=")]
    assert "Chrome/153.0.0.0" in agent and "Headless" not in agent


# The session


def test_browser_session_opens_on_127_0_0_1_and_close_leaves_no_process_and_no_directory(installed, tmp_path):
    home = WorkerHome(tmp_path / "worker")
    install.write(home, installed)
    daemon = holder(home)
    browser = browser_session.BrowserSession(daemon, 7)
    found = {}

    async def go():
        cdp = await browser.open()
        assert await browser.open() == cdp, "a second open starts nothing"
        found["pid"] = browser.process.pid
        found["version"] = await version_of(cdp)
        found["page"] = await page_says(cdp)
        found["cdp"] = cdp
        found["held"] = dict(daemon.browsers)
        found["modes"] = (mode(browser.directory), mode(browser.profile))
        found["listening"] = listening(int(cdp.rsplit(":", 1)[1]))
        await browser.close()

    asyncio.run(go())
    assert found["cdp"].startswith("http://127.0.0.1:")
    assert found["held"] == {7: browser} and daemon.browsers == {}
    assert found["modes"] == (0o700, 0o700)
    assert installed.version in found["version"]["Browser"]
    webdriver, agent = found["page"]
    assert webdriver is not True, "no --enable-automation, AutomationControlled off"
    assert "HeadlessChrome" not in agent and f"Chrome/{installed.version.split('.')[0]}." in agent
    if found["listening"] is not None:
        assert found["listening"] and all(address.startswith("127.0.0.1:") for address in found["listening"])
    assert not browser.directory.exists() and browser.cdp is None
    assert group_gone(found["pid"])
    assert none_left(browser.directory) == []


def listening(port: int) -> list[str] | None:
    """The addresses a process listens on at ``port``, as lsof says them; None without lsof."""
    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    said = subprocess.run([lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    return [line.split()[8] for line in said.splitlines()[1:] if len(line.split()) > 8]


def test_browser_session_past_the_max_fails_with_cause_environment_and_without_chromium_missing_tool(
    installed, tmp_path
):
    home = WorkerHome(tmp_path / "worker")
    install.write(home, dataclasses.replace(installed, max=1))
    daemon = holder(home)
    daemon.browsers[3] = object()  # the browser of another run, the one this worker holds at once
    with pytest.raises(RunFailed) as failed:
        asyncio.run(browser_session.BrowserSession(daemon, 4).open())
    assert failed.value.fields["cause"] == "environment" and "its max" in failed.value.error
    assert not browser_session.run_directory(home, 4).exists() and list(daemon.browsers) == [3]

    bare = WorkerHome(tmp_path / "bare")
    with pytest.raises(RunFailed) as failed:
        asyncio.run(browser_session.BrowserSession(holder(bare), 4).open())
    assert failed.value.fields["cause"] == "missing_tool" and install.COMMAND in failed.value.error


def test_browser_session_preflight_names_chromium_and_node_with_cause_missing_tool(installed, tmp_path):
    bare, empty = WorkerHome(tmp_path / "bare"), tmp_path / "bin"
    empty.mkdir()
    chromium_missing, node_missing = install.problems(bare, str(empty))
    assert chromium_missing.cause == node_missing.cause == "missing_tool"
    assert "Chromium is not installed" in chromium_missing.message and install.COMMAND in chromium_missing.message
    assert "node" in node_missing.message and install.COMMAND in node_missing.message

    home = WorkerHome(tmp_path / "worker")
    install.write(home, installed)
    node = shutil.which("node")
    assert node is not None, "the agent's browser tools run on node"
    assert install.problems(home, str(Path(node).parent)) == []


def test_browser_session_sweep_stops_the_chromium_a_daemon_left_and_keeps_what_it_holds(installed, tmp_path):
    home = WorkerHome(tmp_path / "worker")
    install.write(home, installed)
    left = browser_session.BrowserSession(holder(home), 5)
    asyncio.run(left.open())  # and the daemon that opened it dies: nobody closes it
    pid = left.process.pid
    threading.Thread(target=left.process.wait, daemon=True).start()  # as init reaps what a dead daemon left
    held = browser_session.run_directory(home, 6)
    (held / browser_session.PROFILE).mkdir(parents=True)
    stray = home.runs_dir / "notes" / browser_session.DIRECTORY
    stray.mkdir(parents=True)

    assert asyncio.run(browser_session.sweep(home, held={6})) == [5]
    assert not left.directory.exists() and group_gone(pid)
    assert none_left(left.directory) == []
    assert held.is_dir() and stray.is_dir(), "a held run's browser, and what is no run's, stay"
    assert asyncio.run(browser_session.sweep(home)) == [6] and not held.exists()


# Runs on the daemon


def test_browser_session_a_run_with_browser_gets_its_chromium_while_held_and_nothing_of_it_after(installed, machine):
    install.write(machine.home, installed)
    env_path, held, release = machine.tmp / "env.json", machine.tmp / "held", machine.tmp / "release"
    code = (
        "import json, os, urllib.request as u; "
        "print(json.load(u.urlopen(os.environ['EVO_BROWSER_CDP'] + '/json/version'))['Browser'])"
    )
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"env": str(env_path)},
                {"sh": f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"},
                {"touch": str(held)},
                {"wait_for": str(release)},
                {"result": {"summary": "Looked with the browser."}},
            ]
        }
    )
    found = {}

    async def body(hub, daemon):
        beat = hub.last_heartbeat
        assert runs.BROWSER in beat["features"] and beat["browser"] == installed.report()
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()), browser=True)
        await wait_for(held.exists, "the agent to look at its browser")
        directory = browser_session.run_directory(machine.home, run_id)
        found.update(run=run_id, directory=directory, held=list(daemon.browsers))
        found["while"] = (directory.is_dir(), mode(directory), mode(directory / browser_session.PROFILE))
        found["pid"] = daemon.browsers[run_id].process.pid
        release.touch()
        assert await hub.wait_state(run_id, "done", "failed") == "done", hub.texts(run_id)
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(hub=hub, after=dict(daemon.browsers))

    with_daemon(machine, body)
    run_id, directory, hub = found["run"], found["directory"], found["hub"]
    assert found["held"] == [run_id] and found["while"] == (True, 0o700, 0o700)
    cdp = json.loads(env_path.read_text(encoding="utf-8"))[browser_session.VARIABLE]
    assert cdp.startswith("http://127.0.0.1:")
    looked = machine.commands()[0]
    assert looked["exit"] == 0 and installed.version in looked["stdout"], looked
    texts = " ".join(hub.texts(run_id))
    assert "Chromium and node for its browser" in texts and browser_session.VARIABLE in texts
    assert found["after"] == {} and not directory.exists()
    assert group_gone(found["pid"]) and none_left(directory) == []


def test_browser_session_a_parked_run_closes_its_chromium(installed, machine):
    install.write(machine.home, installed)
    machine.scenarios({f"plan:{PLAN}": [ask()]})
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()), browser=True)
        assert await hub.wait_state(run_id, "waiting", "done", "failed") == "waiting", hub.texts(run_id)
        directory = browser_session.run_directory(machine.home, run_id)
        assert run_id in daemon.browsers and directory.is_dir(), "a run waiting for its owner keeps its browser"
        found.update(directory=directory, pid=daemon.browsers[run_id].process.pid)
        hub.park(run_id)
        await wait_for(lambda: run_id not in daemon.runs, "the parked run to free its slot")
        found["after"] = dict(daemon.browsers)

    with_daemon(machine, body)
    assert found["after"] == {} and not found["directory"].exists()
    assert group_gone(found["pid"]) and none_left(found["directory"]) == []


def test_browser_session_a_run_without_browser_is_as_before_and_the_start_sweeps_what_was_left(installed, machine):
    install.write(machine.home, installed)
    left = browser_session.run_directory(machine.home, 41) / browser_session.PROFILE
    left.mkdir(parents=True)  # the browser of a run a daemon before this one held
    machine.env[browser_session.VARIABLE] = "http://127.0.0.1:9"  # the daemon's own, of no run
    env_path, held, release = machine.tmp / "env.json", machine.tmp / "held", machine.tmp / "release"
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"env": str(env_path)},
                {"touch": str(held)},
                {"wait_for": str(release)},
                {"result": {"summary": "Done without a browser."}},
            ]
        }
    )
    found = {}

    async def body(hub, daemon):
        found["swept"] = not left.parent.exists()
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        assert "browser" not in hub.runs[run_id]["spec"]
        await wait_for(held.exists, "the agent to start")
        found["while"] = (dict(daemon.browsers), browser_session.run_directory(machine.home, run_id).exists())
        release.touch()
        assert await hub.wait_state(run_id, "done", "failed") == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    assert found["swept"], "the daemon's start removed it"
    assert found["while"] == ({}, False)
    assert browser_session.VARIABLE not in json.loads(env_path.read_text(encoding="utf-8"))
    texts = " ".join(found["hub"].texts(found["run"]))
    assert "Chromium" not in texts and browser_session.VARIABLE not in texts
    assert found["hub"].moves(found["run"]) == ["leased", "running", "verifying", "done"]


def test_browser_session_preflight_without_chromium_fails_the_run_before_its_agent(machine):
    found = {}

    async def body(hub, daemon):
        found["beat"] = hub.last_heartbeat
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()), browser=True)
        assert await hub.wait_state(run_id, "done", "failed") == "failed", hub.texts(run_id)
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    assert runs.BROWSER not in found["beat"]["features"] and "browser" not in found["beat"]
    hub, run_id = found["hub"], found["run"]
    run = stopped(hub, run_id, machine)
    assert run["failure_cause"] == "missing_tool"
    assert "Chromium is not installed on this worker" in run["error"] and install.COMMAND in run["error"]
    assert not browser_session.run_directory(machine.home, run_id).exists()
