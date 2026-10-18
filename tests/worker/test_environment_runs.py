"""A run whose claim names a plugin's environment, on the worker end to end and without Postgres, as criterion a7 of
the plan hub-agent-plugins names it: the daemon in this process (the in-memory hub of ``tests.worker.fake_hub``, the
checkouts of ``tests.worker.test_plan_runs``) checks the environment in the run's preflight, pulls its image through
the container engine, and runs the agent of a plugin's ACP runtime in the container: the agent gets the run's leased
variable and none of the daemon's own, writes the step's file in the run's worktree, and the daemon verifies, pushes
and ends the run done, then removes what is left of the run's containers. A worker without a container engine fails
the run in its preflight with cause environment, before its agent starts. The engine is the fake of
``tests.worker.test_environments``, which runs the agent on this machine with the variables its command names."""

from __future__ import annotations

import json
import sys

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import runs  # noqa: E402
from evo_agents.worker.environments import ENGINE_VARIABLE  # noqa: E402
from tests.worker.test_credentials import env_lease  # noqa: E402
from tests.worker.test_environments import DECLARATION as ENVIRONMENT  # noqa: E402
from tests.worker.test_environments import IMAGE, LEASED_VALUE, OWNER_SECRET, Engine  # noqa: E402
from tests.worker.test_plan_runs import PLAN, machine, wait_for, with_daemon  # noqa: E402, F401
from tests.worker.test_plugin_runtime_run import (  # noqa: E402
    DECLARATION,
    FAKE_AGENT,
    FOUND,
    RESULT,
    RUNTIME,
    plugin_hub,
    reported,
)

TURN = [
    {"env": "FAKE_API_KEY"},
    {"env": "OWNER_SECRET"},
    {"write": {"a.txt": "a\n", f"{runs.RESULT_DIR}/result.json": json.dumps(RESULT)}},
    {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Wrote a.txt."}}},
]


def agent_files(machine, **env: str):  # noqa: F811
    """The fake agent's scenario and log, and the daemon's own secret, in the daemon's environment."""
    scenario, log = machine.tmp / "acp-scenario.json", machine.tmp / "acp.jsonl"
    scenario.write_text(json.dumps({"turns": [TURN]}), encoding="utf-8")
    machine.env.update({"FAKE_ACP_SCENARIO": str(scenario), "FAKE_ACP_LOG": str(log), "FAKE_ACP_MODELS": "small,large"})
    machine.env.update({"OWNER_SECRET": OWNER_SECRET, **env})
    machine.env.pop("FAKE_API_KEY", None)
    return log


def queue(hub) -> int:
    """A step run of the plugin's runtime in the plugin's environment, with its env lease of FAKE_API_KEY."""
    run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
    hub.runs[run_id]["spec"].update(runtime=RUNTIME, plugin_runtime=dict(DECLARATION), environment=dict(ENVIRONMENT))
    hub.leases[run_id] = {"leases": [env_lease(1, "FAKE_API_KEY", LEASED_VALUE)], "missing": []}
    return run_id


def test_environment_run_of_a_plugins_runtime_runs_its_agent_in_the_container(machine):  # noqa: F811
    engine = Engine(machine.tmp)
    log = agent_files(machine, **engine.env)
    found = {}

    async def body(hub, daemon):
        plugin_hub(hub)
        await wait_for(lambda: reported(hub) == FOUND, "the heartbeat to report the plugin's runtime")
        assert runs.ENVIRONMENTS in hub.last_heartbeat["features"]
        run_id = queue(hub)
        assert await hub.wait_state(run_id, "done", "failed", "review") == "done", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    hub, run_id = found["hub"], found["run"]
    events = hub.runs[run_id]["events"]
    chunks = [event["body"]["content"]["text"] for event in events if event["kind"] == "agent_message_chunk"]
    assert chunks == ["env FAKE_API_KEY: set", "env OWNER_SECRET: unset", "Wrote a.txt."]
    texts = hub.texts(run_id)
    assert "Preflight passed: 1 repo(s) and environment sandbox/box, its engine and image." in texts
    assert any(
        text.startswith("The agent runs in environment sandbox/box of plugin sandbox 0.3.0: image ") for text in texts
    )
    assert machine.origin("alpha", "show", "feat/alpha:a.txt") == "a", "the daemon pushed what the agent wrote"

    assert engine.asked() == ["info", "image", "pull", "run", "ps"], "the release found no container left to remove"
    (run_call,) = [call for call in engine.calls() if call["args"][0] == "run"]
    worktree = str(machine.directory(run_id))
    assert run_call["image"] == IMAGE and run_call["program"] == sys.executable and run_call["cwd"] == worktree
    assert f"type=bind,src={worktree},dst={worktree}" in run_call["args"]
    assert run_call["args"][-1] == str(FAKE_AGENT)
    assert "FAKE_API_KEY" in run_call["env"] and "OWNER_SECRET" not in run_call["env"] and "PATH" in run_call["env"]
    assert not any(LEASED_VALUE in arg or OWNER_SECRET in arg for arg in run_call["args"])
    assert log.exists(), "the agent ran"


def test_environment_run_on_a_worker_without_a_container_engine_fails_its_preflight(machine):  # noqa: F811
    log = agent_files(machine, **{ENGINE_VARIABLE: "no-such-engine"})
    found = {}

    async def body(hub, daemon):
        plugin_hub(hub)
        await wait_for(lambda: reported(hub) == FOUND, "the heartbeat to report the plugin's runtime")
        run_id = queue(hub)
        assert await hub.wait_state(run_id, "done", "failed", "review") == "failed", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    run = found["hub"].runs[found["run"]]
    assert run["failure_cause"] == "environment"
    assert run["error"] == (
        "preflight: environment sandbox/box of plugin sandbox needs a container engine: "
        f"{ENGINE_VARIABLE}=no-such-engine is not an executable on PATH; the agent did not start"
    )
    assert not log.exists(), "the agent never started"
