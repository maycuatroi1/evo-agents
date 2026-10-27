"""``evo-agents plugin test <directory>``: what a plugin must satisfy before a hub runs it, as ``evo-agents kg connector
test`` does for a connector of kg/1.

Checks, in order:

- ``manifest``: evo-plugin.json reads (``evo_agents.plugins.manifest``);
- ``bundle``: the directory packs into a bundle (``evo_agents.plugins.bundle.pack``);
- ``backend``: an isolated plugin that contributes to a point it serves from a backend (routes, MCP tools, jobs,
  events, channels) declares one, and the files it names are in the bundle;
- ``environment``: its virtual environment builds as the plugin host builds it, in a directory of its own, its
  requirements only ``name==version --hash=sha256:...`` lines installed from the wheels in the bundle;
- ``initialize``: started with the host's command and an environment built from nothing, it answers ``initialize``
  within the timeout, naming plugin/1, its id and its version, and lists exactly the MCP tools the manifest
  contributes, each with an input schema;
- ``ping``;
- one check per extension point it serves: each contribution called once with the smallest params plugin/1 allows,
  and the result checked against the method's schema;
- ``unknown-method``: a method plugin/1 lacks gets error -32601, and the backend serves on;
- ``malformed-line``: a line that is not JSON gets error -32700 with id null, or nothing, and the backend serves on;
- ``records``: a stand-in hub (``evo_agents.plugins.fake_hub``) answers the records API on 127.0.0.1; a plugin that
  asks for records without ``records:use`` in its permissions, for another project's, or on behalf of a person
  with a delegation the call did not carry fails here;
- ``stdout``: nothing but plugin/1 responses on stdout, logs going to stderr;
- ``shutdown``: it answers ``shutdown`` and exits within the grace period.

A check that cannot run because an earlier one failed is reported as skipped, and fails. Standard library only.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.plugins import protocol
from evo_agents.plugins.bundle import BundleError, pack
from evo_agents.plugins.config import defaults, fields
from evo_agents.plugins.environment import (
    PlacementError,
    backend_command,
    backend_env,
    build_env,
    extract,
    placement,
    prepare_data,
)
from evo_agents.plugins.fake_hub import RECORDS, FakeHub
from evo_agents.plugins.manifest import MANIFEST_FILE, Manifest, ManifestError, read_manifest
from evo_agents.plugins.process import BackendGone, BackendProcess, BackendTimeout

DEFAULT_TIMEOUT = 10.0  # seconds for each answer
PROJECT = "conformance"
DELEGATION = "evd_conformance-delegation-not-valid-on-any-hub-0000"
TOKEN = "evp_conformance-token-not-valid-on-any-hub-0000000"
SECRET = "conformance-secret-value"


def _caller(manifest: Manifest) -> dict:
    caller = {"login": "conformance", "project": PROJECT}
    return {**caller, "token": DELEGATION} if RECORDS in manifest.permissions else caller


SMALLEST = {
    "route": lambda name, caller: {
        "route": name,
        "request": {"method": "GET", "path": "/", "query": {}, "headers": {}, "body": ""},
        "caller": caller,
    },
    "tool": lambda name, caller: {"tool": name, "arguments": {}, "caller": caller},
    "job": lambda name, caller: {"job": name},
    "event": lambda name, caller: {"event": name, "project": PROJECT, "payload": {}},
    "channel": lambda name, caller: {"channel": name, "project": PROJECT, "notification": {"title": "conformance"}},
}


@dataclass
class Check:
    name: str
    ok: bool
    details: list[str] = field(default_factory=list)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def add(self, name: str, problems: list[str], passed: list[str] | None = None) -> bool:
        self.checks.append(Check(name, not problems, problems or (passed or [])))
        return not problems

    def skip(self, *names: str, why: str) -> None:
        for name in names:
            self.checks.append(Check(name, False, [f"skipped: {why}"]))

    def to_json(self) -> dict:
        return {"ok": self.ok, "checks": [check.__dict__ for check in self.checks]}


def initialize_params(manifest: Manifest, hub_url: str = "http://127.0.0.1:9/") -> dict:
    """What ``initialize`` gives the backend during the checks: the config's defaults, a stand-in for each secret,
    every permission it asks for granted on one project, and a hub that does not answer."""
    config = defaults(manifest.config)
    config.update({name: SECRET for name, secret in fields(manifest.config).items() if secret})
    return {
        "protocol": protocol.PROTOCOL,
        "plugin": {"id": manifest.id, "version": manifest.version},
        "hub": {"url": hub_url, "token": TOKEN, "version": "0.0.0"},
        "config": {"instance": config, "projects": {PROJECT: config}},
        "grants": {PROJECT: list(manifest.permissions)},
    }


def _backend_problems(manifest: Manifest, files: set[str]) -> list[str]:
    serves = manifest.serves()
    if manifest.backend is None:
        if serves:
            return [f"it contributes {', '.join(serves)}, which an isolated plugin serves from a backend; add backend"]
        return []
    named = [manifest.backend["entry"], manifest.backend.get("requirements")]
    return [f"{path} is not in the bundle" for path in named if path is not None and path not in files]


def _initialize_problems(manifest: Manifest, result: dict) -> list[str]:
    problems = protocol.result_problems("initialize", result)
    if problems:
        return problems
    if result["protocol"] != protocol.PROTOCOL:
        problems.append(f"it speaks {result['protocol']}, not {protocol.PROTOCOL}")
    named = (result["plugin"]["id"], result["plugin"]["version"])
    if named != (manifest.id, manifest.version):
        problems.append(f"it says it is {named[0]} {named[1]}, the manifest {manifest.id} {manifest.version}")
    listed = sorted(tool["name"] for tool in result.get("tools") or [])
    declared = sorted(item["name"] for item in manifest.contributions("mcp_tools"))
    if listed != declared:
        problems.append(f"it lists tools {listed or 'none'}, the manifest contributes {declared or 'none'}")
    return problems


class Session:
    """The checks that need the backend running."""

    def __init__(self, manifest: Manifest, process: BackendProcess, report: Report, timeout: float, hub: FakeHub):
        self.manifest, self.process, self.report, self.timeout, self.hub = manifest, process, report, timeout, hub

    async def ask(self, method: str, params: dict) -> tuple[object, list[str]]:
        try:
            return await self.process.call(method, params, self.timeout), []
        except (BackendTimeout, BackendGone, protocol.RemoteError) as exc:
            return None, [f"{method}: {exc}"]

    async def initialize(self) -> bool:
        result, problems = await self.ask("initialize", initialize_params(self.manifest, self.hub.url))
        if not problems:
            problems = _initialize_problems(self.manifest, result)
        return self.report.add("initialize", problems, [f"{protocol.PROTOCOL}, {self.manifest.id}"])

    async def points(self) -> None:
        for point in self.manifest.serves():
            method = protocol.POINT_METHODS[point]
            problems = []
            for item in self.manifest.contributions(point):
                result, failed = await self.ask(method, SMALLEST[method](item["name"], _caller(self.manifest)))
                problems += failed or [f"{item['name']}: {p}" for p in protocol.result_problems(method, result)]
            self.report.add(point, problems, [f"{len(self.manifest.contributions(point))} answered"])

    async def unknown_method(self) -> None:
        try:
            await self.process.call("no-such-method", {}, self.timeout)
            problems = ["it answered a method plugin/1 lacks with a result"]
        except protocol.RemoteError as exc:
            problems = [] if exc.code == protocol.METHOD_NOT_FOUND else [f"error {exc.code}, not -32601"]
        except (BackendTimeout, BackendGone) as exc:
            problems = [str(exc)]
        if not problems:
            problems = (await self.ask("ping", {}))[1]
        self.report.add("unknown-method", problems)

    async def malformed_line(self) -> None:
        before = len(self.process.violations)
        try:
            await self.process.write_raw(b"{this is not json\n")
        except OSError as exc:
            self.report.add("malformed-line", [f"cannot write to it: {exc}"])
            return
        problems = (await self.ask("ping", {}))[1]
        if len(self.process.violations) > before:  # an answer with id null is no violation; parse_line keeps it
            problems += self.process.violations[before:]
        self.report.add("malformed-line", problems)

    async def shutdown(self) -> None:
        result, problems = await self.ask("shutdown", {})
        if not problems:
            problems = protocol.result_problems("shutdown", result)
        try:
            await asyncio.wait_for(self.process.proc.wait(), self.timeout)
        except TimeoutError:
            problems.append(f"it did not exit within {self.timeout:g}s of shutdown")
        self.report.add("shutdown", problems)


async def _run_backend(manifest: Manifest, root: Path, report: Report, timeout: float) -> None:
    with FakeHub(project=PROJECT, token=TOKEN, delegation=DELEGATION, permissions=manifest.permissions) as hub:
        await _run_backend_on(manifest, root, report, timeout, hub)
    calls = [c for c in hub.calls if "/records" in c]
    report.add("records", hub.violations, [f"{len(calls)} calls to the records API"])


async def _run_backend_on(manifest: Manifest, root: Path, report: Report, timeout: float, hub: FakeHub) -> None:
    where = placement(root, manifest.id, manifest.version)
    # The author's own plugin, under the caller's user: root too (CI runs this as root in the hub's image), which the
    # trampoline refuses unless told, so that the plugin host never runs a backend as root by mistake.
    command = backend_command(where, manifest.backend["entry"], keep_root=True)
    process = BackendProcess(command, backend_env(where), where.data)
    try:
        await process.start()
    except OSError as exc:
        report.add("initialize", [f"cannot start {where.python}: {exc}"])
        return
    session = Session(manifest, process, report, timeout, hub)
    try:
        if await session.initialize():
            report.add("ping", (await session.ask("ping", {}))[1])
            await session.points()
            await session.unknown_method()
            await session.malformed_line()
            await session.shutdown()
        else:
            report.skip("ping", "unknown-method", "malformed-line", "shutdown", why="initialize failed")
    finally:
        await process.stop(grace=min(timeout, 5.0))
        tail = list(process.stderr_tail)[-3:]
        report.add("stdout", process.violations[:10], [f"stderr: {' | '.join(tail)}"] if tail else [])


def _static(directory: Path, report: Report):
    try:
        manifest = read_manifest((directory / MANIFEST_FILE).read_bytes())
    except OSError as exc:
        report.add("manifest", [f"cannot read {directory / MANIFEST_FILE}: {exc.strerror or exc}"])
        return None, None
    except ManifestError as exc:
        report.add("manifest", exc.problems)
        return None, None
    report.add("manifest", [], [f"{manifest.id} {manifest.version}, {manifest.tier}"])
    try:
        bundle = pack(directory)
    except BundleError as exc:
        report.add("bundle", [str(exc)])
        return manifest, None
    report.add("bundle", [], [f"{len(bundle.contents.files)} files, sha256 {bundle.sha256[:16]}"])
    return manifest, bundle


def run_conformance(directory: str | Path, *, timeout: float = DEFAULT_TIMEOUT) -> Report:
    """The checks of the plugin in ``directory``."""
    report = Report()
    manifest, bundle = _static(Path(directory).expanduser(), report)
    if bundle is None:
        return report
    files = {entry.path for entry in bundle.contents.files}
    if not report.add("backend", _backend_problems(manifest, files)) or manifest.backend is None:
        if manifest.backend is None and report.ok:
            report.checks[-1].details = ["none: the plugin has no backend to run"]
        return report
    with tempfile.TemporaryDirectory(prefix="evo-plugin-test-") as tmp:
        root = Path(tmp)
        where = placement(root, manifest.id, manifest.version)
        try:
            extract(bundle.data, where.code)
            prepare_data(where, None)
            build_env(where, manifest.backend.get("requirements"))
        except PlacementError as exc:
            report.add("environment", [str(exc)])
            return report
        report.add("environment", [], [f"{where.python}"])
        asyncio.run(_run_backend(manifest, root, report, timeout))
    return report
