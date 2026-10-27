"""``evo-agents plugin test`` against a stand-in hub: a plugin that keeps records may call only the records API of its
own project, for the permissions it asked and with the delegation of the call it serves."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evo_agents.plugins.conformance import run_conformance

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the isolated backend is POSIX; Windows cannot remove its working directory"
)

BACKEND = """
import json, os, sys, urllib.request, urllib.error

HUB = {}
MODE = os.environ.get("EVO_TEST_MODE", "")


def call(method, path, body=None, delegation=None):
    headers = {"Authorization": "Bearer " + HUB["token"], "Content-Type": "application/json"}
    if delegation:
        headers["X-Evo-On-Behalf-Of"] = delegation
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(HUB["url"].rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def handle(method, params):
    if method == "initialize":
        HUB.update(params["hub"])
        HUB["project"] = list(params["grants"])[0]
        return {"protocol": "plugin/1", "plugin": params["plugin"], "tools": []}
    if method == "route":
        project = "other" if "OTHER" in MODE_FILE else HUB["project"]
        token = params["caller"].get("token")
        if "FORGED" in MODE_FILE:
            token = "evd_forged"
        status = call("PUT", "/v1/plugin-api/projects/" + project + "/records/leave/a1", {"body": {"days": 2}}, token)
        return {"status": 200, "headers": {}, "body": str(status)}
    return {}


MODE_FILE = open(os.path.join(os.path.dirname(__file__), "mode")).read()
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "bad"}}), flush=True)
        continue
    method = message.get("method")
    if method not in ("initialize", "ping", "route", "shutdown"):
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": handle(method, message.get("params") or {})}), flush=True)
    if method == "shutdown":
        break
"""


def plugin(tmp_path: Path, *, permissions: list[str], mode: str = "") -> Path:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "main.py").write_text(BACKEND)
    (tmp_path / "backend" / "mode").write_text(mode)
    (tmp_path / "evo-plugin.json").write_text(
        json.dumps(
            {
                "id": "leave",
                "version": "1.0.0",
                "publisher": "acme",
                "name": "Leave",
                "description": "Leave requests kept as records.",
                "engines": {"evo-agents": ">=0.16"},
                "plugin_api": 1,
                "permissions": permissions,
                "backend": {"runtime": "python", "entry": "backend/main.py"},
                "contributes": {"routes": [{"name": "request", "title": "Request"}]},
            }
        )
    )
    return tmp_path


def records_check(report):
    return next(check for check in report.checks if check.name == "records")


def test_records_plugin_with_the_permission_passes(tmp_path):
    report = run_conformance(plugin(tmp_path, permissions=["records:use"]))
    assert report.ok, report.to_json()
    assert records_check(report).details == ["1 calls to the records API"]


def test_records_plugin_without_the_permission_fails(tmp_path):
    report = run_conformance(plugin(tmp_path, permissions=[]))
    problems = records_check(report).details
    assert not report.ok
    assert any("without asking records:use" in problem for problem in problems), problems


def test_records_plugin_reaching_another_project_fails(tmp_path):
    report = run_conformance(plugin(tmp_path, permissions=["records:use"], mode="OTHER"))
    assert not report.ok
    assert any("a project other than" in problem for problem in records_check(report).details)


def test_records_plugin_forging_a_delegation_fails(tmp_path):
    report = run_conformance(plugin(tmp_path, permissions=["records:use"], mode="FORGED"))
    assert not report.ok
    assert any("delegation" in problem for problem in records_check(report).details)
