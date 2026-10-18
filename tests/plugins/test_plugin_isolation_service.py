"""The plugin host as production runs it: ``evo-agents hub plugin-host``, its uvicorn entry with the event loop uvicorn
picks (uvloop, which uvicorn[standard] of the hub-server extra installs), pulling its plugins from a hub.

On agents.omelet.tech the backend of an installed plugin never started: the host ran on uvloop, whose subprocess_exec
refused the user, group and extra_groups arguments the host passed to give a backend its user, while every test of
the host ran on asyncio's own loop (``asyncio.run``). This test starts the service itself, as a subprocess, against a
stand-in for the hub's /internal/plugin-host/ routes, and checks through the service's own routes that:

- the sample, the hostile and the faulty fixtures start and answer;
- a backend holds no file descriptor but 0, 1 and 2, although uvloop ignores close_fds and the host holds an
  inheritable descriptor (the review of PR #72: one reached the backend as fd 12, with copies of its stdio);
- as root (CI's plugin-host-root job, in the hub's image) each runs under a user of its own, and the hostile one
  reads no secret, writes into no other plugin's directories and signals no other user's process; the limits hold;
- stopping the service (SIGTERM, as Docker stops it) leaves no process of a plugin's user behind, a child that left
  its backend's process group with setsid() included.

Not as root, the service runs with ``--allow-shared-user``, as on a developer's machine, and the hostile check is the
one isolation ``process`` promises (``test_plugin_isolation``). It needs the hub-server extra (uvicorn, fastapi).
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import evo_agents
from evo_agents.plugins.environment import LIMITS, NPROC, UID_BASE
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
)

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="the plugin host runs backends through a POSIX trampoline"),
    pytest.mark.skipif(
        importlib.util.find_spec("uvicorn") is None or importlib.util.find_spec("fastapi") is None,
        reason="the plugin host service needs the hub-server extra",
    ),
]
LINUX = sys.platform.startswith("linux")
TOKEN = HUB_SECRETS["EVO_HUB_PLUGIN_HOST_TOKEN"]
PLUGINS = {"sample": (2, {"api_key": SIBLING_SECRET}), "hostile": (3, {"api_key": "its-own"}), "faulty": (4, {})}
START_WITHIN = 180.0  # seconds for the three backends to run: three environments to build


class Hub(BaseHTTPRequestHandler):
    """The hub's routes for the plugin host (``evo_agents.hub.server.plugin_host``), serving the fixture plugins."""

    minted: list[str] = []
    reports: list[list[dict]] = []

    def log_message(self, *args) -> None:
        pass

    def _answer(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> list[str] | None:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._answer(401, b'{"detail":"the shared secret"}')
            return None
        return self.path.removeprefix("/internal/plugin-host/").split("/")

    def do_GET(self) -> None:
        route = self._route()
        if route == ["plugins"]:
            items = [_item(name, plugin_id, config) for name, (plugin_id, config) in PLUGINS.items()]
            self._answer(200, json.dumps(items).encode())
        elif route and len(route) == 3 and route[0] == "plugins" and route[2] == "bundle" and route[1] in PLUGINS:
            self._answer(200, bundle(route[1]), "application/gzip")
        elif route is not None:
            self._answer(404, b'{"detail":"not found"}')

    def do_POST(self) -> None:
        route = self._route()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if route and len(route) == 3 and route[0] == "plugins" and route[2] == "token":
            Hub.minted.append(route[1])
            self._answer(200, json.dumps({"token": f"evp_service-{len(Hub.minted)}"}).encode())
        elif route == ["status"]:
            Hub.reports.append(json.loads(body or b"[]"))
            self._answer(200, b"null")
        elif route is not None:
            self._answer(404, b'{"detail":"not found"}')


def _item(name: str, plugin_id: int, config: dict) -> dict:
    """One plugin as GET /internal/plugin-host/plugins gives it: no token in ``init``, which the host asks for at
    each start."""
    payload = init(name, plugin_id, config)
    del payload["hub"]["token"]
    return {"name": name, "tier": "isolated", "backend": manifest(name).backend, "init": payload}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _request(port: int, method: str, path: str, body: dict | None = None, timeout: float = 70.0) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _call(port: int, plugin: str, method: str, params: dict) -> dict:
    status, answer = _request(port, "POST", f"/v1/host/plugins/{plugin}/call", {"method": method, "params": params})
    assert status == 200 and "result" in answer, (plugin, method, status, answer)
    return answer["result"]


class Service:
    """``evo-agents hub plugin-host`` in a subprocess, its output in a file for the assertion messages."""

    def __init__(self, hub_url: str, data_dir: Path, log: Path, inherited: int):
        self.port, self.log = _free_port(), log
        command = [sys.executable, "-m", "evo_agents", "hub", "plugin-host", "--hub-url", hub_url]
        command += ["--data-dir", str(data_dir), "--host", "127.0.0.1", "--port", str(self.port)]
        if not as_root():
            command.append("--allow-shared-user")
        source = str(Path(evo_agents.__file__).resolve().parents[1])  # this checkout, not an installed copy
        env = {**os.environ, **HUB_SECRETS, "PYTHONPATH": source, "EVO_HUB_LOG_LEVEL": "INFO"}
        self.output = log.open("wb")
        self.proc = subprocess.Popen(
            command, env=env, cwd=str(data_dir), stdout=self.output, stderr=subprocess.STDOUT, pass_fds=(inherited,)
        )

    def tail(self) -> str:
        if not self.output.closed:
            self.output.flush()
        return "\n".join(self.log.read_text(errors="replace").splitlines()[-40:])

    def wait_live(self, within: float = 60.0) -> None:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            assert self.proc.poll() is None, f"the plugin host exited {self.proc.returncode}:\n{self.tail()}"
            try:
                if _request(self.port, "GET", "/health/live", timeout=2)[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.2)
        pytest.fail(f"the plugin host never answered /health/live:\n{self.tail()}")

    def wait_running(self, names: set[str], within: float = START_WITHIN) -> list[dict]:
        deadline, backends = time.monotonic() + within, []
        while time.monotonic() < deadline:
            assert self.proc.poll() is None, f"the plugin host exited {self.proc.returncode}:\n{self.tail()}"
            backends = _request(self.port, "GET", "/v1/host/backends", timeout=10)[1]["backends"]
            if {item["plugin"] for item in backends if item["state"] == "running"} >= names:
                return backends
            time.sleep(0.5)
        pytest.fail(f"the backends never ran: {json.dumps(backends)}\n{self.tail()}")

    def stop(self) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.output.close()
        return self.proc.returncode


def test_plugin_isolation_host_service_runs_backends_as_production_does(plugin_root, tmp_path):
    if os.environ.get(ROOT_VARIABLE):  # the hub's image: uvicorn[standard], so uvicorn picks uvloop, as in production
        assert importlib.util.find_spec("uvloop") is not None, "the hub's image runs the plugin host on uvloop"
    Hub.minted, Hub.reports = [], []
    hub = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    inherited = os.open(tmp_path / "inherited", os.O_CREAT | os.O_WRONLY, 0o600)  # the host's, inheritable there
    service = Service(f"http://127.0.0.1:{hub.server_address[1]}", plugin_root, tmp_path / "plugin-host.log", inherited)
    os.close(inherited)
    try:
        service.wait_live()
        backends = service.wait_running(set(PLUGINS))
        assert _call(service.port, "sample", "ping", {}) == {}
        report = json.loads(text(_call(service.port, "hostile", "tool", tool("probe"))))
        limits = json.loads(text(_call(service.port, "faulty", "tool", tool("limits")))) if LINUX else {}
        token = text(_call(service.port, "faulty", "tool", tool("token")))
        if as_root():  # a child that left the backend's group: only killing the plugin's user (kill_user) ends it
            orphan = int(text(_call(service.port, "faulty", "tool", tool("orphan"))))
            assert orphan in processes_of(UID_BASE + PLUGINS["faulty"][0])
    finally:
        code = service.stop()
        hub.shutdown()
        hub.server_close()

    # uvicorn shuts down (the lifespan stops every backend), then raises the SIGTERM it caught again
    assert code in (0, -signal.SIGTERM) and "Application shutdown complete" in service.tail(), (code, service.tail())
    assert sorted(Hub.minted) == sorted(PLUGINS) and token.startswith("evp_service-")  # one token per start
    assert Hub.reports, "the host reported no status to the hub"
    assert report["fds"] == [0, 1, 2], report  # no descriptor of the host's, whatever its loop passed on
    if not as_root():  # isolation process: a developer's machine; the host's /proc is readable, its env is not passed
        assert {item["isolation"] for item in backends} == {"process"}
        assert not [where for where in report["found"] if where.startswith("env:")], report
        assert not [where for where, value in report["found"].items() if SIBLING_SECRET in value], report
        return
    assert {item["isolation"] for item in backends} == {"uid"}
    assert report["found"] == {} and report["wrote"] == {} and report["signalled"] == {}, report
    assert report["uid"] == UID_BASE + PLUGINS["hostile"][0]
    assert limits["core"] == 0 and limits["nproc"] == NPROC and limits["as"] <= LIMITS["as"], limits
    for plugin_id, _ in PLUGINS.values():
        assert processes_of(UID_BASE + plugin_id) == [], f"processes of plugin user {UID_BASE + plugin_id} remain"
