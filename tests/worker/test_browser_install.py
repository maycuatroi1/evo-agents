"""Chromium installed from the hub: the daemon carries out its owner's Install browser (feature browser_install) with
the install of `evo-agents worker browser install`, against the fake hub and a stand-in for the install.

- A request new to the daemon installs once, in the background, quiet; a failure is reported with why in the
  heartbeat's browser_install until the hub ends the request, and a new request installs again.
- Once Chromium is in, the next heartbeat says the feature browser and its Chromium, which ends the request on the hub.
- A machine without fontconfig (an Ubuntu 22.04 server image) gets it, a font and the other libraries it lacks with
  apt-get download, no sudo, and a fontconfig file of its own that Chromium is pointed at; a machine with
  /etc/fonts/fonts.conf keeps its own.
"""

from __future__ import annotations

import platform
import subprocess

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker.browser import chromium, install
from evo_agents.worker.home import WorkerHome
from tests.worker.test_plan_runs import machine, wait_for, with_daemon  # noqa: F401 (machine)

FIRST = "2026-10-25T07:00:00.123456Z"
SECOND = "2026-10-25T07:05:00.654321Z"
NO_NPX = "npx is not on PATH: install Node.js 18 or later"


def test_an_install_browser_request_installs_in_the_background_reports_a_failure_and_a_new_one_installs(
    machine,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    chrome = tmp_path / "chrome"
    chrome.write_text("#!/bin/sh\n", encoding="utf-8")
    chrome.chmod(0o755)
    calls: list[bool] = []

    def fake_install(home, env, *, max_browsers=None, say=print, quiet=False):
        calls.append(quiet)
        if len(calls) == 1:
            raise install.InstallError(NO_NPX)
        installed = install.Installed(str(chrome), "153.0.8010.12", True, 2)
        install.write(home, installed)
        return installed, True

    monkeypatch.setattr(install, "install", fake_install)

    async def body(hub, daemon):
        def said() -> dict:
            return hub.last_heartbeat or {}

        assert "browser_install" in said()["features"] and "browser" not in said()["features"]
        assert "browser_install" not in said()
        hub.browser_install = {"requested_at": FIRST}
        await wait_for(lambda: (said().get("browser_install") or {}).get("ended") == "failed", "the failure reported")
        assert said()["browser_install"] == {"requested_at": FIRST, "ended": "failed", "error": NO_NPX}
        assert "browser" not in said()["features"]
        hub.browser_install = None  # the hub ended the request
        await wait_for(lambda: "browser_install" not in said(), "the report of the ended request dropped")
        hub.browser_install = {"requested_at": SECOND}
        await wait_for(lambda: "browser" in said()["features"], "the feature browser")
        assert said()["browser"] == {"chromium": "153.0.8010.12", "display": "headless", "max": 2, "sandbox": True}
        assert said()["browser_install"] == {"requested_at": SECOND}, "an open request the hub still carries"
        assert calls == [True, True], "one quiet install per request, none again for the same one"

    with_daemon(machine, body)


@pytest.mark.parametrize("has_fontconfig", [False, True])
def test_a_machine_without_fontconfig_gets_it_a_font_and_a_fontconfig_file_chromium_is_pointed_at(
    tmp_path, monkeypatch, has_fontconfig
):
    home = WorkerHome(tmp_path / "home")
    home.ensure()
    system_conf = tmp_path / "etc-fonts" / "fonts.conf"
    if has_fontconfig:
        system_conf.parent.mkdir()
        system_conf.write_text("<fontconfig/>", encoding="utf-8")
    looks = iter([["libfontconfig.so.1", "libwayland-server.so.0", "libxcb-randr.so.0", "libxkbcommon.so.0"], []])
    fetched: list[str] = []

    def fake_run(command, env, **options):
        if command[:2] == ["apt-get", "download"]:
            fetched.append(command[2])
            return subprocess.CompletedProcess(command, 100 if command[2].endswith("t64") else 0)  # an Ubuntu 22.04
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(install.sys, "platform", "linux")
    monkeypatch.setattr(install.shutil, "which", lambda name, **options: f"/usr/bin/{name}")
    monkeypatch.setattr(install, "missing_libraries", lambda *args: next(looks))
    monkeypatch.setattr(install, "_run", fake_run)
    monkeypatch.setattr(install, "SYSTEM_FONTS_CONF", system_conf)

    libraries = install.linux_libraries(tmp_path / "chrome", home, {}, lambda _: None)
    root = install.directory(home) / install.LIBRARIES
    assert libraries == str(root / "usr" / "lib" / f"{platform.machine()}-linux-gnu")
    wanted = {"libfontconfig1", "fontconfig-config", "fonts-dejavu-core", "libwayland-server0", "libxcb-randr0"}
    assert wanted | {"libxkbcommon0", "libasound2"} <= set(fetched)
    conf = root / chromium.FONTS_CONF
    env = chromium.environment({"PATH": "/usr/bin"}, libraries)
    assert env["LD_LIBRARY_PATH"] == libraries
    if has_fontconfig:
        assert not conf.exists() and "FONTCONFIG_FILE" not in env
        return
    text = conf.read_text(encoding="utf-8")
    assert f"<dir>{root}/usr/share/fonts</dir>" in text and f"<cachedir>{root}/fontconfig-cache</cachedir>" in text
    assert env["FONTCONFIG_FILE"] == str(conf)
    assert chromium.environment({"FONTCONFIG_FILE": "/mine.conf"}, libraries)["FONTCONFIG_FILE"] == "/mine.conf"
    assert "FONTCONFIG_FILE" not in chromium.environment({}, None)
