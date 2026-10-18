"""The core adapter of ACP (``evo_agents.worker.runtimes.acp``) against a fake ACP agent (tests/fixtures/acp/
fake_agent.py), a real process speaking JSON-RPC on its stdin and stdout: the handshake, the session and its model,
the events of a turn in the hub's shapes, permissions granted, a message of the owner as the next turn, an interrupt,
an agent that dies, a session loaded again, and what a profile or a missing secret refuses before the agent starts."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from evo_agents.plugins.runtimes import AcpRuntime
from evo_agents.worker.adapter import AgentEvent, Outcome, RunContext
from evo_agents.worker.runtimes import acp, common

FAKE_AGENT = Path(__file__).parents[1] / "fixtures" / "acp" / "fake_agent.py"
PROMPT = "Write a.txt."
DECLARATION = AcpRuntime(
    runtime="sandbox/fake",
    plugin="sandbox",
    version="0.2.0",
    title="Fake ACP agent",
    command=(sys.executable, str(FAKE_AGENT)),
    models=("small", "large"),
    secrets=("FAKE_API_KEY",),
)
Adapter = acp.adapter_class(DECLARATION)
AXES = ("credential", "tool", "skill", "worker", "project", "model", "runtime")


def profile(instructions: str = "", **allow) -> dict:
    permissions = {axis: {"mode": "all"} for axis in AXES}
    for axis, items in allow.items():
        permissions[axis] = {"mode": "allowlist", "allow": list(items)}
    return {
        "handle": "helper",
        "version": 2,
        "name": "Helper",
        "instructions": instructions,
        "permissions": permissions,
    }


class Agent:
    """The fake agent's files: its scenario and the log of every message it read and wrote."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.scenario_path = tmp_path / "scenario.json"
        self.log_path = tmp_path / "acp.jsonl"
        self.worktree = tmp_path / "worktree"
        self.worktree.mkdir()

    def turns(self, *turns: list[dict]) -> None:
        self.scenario_path.write_text(json.dumps({"turns": list(turns)}), encoding="utf-8")

    def env(self, **extra: str) -> dict:
        return {
            **os.environ,
            "FAKE_ACP_SCENARIO": str(self.scenario_path),
            "FAKE_ACP_LOG": str(self.log_path),
            "FAKE_ACP_MODELS": "small,large",
            **extra,
        }

    def context(self, *, resume: str | None = None, env: dict | None = None, **run) -> RunContext:
        spec = {"id": 41, "project": "demo", "step_key": "9", "runtime": DECLARATION.runtime, **run}
        return RunContext(run=spec, worktree=self.worktree, prompt=PROMPT, env=env or self.env(), resume_session=resume)

    def messages(self, direction: str, method: str | None = None) -> list[dict]:
        if not self.log_path.exists():
            return []
        lines = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        found = [line["message"] for line in lines if line["dir"] == direction]
        return [message for message in found if method is None or message.get("method") == method]


@pytest.fixture
def agent(tmp_path) -> Agent:
    found = Agent(tmp_path)
    found.turns([])
    return found


async def collect(adapter, during=None) -> tuple[list[AgentEvent], Outcome]:
    events: list[AgentEvent] = []
    await adapter.start()

    async def read() -> None:
        async for event in adapter.events():
            events.append(event)

    reader = asyncio.create_task(read())
    if during is not None:
        await during(adapter, events)
    await asyncio.wait_for(reader, 30)
    return events, await asyncio.wait_for(adapter.wait(), 30)


def texts(events: list[AgentEvent]) -> list[str]:
    return [event.body["content"]["text"] for event in events if event.kind == "agent_message_chunk"]


USAGE = {"sessionUpdate": "usage_update", "used": 5300, "size": 200000, "cost": {"amount": 0.045, "currency": "USD"}}
TOOL = {
    "sessionUpdate": "tool_call",
    "toolCallId": "call-1",
    "title": "Write a.txt",
    "kind": "edit",
    "status": "pending",
}
DONE = {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "completed"}
PLAN = {"sessionUpdate": "plan", "entries": [{"content": "Write a.txt", "priority": "high", "status": "pending"}]}
THOUGHT = {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}}
ANSWER = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Wrote a.txt."}}
MODES = {"sessionUpdate": "current_mode_update", "currentModeId": "code"}


def test_acp_turn_maps_the_updates_to_the_hubs_events_and_grants_permissions(agent):
    agent.turns(
        [
            {"update": PLAN},
            {"update": THOUGHT},
            {"update": TOOL},
            {"permission": "edit"},
            {"write": {"a.txt": "a\n"}},
            {"update": DONE},
            {"update": MODES},
            {"update": USAGE},
            {"update": ANSWER},
        ]
    )
    adapter = Adapter(agent.context())
    events, outcome = asyncio.run(collect(adapter))
    assert [event.kind for event in events] == [
        "plan",
        "agent_thought_chunk",
        "tool_call",
        "agent_message_chunk",
        "tool_call_update",
        "output",
        "usage_update",
        "agent_message_chunk",
    ]
    assert events[0].body == {"entries": PLAN["entries"]} and events[2].body == {
        k: v for k, v in TOOL.items() if k != "sessionUpdate"
    }
    assert texts(events) == ["permission: allow-once", "Wrote a.txt."]
    assert events[5].body == {"raw": MODES}
    assert (agent.worktree / "a.txt").read_text() == "a\n"
    assert outcome == Outcome(
        True,
        None,
        {"cost": 0.045, common.PEAK_CONTEXT: 5300, "context_window": 200000},
        "permission: allow-onceWrote a.txt.",
    )
    assert adapter.session_id == "sess-1" and adapter.agent_info["name"] == "fake-acp"
    (started,) = agent.messages("in", "initialize")
    assert started["params"]["protocolVersion"] == 1
    assert started["params"]["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }
    (new,) = agent.messages("in", "session/new")
    assert new["params"] == {"cwd": str(agent.worktree.resolve()), "mcpServers": []}
    (prompt,) = agent.messages("in", "session/prompt")
    blocks = prompt["params"]["prompt"]
    assert blocks == [common.text_block(common.HEADLESS_NOTE), common.text_block(PROMPT)]
    assert agent.messages("in", "session/set_config_option") == [], "no model asked, the agent's own"


def test_acp_model_goes_through_the_agents_config_option_of_category_model(agent):
    adapter = Adapter(agent.context(model="large"))
    _, outcome = asyncio.run(collect(adapter))
    assert outcome.completed
    (chosen,) = agent.messages("in", "session/set_config_option")
    assert chosen["params"] == {"sessionId": "sess-1", "configId": "model", "value": "large"}
    limited = Adapter(agent.context(profile=profile(model=["small"])))
    asyncio.run(collect(limited))
    assert agent.messages("in", "session/set_config_option")[-1]["params"]["value"] == "small"


def test_acp_model_the_agent_refuses_or_cannot_take_fails_the_start(agent):
    with pytest.raises(
        RuntimeError, match="the agent did not take model huge: session/set_config_option: no model huge"
    ):
        asyncio.run(Adapter(agent.context(model="huge")).start())
    without = Adapter(agent.context(model="large", env=agent.env(FAKE_ACP_MODELS="")))
    with pytest.raises(RuntimeError, match="offers no config option of category model"):
        asyncio.run(without.start())
    assert agent.messages("in", "session/prompt") == [], "no prompt went out"


def test_acp_owner_message_during_a_turn_starts_the_next_turn(agent):
    gate = agent.tmp / "go"
    agent.turns([{"update": THOUGHT}, {"wait_file": str(gate)}], [{"update": ANSWER}])
    adapter = Adapter(agent.context())

    async def during(adapter, events):
        while not events:
            await asyncio.sleep(0.02)
        await adapter.send("Also write b.txt.")
        gate.write_text("go")

    events, outcome = asyncio.run(collect(adapter, during))
    assert outcome.completed and outcome.summary == "Wrote a.txt."
    prompts = agent.messages("in", "session/prompt")
    assert [prompt["params"]["prompt"] for prompt in prompts][1] == [common.text_block("Also write b.txt.")]
    with pytest.raises(common.AgentFinished):
        asyncio.run(adapter.send("too late"))


def test_acp_interrupt_cancels_the_session_and_the_turn_ends_interrupted(agent):
    agent.turns([{"update": THOUGHT}, {"wait_cancel": True}])
    adapter = Adapter(agent.context())

    async def during(adapter, events):
        while not events:
            await asyncio.sleep(0.02)
        await adapter.interrupt()

    _, outcome = asyncio.run(collect(adapter, during))
    assert outcome.completed is False and outcome.error == "interrupted"
    assert agent.messages("in", "session/cancel") == [
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "sess-1"}}
    ]


def test_acp_agent_that_dies_fails_the_turn_with_its_exit_status_and_its_stderr_in_the_log(agent):
    agent.turns([{"stderr": "boom: out of tokens"}, {"exit": 3}])
    events, outcome = asyncio.run(collect(Adapter(agent.context())))
    assert outcome.completed is False
    assert outcome.error.startswith("sandbox/fake: the agent ended") and "(exit status 3)" in outcome.error
    assert events[-1].kind == "output" and events[-1].body == {"raw": {"stderr": ["boom: out of tokens"]}}


def test_acp_turn_that_stops_otherwise_than_end_turn_did_not_complete(agent):
    agent.turns([{"stop": "max_tokens"}])
    _, outcome = asyncio.run(collect(Adapter(agent.context())))
    assert outcome == Outcome(False, "sandbox/fake ended its turn max_tokens", None, None)


def test_acp_session_goes_on_with_session_load_and_its_replay_is_no_event(agent):
    adapter = Adapter(agent.context(resume="sess-0", env=agent.env(FAKE_ACP_LOAD="1")))
    agent.turns([{"update": ANSWER}])
    events, outcome = asyncio.run(collect(adapter))
    assert outcome.completed and adapter.loaded and adapter.session_id == "sess-0"
    assert texts(events) == ["Wrote a.txt."], "the replayed history is not the run's"
    (loaded,) = agent.messages("in", "session/load")
    assert loaded["params"]["sessionId"] == "sess-0" and agent.messages("in", "session/new") == []
    fresh = Adapter(agent.context(resume="sess-0"))
    asyncio.run(collect(fresh))
    assert fresh.session_id == "sess-1" and not fresh.loaded


def test_acp_first_prompt_carries_the_profiles_instructions_after_the_headless_note(agent):
    adapter = Adapter(agent.context(profile=profile(instructions="Answer in Vietnamese.")))
    asyncio.run(collect(adapter))
    (prompt,) = agent.messages("in", "session/prompt")
    note = prompt["params"]["prompt"][0]["text"]
    assert note.startswith(common.HEADLESS_NOTE) and note.endswith("Answer in Vietnamese.")


def test_acp_profile_that_limits_tools_or_skills_is_refused_and_one_of_models_applies():
    assert Adapter.profile_refusal({}) is None and Adapter.profile_refusal({"profile": profile()}) is None
    assert Adapter.profile_refusal({"profile": profile(model=["small"], instructions="Be brief.")}) is None
    assert Adapter.profile_refusal({"profile": profile(tool=["read"])}) == (
        "the ACP agent of sandbox/fake keeps its own tools, which the protocol cannot turn off, and agent profile "
        "@helper (version 2) limits them"
    )
    assert "tools and skills" in Adapter.profile_refusal({"profile": profile(tool=["read"], skill=[])})
    assert "is not one agent profile" in Adapter.profile_refusal(
        {"model": "large", "profile": profile(model=["small"])}
    )


def test_acp_missing_secret_is_refused_before_the_agent_starts(agent):
    without = {key: value for key, value in agent.env().items() if key != "FAKE_API_KEY"}
    why = Adapter.environment_refusal(agent.context(env=without))
    assert why == (
        "the agent of sandbox/fake needs FAKE_API_KEY, which neither the run's leases nor the worker's environment "
        "set: bind a secret of that name to the project, or set it in the daemon's environment"
    )
    assert Adapter.environment_refusal(agent.context(env={**without, "FAKE_API_KEY": "k"})) is None


def test_acp_runtime_is_available_when_its_program_is_on_path(tmp_path, monkeypatch):
    found = Adapter.detect()
    assert found.available and found.reason is None and Adapter.models() == ["small", "large"]
    missing = acp.adapter_class(AcpRuntime("sandbox/gone", "sandbox", "0.2.0", "Gone", ("no-such-acp-agent",)))
    monkeypatch.setenv("PATH", str(tmp_path))
    gone = missing.detect()
    assert not gone.available
    assert gone.reason == "no-such-acp-agent is not on PATH, for sandbox/gone of plugin sandbox"
    assert missing.models() is None and missing.runtime == "sandbox/gone" and missing.interactive is False


def test_acp_program_missing_on_the_agents_path_fails_the_start(agent, tmp_path):
    missing = acp.adapter_class(AcpRuntime("sandbox/gone", "sandbox", "0.2.0", "Gone", ("no-such-acp-agent",)))
    with pytest.raises(RuntimeError, match="no-such-acp-agent is not on PATH"):
        asyncio.run(missing(agent.context(env={**agent.env(), "PATH": str(tmp_path)})).start())


def test_acp_updates_map_to_events_and_permissions_pick_an_allowing_option():
    assert acp.events_of({"sessionUpdate": "user_message_chunk", "content": {}}) == []
    (event,) = acp.events_of(USAGE)
    assert event.kind == "usage_update" and event.body == {k: v for k, v in USAGE.items() if k != "sessionUpdate"}
    assert acp.events_of({"sessionUpdate": "notice", "severity": "warning"})[0].kind == "output"
    always = [{"optionId": "no", "kind": "reject_once"}, {"optionId": "yes", "kind": "allow_always"}]
    assert acp.permission_choice(always) == {"outcome": {"outcome": "selected", "optionId": "yes"}}
    assert acp.permission_choice([{"optionId": "no", "kind": "reject_always"}]) == {"outcome": {"outcome": "cancelled"}}
    assert acp.permission_choice(None) == {"outcome": {"outcome": "cancelled"}}
    assert acp.model_option([{"id": "m", "category": "model"}]) == "m" and acp.model_option(None) is None
