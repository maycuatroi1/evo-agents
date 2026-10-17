"""What the model reads from the MCP tools in a real Claude Code session.

``claude -p`` calls plan_handoff of a hub served by ``hub serve`` on a local port, through ``evo-agents hub mcp``, and
kg_context of ``evo-agents kg serve`` on a small graph. The tool result Claude Code puts before the model is, byte for
byte, the text of ``evo-agents hub plan handoff`` and of ``evo-agents kg query``: Claude Code shows the model a
result's structuredContent instead of its text when it has one, which is why the servers send the text alone. The
PostToolUse hook of evo-kg runs on the same call and records the labels it read from the text block's _meta.

It needs ``claude`` signed in on this machine and EVO_REAL_CLAUDE=1, and costs one short Haiku session; CI skips it.
The session loads no settings, plugins or MCP servers of the machine (``--setting-sources project`` in an empty
directory, ``--strict-mcp-config``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request

import pytest

from evo_agents.kg.build import build_project
from evo_agents.kg.sessions import READ_META
from evo_agents.kg.sync import sync_project
from tests.hub import live, pg
from tests.hub.test_handoff import rollout
from tests.hub.test_mcp import sign_in
from tests.hub.test_plans import PROJECT, registration
from tests.kg.projects import make_project

pytestmark = [
    pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON),
    pytest.mark.skipif(
        os.environ.get("EVO_REAL_CLAUDE") != "1" or shutil.which("claude") is None,
        reason="EVO_REAL_CLAUDE=1 runs a real claude -p session on this machine",
    ),
]

PROMPT = (
    'Call mcp__evo-hub__plan_handoff with plan_id "rollout" and no other argument. Then call mcp__evo-kg__kg_context '
    'with query "search" and no other argument. Call no other tool. Then answer with the one word: done.'
)
SAVE_PAYLOAD = "import pathlib, sys\npathlib.Path(sys.argv[1]).open('a').write(sys.stdin.read().strip() + '\\n')\n"


def put(url: str, path: str, body: dict, headers: dict) -> None:
    data = json.dumps(body).encode()
    sent = urllib.request.Request(
        url + path, data=data, method="PUT", headers={**headers, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(sent, timeout=30) as response:
        assert response.status in (200, 201)


def shown_to_the_model(stream: str) -> dict[str, tuple[dict, str]]:
    """Per tool called in a stream-json transcript: its input and the text of its result as the model got it."""
    calls, shown = {}, {}
    for line in stream.splitlines():
        event = json.loads(line)
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls[block["id"]] = (block["name"], block["input"])
            elif block.get("type") == "tool_result":
                name, arguments = calls[block["tool_use_id"]]
                if not name.startswith("mcp__evo-"):
                    continue  # ToolSearch, which loads the deferred MCP tools
                content = block["content"]
                if isinstance(content, list):
                    assert all(part["type"] == "text" for part in content), content
                    content = "".join(part["text"] for part in content)
                shown[name] = (arguments, content)
    return shown


def test_claude_code_shows_the_model_the_text_the_cli_prints(hub_db, tmp_path, monkeypatch):
    kg_home = tmp_path / "kg-home"
    monkeypatch.setenv("EVO_KG_HOME", str(kg_home))
    (tmp_path / "kg").mkdir()
    project = make_project(tmp_path / "kg", kg_home, level="public", app_level="customer")
    sync_project(project)
    assert build_project(project).ok
    bound = ["--project", str(project.harness.root), "--backend", "local", "--sink", "wide"]

    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN) as served:
        admin = live.bearer(live.insert_token(hub_db, live.ADMIN))
        token = live.insert_token(hub_db, "alice")
        put(served.url, f"/v1/projects/{PROJECT}", registration(), admin)
        put(
            served.url, f"/v1/admin/projects/{PROJECT}/grants/alice", {"role": "writer", "max_level": "internal"}, admin
        )
        body = rollout()
        for step in body["steps"]:
            step |= {"repo": "evo-agents", "what": f"do {step['title'].lower()}"}
        put(served.url, f"/v1/projects/{PROJECT}/plans/rollout", {"body": body}, live.bearer(token))
        home = sign_in(tmp_path / "home", served.url, "alice", token)

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        saver, payloads = tmp_path / "save_payload.py", tmp_path / "post-tool.jsonl"
        saver.write_text(SAVE_PAYLOAD, encoding="utf-8")
        python = sys.executable
        servers = {
            "evo-hub": {
                "command": python,
                "args": ["-m", "evo_agents", "hub", "mcp", "--project", PROJECT],
                "env": {"HOME": str(home)},
            },
            "evo-kg": {
                "command": python,
                "args": ["-m", "evo_agents", "kg", "serve", *bound],
                "env": {"EVO_KG_HOME": str(kg_home)},
            },
        }
        hooks = [
            {"type": "command", "command": f"{python} -m evo_agents kg hook post-tool"},
            {"type": "command", "command": f"{python} {saver} {payloads}"},
        ]
        settings = {"hooks": {"PostToolUse": [{"matcher": "mcp__evo-kg__.*", "hooks": hooks}]}}
        (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        ran = subprocess.run(
            [
                "claude",
                "-p",
                PROMPT,
                "--model",
                "haiku",
                "--output-format",
                "stream-json",
                "--verbose",
                "--setting-sources",
                "project",
                "--strict-mcp-config",
                "--mcp-config",
                str(tmp_path / "mcp.json"),
                "--settings",
                str(tmp_path / "settings.json"),
                "--allowedTools",
                "mcp__evo-hub__plan_handoff,mcp__evo-kg__kg_context",
            ],  # fmt: skip
            cwd=session_dir,
            env={**os.environ, "EVO_SPOOL_THRESHOLD": "off"},  # the hooks of evo-hub stay out anyway
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert ran.returncode == 0, ran.stderr
        shown = shown_to_the_model(ran.stdout)
        assert set(shown) == {"mcp__evo-hub__plan_handoff", "mcp__evo-kg__kg_context"}, shown

        arguments, handoff = shown["mcp__evo-hub__plan_handoff"]
        assert arguments.get("plan_id") == "rollout"
        options = [f"--{key.replace('_', '-')}={value}" for key, value in arguments.items() if key != "plan_id"]
        printed = pg.cli(
            ["hub", "plan", "handoff", "rollout", "--project", PROJECT, *options], pg.clean_env(HOME=str(home))
        )
        assert printed.returncode == 0, printed.stderr
        assert handoff == printed.stdout and handoff.startswith("# Handoff of plan rollout, revision 1")

    arguments, context = shown["mcp__evo-kg__kg_context"]
    pairs = [f"--arg={key}={json.dumps(value)}" for key, value in arguments.items()]
    query = ["kg", "query", "kg_context", *bound, *pairs]
    printed = pg.cli(query, pg.clean_env(EVO_KG_HOME=str(kg_home)))
    assert printed.returncode == 0, printed.stderr
    assert context + "\n" == printed.stdout and "app/search.py" in context

    (payload,) = [json.loads(line) for line in payloads.read_text(encoding="utf-8").splitlines()]
    (block,) = payload["tool_response"]
    read = block["_meta"][READ_META]
    assert block["text"] == context and read["project"] == "demo" and read["results_read"] > 0
    record = json.loads((kg_home / "sessions" / f"{payload['session_id']}.json").read_text(encoding="utf-8"))
    assert record["level"] == read["label"]["level"] == "customer" and record["projects"] == ["demo"]
    assert record["results_read"] == read["results_read"]
