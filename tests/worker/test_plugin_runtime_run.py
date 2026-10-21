"""A run of a plugin's runtime on the worker, end to end and without Postgres, as criterion a6 of the plan
hub-agent-plugins names it: the daemon in this process (the in-memory hub of ``tests.worker.fake_hub``, the checkouts
of ``tests.worker.test_plan_runs``) takes the plugins its owner accepted from the settings of the heartbeat answer and
the declarations of their runtimes, finds the program of the fake ACP agent (tests/fixtures/acp/fake_agent.py) and
reports the runtime with its models, says it runs plugins' runtimes, and runs a step run of the runtime through the
core adapter of ACP with the declaration its claim carries: the agent writes the step's file, the verify passes, the
branch is pushed, and the run is done. A variable the declaration needs that neither the leases nor the daemon's
environment set fails the run before its agent starts, with cause credentials."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import runs  # noqa: E402
from evo_agents.worker.daemon import Daemon  # noqa: E402
from evo_agents.worker.home import WorkerConfig  # noqa: E402
from tests.worker.fake_hub import TOKEN  # noqa: E402
from tests.worker.test_plan_runs import PLAN, machine, wait_for, with_daemon  # noqa: E402, F401

FAKE_AGENT = Path(__file__).parents[1] / "fixtures" / "acp" / "fake_agent.py"
RUNTIME = "sandbox/fake"
DECLARATION = {
    "runtime": RUNTIME,
    "plugin": "sandbox",
    "version": "0.2.0",
    "title": "Fake ACP agent",
    "command": [sys.executable, str(FAKE_AGENT)],
    "models": ["small", "large"],
    "secrets": ["FAKE_API_KEY"],
}
SETTINGS = {"slots": 1, "name": "mac", "labels": [], "plugins": ["sandbox"]}
FOUND = {"available": True, "version": None, "reason": None, "models": ["small", "large"]}
RESULT = {"verify_commands": ["test -f a.txt"], "summary": "Wrote a.txt."}
TURN = [
    {
        "update": {
            "sessionUpdate": "plan",
            "entries": [{"content": "Write a.txt", "priority": "high", "status": "pending"}],
        }
    },
    {"update": {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Write a.txt", "kind": "edit"}},
    {"permission": "edit"},
    {"env": "FAKE_API_KEY"},
    {"write": {"a.txt": "a\n", f"{runs.RESULT_DIR}/result.json": json.dumps(RESULT)}},
    {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"}},
    {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Wrote a.txt."}}},
]


def agent_files(machine, **env: str) -> Path:  # noqa: F811
    """The fake agent's scenario in the daemon's environment, which its agents inherit; the agent's log."""
    scenario, log = machine.tmp / "acp-scenario.json", machine.tmp / "acp.jsonl"
    scenario.write_text(json.dumps({"turns": [TURN]}), encoding="utf-8")
    machine.env.update({"FAKE_ACP_SCENARIO": str(scenario), "FAKE_ACP_LOG": str(log), "FAKE_ACP_MODELS": "small,large"})
    machine.env.update(env)
    return log


def plugin_hub(hub) -> None:
    """The settings of a worker whose owner accepted plugin sandbox, and the declaration of its runtime fake."""
    hub.settings = dict(SETTINGS)
    hub.plugin_runtimes = [dict(DECLARATION)]


def queue(hub) -> int:
    run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
    hub.runs[run_id]["spec"].update(runtime=RUNTIME, plugin_runtime=dict(DECLARATION))
    return run_id


def reported(hub) -> dict | None:
    return ((hub.last_heartbeat or {}).get("runtimes") or {}).get(RUNTIME)


def test_plugin_runtime_run_goes_through_the_adapter_of_acp_on_a_worker_that_reports_it(machine):  # noqa: F811
    log = agent_files(machine, FAKE_API_KEY="k-" + "1" * 20)
    found = {}

    async def body(hub, daemon):
        plugin_hub(hub)
        await wait_for(lambda: reported(hub) == FOUND, "the heartbeat to report the plugin's runtime")
        assert runs.PLUGIN_RUNTIMES in hub.last_heartbeat["features"]
        run_id = queue(hub)
        assert await hub.wait_state(run_id, "done", "failed", "review") == "done", hub.texts(run_id)
        found.update(hub=hub, run=run_id, config=daemon.config)

    with_daemon(machine, body)
    hub, run_id = found["hub"], found["run"]
    assert found["config"].plugins == ["sandbox"]
    assert machine.home.load_config().plugins == ["sandbox"], "config.json keeps the owner's plugins"
    events = hub.runs[run_id]["events"]
    kinds = [event["kind"] for event in events if event["kind"] != "system"]
    assert kinds[:3] == ["plan", "tool_call", "agent_message_chunk"] and "tool_call_update" in kinds
    chunks = [event["body"]["content"]["text"] for event in events if event["kind"] == "agent_message_chunk"]
    assert chunks == ["permission: allow-once", "env FAKE_API_KEY: set", "Wrote a.txt."]
    assert any(text.startswith(f"{RUNTIME} started in ") for text in hub.texts(run_id))
    assert machine.origin("alpha", "show", "feat/alpha:a.txt") == "a"
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    (prompt,) = [line["message"] for line in lines if line["message"].get("method") == "session/prompt"]
    assert "Alpha file" in prompt["params"]["prompt"][1]["text"], "the step's prompt goes to the agent"


def test_plugin_runtime_run_without_a_secret_its_agent_needs_fails_before_the_agent_starts(machine):  # noqa: F811
    log = agent_files(machine)
    machine.env.pop("FAKE_API_KEY", None)
    found = {}

    async def body(hub, daemon):
        plugin_hub(hub)
        await wait_for(lambda: reported(hub) == FOUND, "the heartbeat to report the plugin's runtime")
        run_id = queue(hub)
        assert await hub.wait_state(run_id, "done", "failed", "review") == "failed", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    run = found["hub"].runs[found["run"]]
    assert run["failure_cause"] == "credentials"
    assert run["error"].startswith(f"the agent of {RUNTIME} needs FAKE_API_KEY, which neither the run's leases")
    assert not log.exists(), "the agent never started"


def test_plugin_runtime_declarations_of_plugins_the_owner_did_not_accept_or_unreadable_are_left_out(machine):  # noqa: F811
    config = WorkerConfig(url="http://hub.invalid", worker_id=1, name="mac", projects=["demo"], plugins=["sandbox"])
    daemon = Daemon(machine.home, config, TOKEN, adapters={}, env=machine.env)
    other = {**DECLARATION, "runtime": "other/fake", "plugin": "other"}
    broken = {**DECLARATION, "runtime": "sandbox/broken", "command": []}
    daemon._take_worker_state({"drain": False, "plugin_runtimes": [DECLARATION, other, broken]})
    assert sorted(daemon.plugin_adapters) == [RUNTIME]
    cls = daemon.plugin_adapters[RUNTIME]
    assert (cls.runtime, cls.declaration.command) == (RUNTIME, tuple(DECLARATION["command"]))
    assert daemon._detect()[RUNTIME] == FOUND
    daemon._take_worker_state({"drain": False, "settings": {**SETTINGS, "plugins": []}})
    assert daemon.config.plugins == [] and daemon.plugin_adapters == {}, "no plugin accepted, no runtime of one"
    daemon._take_worker_state({"drain": False, "settings": {**SETTINGS, "plugins": "sandbox"}})
    assert daemon.config.plugins == [], "settings this daemon cannot read are left"
