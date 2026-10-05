"""The adapters of the three runtimes (``evo_agents.worker.runtimes``), each against a fake of what it drives:

- Claude Code: the real ``ClaudeSDKClient`` of claude-agent-sdk, over a transport that plays the CLI's side of
  stream-json, printing the sample events of research_notes/worker-runtimes.md (``tests.worker.fake_adapter.SAMPLES``,
  with the fields the research cut and the SDK reads put back).
- Codex: an ``AsyncCodex`` of the test's own, whose turns stream the app-server's notifications as openai-codex types
  them, with the content of the research's samples in the shape ``codex app-server`` sends.
- opencode: an HTTP server of the test's own with the routes of ``opencode serve`` (Basic auth, ``/session``,
  ``/event`` as server-sent events, ``prompt_async``, ``abort``, the permission reply), sending the parts of the
  research's samples as ``message.part.updated`` events.

The checks step 9 of the worker-fleet plan names: each runtime gets the settings of full permissions; its events come
out in the hub's kinds; a message reaches the agent when it should (in the turn, or in the next one); a runtime that
is missing or too old is reported unsupported. Around them: interrupt and stop at the turn boundary, the launcher's
own session, ``opencode serve`` on a free port with stdin /dev/null, and ``evo-agents worker selftest``.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evo_agents.worker import adapter as adapter_module
from evo_agents.worker.adapter import Adapter, AgentEvent, Detection, Outcome, RunContext
from evo_agents.worker.runtimes import common
from evo_agents.worker.runtimes import opencode as opencode_module
from evo_agents.worker.runtimes.claude_code import ClaudeCodeAdapter, events_of
from evo_agents.worker.runtimes.codex import CodexAdapter
from evo_agents.worker.runtimes.opencode import OpencodeAdapter, Server
from tests.worker import fake_adapter

try:  # the worker extra; without it the tests of the runtime that needs it skip
    import claude_agent_sdk
except ImportError:
    claude_agent_sdk = None
try:
    from aiohttp import web
except ImportError:
    web = None

PROMPT = "Do step 9."
MESSAGE = "Reply with the single word SECOND."


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def needs(module: str):
    return pytest.importorskip(module, reason="the worker extra is not installed")


def _version_binary(bin_dir: Path, name: str, output: str) -> Path:
    bin_dir.mkdir(exist_ok=True)
    return _script(bin_dir / name, f"print({output!r})\n")


def _context(tmp_path: Path, runtime: str, *, resume: str | None = None, env: dict | None = None, **run) -> RunContext:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("claude", "codex", "opencode"):  # what `which` finds; the fakes never run them
        _script(bin_dir / name, "import sys\nsys.exit(3)\n")
    environment = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", **(env or {})}
    spec = {"id": 41, "project": "demo", "step_key": "9", "title": "adapters", "runtime": runtime, **run}
    return RunContext(run=spec, worktree=worktree, prompt=PROMPT, env=environment, resume_session=resume)


async def _collect(adapter: Adapter, during=None) -> tuple[list[AgentEvent], Outcome]:
    """Start the adapter, read its events to the end (running ``during`` meanwhile), and wait for its outcome."""
    events: list[AgentEvent] = []
    await adapter.start()

    async def read() -> None:
        async for event in adapter.events():
            events.append(event)

    reader = asyncio.create_task(read())
    if during is not None:
        await during(adapter, events)
    await asyncio.wait_for(reader, 10)
    outcome = await asyncio.wait_for(adapter.wait(), 10)
    return events, outcome


def _kinds(events: list[AgentEvent]) -> list[str]:
    return [event.kind for event in events]


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.01)


# Versions, detection and the launcher


def test_versions_compare_by_their_numbers():
    assert common.version_tuple("2.1.289 (Claude Code)") == (2, 1, 289)
    assert common.at_least("2.1.289", "2.1.289") and common.at_least("2.1.300", "2.1.289")
    assert not common.at_least("2.1.250", "2.1.289") and not common.at_least("garbage", "1.0")
    assert common.at_least("0.160", "0.153.4") and common.at_least("1.18.34-beta.1", "1.18.34")


@pytest.mark.parametrize(
    ("adapter_cls", "old", "new", "package"),
    [
        (
            ClaudeCodeAdapter,
            ("2.1.200 (Claude Code)", "2.1.200"),
            ("2.1.300 (Claude Code)", "2.1.300"),
            "claude-agent-sdk",
        ),
        (CodexAdapter, ("codex-cli 0.150.0", "0.150.0"), ("codex-cli 0.160.1", "0.160.1"), "openai-codex"),
        (OpencodeAdapter, ("1.17.9", "1.17.9"), ("1.18.34", "1.18.34"), "aiohttp"),
    ],
)
def test_a_runtime_missing_too_old_or_without_its_sdk_is_unsupported_with_the_reason(
    tmp_path, monkeypatch, adapter_cls, old, new, package
):
    binary = adapter_cls.binary
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    (tmp_path / "bin").mkdir()
    missing = adapter_cls.detect()
    assert missing == Detection(False, None, f"{binary} is not on PATH")

    _version_binary(tmp_path / "bin", binary, old[0])
    too_old = adapter_cls.detect()
    assert too_old.available is False and too_old.version == old[1]
    assert "is older than" in too_old.reason and "the oldest version this worker was checked with" in too_old.reason

    _version_binary(tmp_path / "bin", binary, new[0])
    real = common.package_version
    monkeypatch.setattr(common, "package_version", lambda name: "0.0.1" if name == package else real(name))
    old_sdk = adapter_cls.detect()
    assert old_sdk.available is False and f"{package} 0.0.1 is older than" in old_sdk.reason
    monkeypatch.setattr(common, "package_version", lambda name: None if name == package else real(name))
    no_sdk = adapter_cls.detect()
    assert (
        no_sdk.available is False
        and no_sdk.reason == f"{package} is not installed: install the worker extra, evo-ak[worker]"
    )
    monkeypatch.setattr(common, "package_version", lambda name: "999.0")
    assert adapter_cls.detect() == Detection(True, new[1], None)


def test_the_package_adapts_all_three_runtimes():
    loaded = adapter_module.load_adapters({})
    assert {runtime: cls.__name__ for runtime, cls in loaded.items()} == {
        "claude-code": "ClaudeCodeAdapter",
        "opencode": "OpencodeAdapter",
        "codex": "CodexAdapter",
    }
    assert all(loaded[runtime].runtime == runtime for runtime in loaded)


def test_the_launcher_runs_the_runtime_in_a_session_of_its_own_and_writes_its_pid(tmp_path):
    program = _script(
        tmp_path / "program",
        "import json, os, sys\nprint(json.dumps({'pid': os.getpid(), 'sid': os.getsid(0), 'args': sys.argv[1:]}))\n",
    )
    launcher = common.write_launcher(tmp_path, str(program))
    assert os.access(launcher, os.X_OK)
    done = subprocess.run([str(launcher), "--flag", "a b"], capture_output=True, text=True, check=True)
    seen = json.loads(done.stdout)
    assert seen["sid"] == seen["pid"] != os.getsid(0), "the runtime leads a session, so a Ctrl-C here misses it"
    assert seen["args"] == ["--flag", "a b"]
    assert common.read_pid(tmp_path / "pid") == seen["pid"]


def test_killing_the_group_never_signals_the_daemons_own(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append(pid))
    common.kill_group(os.getpgrp())
    common.kill_group(None)
    common.kill_group(1)
    assert sent == []


def test_the_model_and_effort_come_from_the_run_then_the_environment(tmp_path):
    context = _context(tmp_path, "codex", env={"EVO_WORKER_CODEX_MODEL": "gpt-x", "EVO_WORKER_CODEX_EFFORT": "low"})
    assert common.run_setting(context, "codex", "model") == "gpt-x"
    assert common.run_setting(context, "codex", "effort") == "low"
    context = _context(tmp_path, "codex", model="gpt-y", env={"EVO_WORKER_CODEX_MODEL": "gpt-x"})
    assert common.run_setting(context, "codex", "model") == "gpt-y"
    assert common.run_setting(_context(tmp_path, "opencode"), "opencode", "model") is None


# Claude Code


SESSION = "a09b829c-1d84-4959-8101-826c49e52197"
# What the research cut from its samples and the SDK reads, as Claude Code 2.1.289 sends it.
_PUT_BACK = {
    "assistant": lambda raw: raw["message"].setdefault("model", "claude-opus-5-5"),
    "task_started": lambda raw: raw.update(description="sleep 8 && echo slept", uuid="u-1"),
    "task_notification": lambda raw: raw.update(output_file="", summary="slept", uuid="u-2"),
    "rate_limit_event": lambda raw: raw.update(uuid="u-3"),
    "result": lambda raw: raw.update(duration_api_ms=15000),
}


def claude_samples() -> list[dict]:
    samples = []
    for raw in copy.deepcopy(fake_adapter.SAMPLES):
        raw["session_id"] = SESSION
        for key in (raw["type"], raw.get("subtype")):
            if key in _PUT_BACK:
                _PUT_BACK[key](raw)
        samples.append(raw)
    return samples


def _result(text: str, *, subtype: str = "success", is_error: bool = False) -> dict:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "num_turns": 1,
        "result": text,
        "stop_reason": "end_turn",
        "session_id": SESSION,
        "duration_ms": 10,
        "duration_api_ms": 9,
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }


def _assistant(*blocks: dict) -> dict:
    message = {"id": "msg_1", "role": "assistant", "model": "claude-opus-5-5", "content": list(blocks)}
    return {"type": "assistant", "message": message, "parent_tool_use_id": None, "session_id": SESSION}


def _tool_use(tool_id: str, command: str) -> dict:
    return _assistant({"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}})


def _tool_result(tool_id: str, text: str) -> dict:
    content = [{"tool_use_id": tool_id, "type": "tool_result", "content": text, "is_error": False}]
    return {"type": "user", "message": {"role": "user", "content": content}, "session_id": SESSION}


INIT = {"type": "system", "subtype": "init", "session_id": SESSION, "permissionMode": "bypassPermissions"}


class FakeCLI:
    """Claude Code's side of stream-json, scripted by ``behaviour(cli)``, as a transport of claude-agent-sdk
    (``claude_agent_sdk.Transport``): it answers the SDK's control requests, and logs, in order, the user messages it
    reads, the results it prints and the end of its stdin."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.log: list[tuple] = []
        self.users: asyncio.Queue = asyncio.Queue()
        self.out: asyncio.Queue = asyncio.Queue()
        self.input_closed = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.busy = asyncio.Event()
        self.task = None

    async def connect(self) -> None:
        self.task = asyncio.create_task(self.behaviour(self))

    def is_ready(self) -> bool:
        return True

    async def write(self, data: str) -> None:
        message = json.loads(data)
        if message["type"] == "control_request":
            subtype = message["request"]["subtype"]
            self.log.append(("control", subtype))
            response = {"subtype": "success", "request_id": message["request_id"], "response": {}}
            self.print({"type": "control_response", "response": response})
            if subtype == "interrupt":
                self.interrupted.set()
        elif message["type"] == "user":
            text = message["message"]["content"]
            self.log.append(("user", text))
            self.users.put_nowait(text)

    def print(self, message: dict) -> None:
        if message.get("type") == "result":
            self.log.append(("result", message["result"]))
        self.out.put_nowait(message)

    def exit(self) -> None:
        self.out.put_nowait(None)

    async def end_input(self) -> None:
        self.log.append(("end_input",))
        self.input_closed.set()

    async def read_messages(self):
        while (message := await self.out.get()) is not None:
            yield message

    async def close(self) -> None:
        self.exit()
        if self.task is not None:
            self.task.cancel()


class UnderTest(ClaudeCodeAdapter):
    behaviour = None

    def make_client(self, options):
        self.cli = FakeCLI(type(self).behaviour)
        return claude_agent_sdk.ClaudeSDKClient(options, transport=self.cli)


def _claude(tmp_path, behaviour, **kwargs) -> UnderTest:
    needs("claude_agent_sdk")
    cls = type("Claude", (UnderTest,), {"behaviour": staticmethod(behaviour)})
    return cls(_context(tmp_path, "claude-code", **kwargs))


async def _samples(cli: FakeCLI) -> None:
    await cli.users.get()
    for message in claude_samples():
        cli.print(message)
    await cli.input_closed.wait()  # Claude Code exits once its stdin ends after the result
    cli.exit()


def test_claude_code_runs_with_full_permissions_in_the_worktree_under_the_daemons_session_id(tmp_path):
    adapter = _claude(tmp_path, _samples)
    session = adapter.session_id
    assert len(session) == 36, "the daemon makes the session id, so it is known before the agent starts"
    events, outcome = asyncio.run(_collect(adapter))
    options = adapter.options
    assert options.permission_mode == "bypassPermissions"
    assert options.session_id == session and options.resume is None
    assert options.cwd == str(tmp_path / "worktree")
    assert options.system_prompt == {"type": "preset", "preset": "claude_code", "append": common.HEADLESS_NOTE}
    assert "CLAUDECODE" not in options.env, "the agent is no child of a Claude Code session the daemon runs in"
    # The flags the SDK gives the CLI with these options (its own transport builds them; the fake took its place).
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    command = SubprocessCLITransport(prompt="", options=options)._build_command()
    assert command[0] == options.cli_path and "--permission-mode" in command
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"
    assert f"--session-id={session}" in command and "--append-system-prompt" in command
    assert not Path(options.cli_path).exists(), "the launcher is removed with the run"
    assert adapter.cli.log == [("control", "initialize"), ("user", PROMPT), ("result", "SECOND"), ("end_input",)]


def test_claude_codes_sample_events_come_out_in_the_hubs_kinds(tmp_path):
    events, outcome = asyncio.run(_collect(_claude(tmp_path, _samples)))
    assert _kinds(events) == [
        "output",  # system/init
        "tool_call",
        "output",  # task_started
        "output",  # task_notification
        "tool_call_update",
        "agent_message_chunk",
        "output",  # rate_limit_event
        "usage_update",
    ]
    tool = events[1].body
    assert tool == {
        "toolCallId": "toolu_01P69eEarUZUJB9qpZopfcYy",
        "title": "Bash",
        "kind": "execute",
        "status": "pending",
        "rawInput": {"command": "sleep 8 && echo slept", "description": "..."},
    }
    assert events[4].body["status"] == "completed" and events[4].body["toolCallId"] == tool["toolCallId"]
    assert events[4].body["content"] == [{"type": "content", "content": {"type": "text", "text": "slept"}}]
    assert events[5].body == {"content": {"type": "text", "text": "SECOND"}}
    assert events[0].body["raw"]["subtype"] == "init" and events[6].body["raw"]["type"] == "rate_limit_event"
    usage = events[7].body
    assert usage["usage"]["output_tokens"] == 405 and usage["cost"] == {"amount": 0.2041276, "currency": "USD"}
    assert outcome.completed and outcome.error is None and outcome.summary == "SECOND"
    assert outcome.usage == {
        "input_tokens": 4,
        "cache_creation_input_tokens": 23003,
        "cache_read_input_tokens": 59938,
        "output_tokens": 405,
        "total_cost_usd": 0.2041276,
    }


def test_a_message_during_a_claude_turn_goes_in_before_the_result_and_none_after_it(tmp_path):
    async def behaviour(cli: FakeCLI) -> None:
        await cli.users.get()
        cli.print(INIT)
        cli.print(_tool_use("toolu_1", "sleep 8 && echo slept"))
        cli.busy.set()
        await cli.users.get()  # the owner's message, taken at the next tool boundary
        cli.print(_tool_result("toolu_1", "slept"))
        cli.print(_assistant({"type": "text", "text": "SECOND"}))
        cli.print(_result("SECOND"))
        await cli.input_closed.wait()
        cli.exit()

    late: list[Exception] = []

    async def during(adapter, events) -> None:
        await _until(adapter.cli.busy.is_set)
        assert ("end_input",) not in adapter.cli.log, "the input stays open during the turn"
        await adapter.send(MESSAGE)
        await _until(lambda: ("end_input",) in adapter.cli.log)
        with pytest.raises(common.AgentFinished) as refused:
            await adapter.send("too late")
        late.append(refused.value)

    adapter = _claude(tmp_path, behaviour)
    events, outcome = asyncio.run(_collect(adapter, during))
    assert adapter.cli.log == [
        ("control", "initialize"),
        ("user", PROMPT),
        ("user", MESSAGE),
        ("result", "SECOND"),
        ("end_input",),
    ]
    assert late and outcome.completed and outcome.summary == "SECOND"


def test_interrupting_claude_sends_the_sdks_interrupt_then_ends_its_input(tmp_path):
    async def behaviour(cli: FakeCLI) -> None:
        await cli.users.get()
        cli.print(INIT)
        cli.print(_tool_use("toolu_1", "sleep 600"))
        cli.busy.set()
        await cli.interrupted.wait()
        await cli.input_closed.wait()
        cli.exit()  # no result: an interrupted Claude Code ends without one, and exits 0

    async def during(adapter, events) -> None:
        await _until(adapter.cli.busy.is_set)
        await adapter.interrupt()

    adapter = _claude(tmp_path, behaviour)
    events, outcome = asyncio.run(_collect(adapter, during))
    assert adapter.cli.log[-2:] == [("control", "interrupt"), ("end_input",)]
    assert outcome == Outcome(False, "interrupted", None, None)


def test_stopping_claude_at_the_turn_boundary_ends_its_input_and_lets_the_turn_finish(tmp_path):
    async def behaviour(cli: FakeCLI) -> None:
        await cli.users.get()
        cli.print(INIT)
        cli.print(_tool_use("toolu_1", "sleep 6 && echo hi > closemid.txt"))
        cli.busy.set()
        await cli.input_closed.wait()  # stdin ends mid-turn; the turn goes on to its result
        cli.print(_tool_result("toolu_1", ""))
        cli.print(_assistant({"type": "text", "text": "DONE"}))
        cli.print(_result("DONE"))
        cli.exit()

    async def during(adapter, events) -> None:
        await _until(adapter.cli.busy.is_set)
        await adapter.stop_at_turn_boundary()

    adapter = _claude(tmp_path, behaviour)
    events, outcome = asyncio.run(_collect(adapter, during))
    assert adapter.cli.log[-2:] == [("end_input",), ("result", "DONE")]
    assert outcome.completed and outcome.summary == "DONE"


def test_claude_ending_without_a_result_or_with_an_error_result_is_not_a_completed_turn(tmp_path):
    async def silent(cli: FakeCLI) -> None:
        await cli.users.get()
        cli.print(INIT)
        cli.exit()

    events, outcome = asyncio.run(_collect(_claude(tmp_path, silent)))
    assert not outcome.completed and "ended without finishing its turn" in outcome.error

    async def failing(cli: FakeCLI) -> None:
        await cli.users.get()
        cli.print(_result("Prompt is too long", subtype="error_during_execution", is_error=True))
        await cli.input_closed.wait()
        cli.exit()

    events, outcome = asyncio.run(_collect(_claude(tmp_path, failing)))
    assert not outcome.completed
    assert outcome.error == "claude-code ended its turn with error_during_execution: Prompt is too long"


def test_claude_resumes_the_runs_session_instead_of_making_one(tmp_path):
    adapter = _claude(tmp_path, _samples, resume=SESSION)
    asyncio.run(_collect(adapter))
    assert adapter.session_id == SESSION
    assert adapter.options.resume == SESSION and adapter.options.session_id is None


def test_claude_thinking_and_its_todo_list_come_out_as_thoughts_and_a_plan():
    sdk = needs("claude_agent_sdk")
    message = sdk.AssistantMessage(
        content=[
            sdk.ThinkingBlock(thinking="", signature="kept by the API"),
            sdk.ThinkingBlock(thinking="Check the tests first.", signature="s"),
            sdk.ToolUseBlock(
                id="toolu_9",
                name="TodoWrite",
                input={"todos": [{"content": "Write the adapter", "status": "in_progress", "activeForm": "Writing"}]},
            ),
        ],
        model="claude-opus-5-5",
    )
    events = events_of(message)
    assert _kinds(events) == ["agent_thought_chunk", "tool_call", "plan"]
    assert events[2].body == {
        "entries": [{"content": "Write the adapter", "status": "in_progress", "priority": "medium"}]
    }


# Codex


THREAD = "01a10b1f-ad53-7e41-8a28-30e04b1e06fe"
WORKDIR = "/tmp/worktree"


def notification(method: str, params: dict):
    """A notification as openai-codex hands it over: typed by the app-server's schema."""
    from openai_codex.generated.notification_registry import NOTIFICATION_MODELS
    from openai_codex.models import Notification, UnknownNotification

    model = NOTIFICATION_MODELS.get(method)
    return Notification(method, model.model_validate(params) if model else UnknownNotification(params))


def codex_turn_samples(turn_id: str) -> list[tuple[str, dict]]:
    """The research's `codex exec --json` samples (the printf command, the agent's message, the usage of
    turn.completed), in the notifications `codex app-server` sends for the same turn."""
    ids = {"threadId": THREAD, "turnId": turn_id}
    command = {
        "type": "commandExecution",
        "id": "exec-1",
        "command": "/bin/zsh -lc \"printf 'hi' > codex.txt\"",
        "commandActions": [{"command": "printf 'hi' > codex.txt", "type": "unknown"}],
        "cwd": WORKDIR,
        "status": "inProgress",
    }
    done = {**command, "status": "completed", "exitCode": 0, "durationMs": 4, "aggregatedOutput": ""}
    message = {
        "type": "agentMessage",
        "id": "msg_1",
        "phase": "final_answer",
        "text": "Đã tạo `codex.txt` chứa đúng `hi`.",
    }
    total = {
        "inputTokens": 63195,
        "cachedInputTokens": 31360,
        "cacheWriteInputTokens": 0,
        "outputTokens": 50,
        "reasoningOutputTokens": 0,
        "totalTokens": 63245,
    }
    hook = {
        "run": {
            "id": "stop:3:/home/owner/.codex/hooks.json",
            "displayOrder": 3,
            "entries": [],
            "eventName": "stop",
            "executionMode": "sync",
            "handlerType": "command",
            "scope": "turn",
            "source": "user",
            "sourcePath": "/home/owner/.codex/hooks.json",
            "startedAt": 1791208175,
            "completedAt": 1791208176,
            "durationMs": 99,
            "status": "completed",
        },
        **ids,
    }
    return [
        ("turn/started", {"threadId": THREAD, "turn": {"id": turn_id, "items": [], "status": "inProgress"}}),
        ("item/started", {"item": command, "startedAtMs": 1, **ids}),
        ("item/completed", {"item": done, "completedAtMs": 2, **ids}),
        ("thread/tokenUsage/updated", {"tokenUsage": {"last": total, "total": total}, **ids}),
        ("item/started", {"item": {**message, "text": ""}, "startedAtMs": 3, **ids}),
        ("item/agentMessage/delta", {"delta": message["text"], "itemId": "msg_1", **ids}),
        ("item/completed", {"item": message, "completedAtMs": 4, **ids}),
        ("hook/started", {"run": {**hook["run"], "status": "running"}, **ids}),
        ("hook/completed", hook),
    ]


def turn_completed(turn_id: str, status: str = "completed", error: str | None = None) -> tuple[str, dict]:
    turn = {"id": turn_id, "items": [], "status": status, **({"error": {"message": error}} if error else {})}
    return ("turn/completed", {"threadId": THREAD, "turn": turn})


class FakeTurn:
    def __init__(self, codex: FakeCodex, turn_id: str):
        self.codex, self.id = codex, turn_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.refuse_steer = False
        self.steer_tried = asyncio.Event()
        self.done = False

    def send(self, method: str, params: dict) -> None:
        self.queue.put_nowait(notification(method, params))

    async def stream(self):
        while True:
            item = await self.queue.get()
            yield item
            if item.method == "turn/completed":
                self.done = True
                return

    async def steer(self, text: str):
        self.steer_tried.set()
        if self.refuse_steer or self.done:
            from openai_codex import InvalidRequestError

            self.codex.log.append(("steer refused", self.id))
            raise InvalidRequestError(-32600, f"expected active turn `{self.id}` but found none")
        self.codex.log.append(("steer", self.id, text))

    async def interrupt(self):
        self.codex.log.append(("interrupt", self.id))
        self.send(*turn_completed(self.id, "interrupted"))


class FakeThread:
    def __init__(self, codex: FakeCodex, thread_id: str):
        self.codex, self.id = codex, thread_id

    async def turn(self, text: str, **options) -> FakeTurn:
        turn = FakeTurn(self.codex, f"turn-{len(self.codex.turns) + 1}")
        self.codex.turns.append(turn)
        self.codex.log.append(("turn", self.id, turn.id, text))
        self.codex.tasks.append(asyncio.create_task(self.codex.behaviour(self.codex, turn)))
        return turn


class FakeCodex:
    """An AsyncCodex of the test's own: what the adapter asks of it is logged, its turns follow ``behaviour``."""

    def __init__(self, config, behaviour):
        self.config = config
        self.behaviour = behaviour
        self.log: list[tuple] = []
        self.turns: list[FakeTurn] = []
        self.tasks: list[asyncio.Task] = []
        self.closed = False

    async def __aenter__(self):
        from types import SimpleNamespace

        self.metadata = SimpleNamespace(serverInfo=SimpleNamespace(version="0.153.4 (Mac OS 26.5.2; arm64)"))
        return self

    async def thread_start(self, **options) -> FakeThread:
        self.log.append(("thread/start", options))
        return FakeThread(self, THREAD)

    async def thread_resume(self, thread_id: str, **options) -> FakeThread:
        self.log.append(("thread/resume", thread_id, options))
        return FakeThread(self, thread_id)

    async def close(self) -> None:
        self.closed = True


def _codex(tmp_path, behaviour, **kwargs) -> CodexAdapter:
    needs("openai_codex")

    class Under(CodexAdapter):
        def make_codex(self, config):
            self.fake = FakeCodex(config, behaviour)
            return self.fake

    return Under(_context(tmp_path, "codex", **kwargs))


async def _codex_samples(codex: FakeCodex, turn: FakeTurn) -> None:
    for method, params in codex_turn_samples(turn.id):
        turn.send(method, params)
    turn.send(*turn_completed(turn.id))


def test_codex_starts_a_thread_with_full_access_in_the_worktree_through_its_app_server(tmp_path):
    adapter = _codex(tmp_path, _codex_samples, effort="low")
    events, outcome = asyncio.run(_collect(adapter))
    from openai_codex import ApprovalMode, Sandbox

    worktree = str(tmp_path / "worktree")
    assert adapter.session_id == THREAD and adapter.server_version == "0.153.4"
    start = adapter.fake.log[0]
    assert start[0] == "thread/start"
    assert start[1] == {
        "approval_mode": ApprovalMode.deny_all,
        "sandbox": Sandbox.full_access,
        "cwd": worktree,
        "developer_instructions": common.HEADLESS_NOTE,
        "model": None,
        "config": {"model_reasoning_effort": "low"},
    }
    # On the wire these are what --dangerously-bypass-approvals-and-sandbox sets: no approval asked, no sandbox.
    from openai_codex import ApprovalMode, Sandbox
    from openai_codex._approval_mode import _approval_mode_settings
    from openai_codex._sandbox import _sandbox_mode

    assert _approval_mode_settings(ApprovalMode.deny_all)[0].root.value == "never"
    assert _sandbox_mode(Sandbox.full_access).value == "danger-full-access"
    config = adapter.fake.config
    codex_bin = str(tmp_path / "bin" / "codex")
    assert config.cwd == worktree and config.codex_bin == codex_bin
    assert config.launch_args_override[:3] == (sys.executable, "-I", "-c")
    assert config.launch_args_override[-4:] == (codex_bin, "app-server", "--listen", "stdio://")
    assert adapter.fake.log[1] == ("turn", THREAD, "turn-1", PROMPT)
    assert adapter.fake.closed


def test_codexs_sample_events_come_out_in_the_hubs_kinds(tmp_path):
    events, outcome = asyncio.run(_collect(_codex(tmp_path, _codex_samples)))
    assert _kinds(events) == ["tool_call", "tool_call_update", "usage_update", "agent_message_chunk", "output"]
    assert events[0].body == {
        "toolCallId": "exec-1",
        "title": "/bin/zsh -lc \"printf 'hi' > codex.txt\"",
        "kind": "execute",
        "status": "in_progress",
        "rawInput": {"command": "/bin/zsh -lc \"printf 'hi' > codex.txt\"", "cwd": WORKDIR},
    }
    assert events[1].body == {
        "toolCallId": "exec-1",
        "status": "completed",
        "rawOutput": {"exitCode": 0, "durationMs": 4},
    }
    assert events[2].body["usage"]["inputTokens"] == 63195 and events[2].body["last"]["outputTokens"] == 50
    assert events[3].body == {"content": {"type": "text", "text": "Đã tạo `codex.txt` chứa đúng `hi`."}}
    assert events[4].body["raw"]["method"] == "hook/completed", "a hook's start is left out, its end kept raw"
    assert outcome.completed and outcome.summary == "Đã tạo `codex.txt` chứa đúng `hi`."
    assert outcome.usage == {
        "input_tokens": 63195,
        "cached_input_tokens": 31360,
        "cache_write_input_tokens": 0,
        "output_tokens": 50,
        "reasoning_output_tokens": 0,
        "total_tokens": 63245,
    }


def test_a_message_during_a_codex_turn_steers_it(tmp_path):
    release = asyncio.Event()

    async def behaviour(codex, turn) -> None:
        samples = codex_turn_samples(turn.id)
        turn.send(*samples[1])  # the command starts
        await release.wait()
        for method, params in samples[2:]:
            turn.send(method, params)
        turn.send(*turn_completed(turn.id))

    async def during(adapter, events) -> None:
        await _until(lambda: any(event.kind == "tool_call" for event in events))
        await adapter.send(MESSAGE)
        release.set()
        await _until(lambda: adapter.finished)
        with pytest.raises(common.AgentFinished):
            await adapter.send("too late")

    adapter = _codex(tmp_path, behaviour)
    events, outcome = asyncio.run(_collect(adapter, during))
    assert [entry for entry in adapter.fake.log if entry[0] != "thread/start"] == [
        ("turn", THREAD, "turn-1", PROMPT),
        ("steer", "turn-1", MESSAGE),
    ]
    assert outcome.completed


def test_a_message_codexs_turn_no_longer_takes_starts_the_next_turn_on_the_same_thread(tmp_path):
    async def behaviour(codex, turn) -> None:
        if turn.id == "turn-1":
            turn.refuse_steer = True  # the turn has ended on the app-server's side
            turn.send(*codex_turn_samples(turn.id)[1])
            await turn.steer_tried.wait()
            turn.send(*turn_completed(turn.id))
        else:
            turn.send("item/completed", {**codex_turn_samples(turn.id)[6][1]})
            turn.send(*turn_completed(turn.id))

    async def during(adapter, events) -> None:
        await _until(lambda: any(event.kind == "tool_call" for event in events))
        await adapter.send(MESSAGE)

    adapter = _codex(tmp_path, behaviour)
    events, outcome = asyncio.run(_collect(adapter, during))
    assert [entry for entry in adapter.fake.log if entry[0] != "thread/start"] == [
        ("turn", THREAD, "turn-1", PROMPT),
        ("steer refused", "turn-1"),
        ("turn", THREAD, "turn-2", MESSAGE),
    ]
    assert outcome.completed and _kinds(events)[-1] == "agent_message_chunk"


def test_interrupting_codex_interrupts_its_turn(tmp_path):
    async def behaviour(codex, turn) -> None:
        turn.send(*codex_turn_samples(turn.id)[1])

    async def during(adapter, events) -> None:
        await _until(lambda: bool(events))
        await adapter.interrupt()

    adapter = _codex(tmp_path, behaviour)
    events, outcome = asyncio.run(_collect(adapter, during))
    assert ("interrupt", "turn-1") in adapter.fake.log
    assert not outcome.completed and outcome.error == "interrupted"
    assert adapter.fake.closed


def test_a_failed_codex_turn_fails_with_its_error_and_a_resumed_thread_is_the_runs(tmp_path):
    async def behaviour(codex, turn) -> None:
        turn.send(*turn_completed(turn.id, "failed", "stream disconnected before completion"))

    adapter = _codex(tmp_path, behaviour, resume=THREAD)
    events, outcome = asyncio.run(_collect(adapter))
    assert adapter.fake.log[0][:2] == ("thread/resume", THREAD)
    assert outcome == Outcome(False, "codex ended its turn failed: stream disconnected before completion", None, None)


# opencode


OC_SESSION = "ses_ef4e76fa8ffefPeJ5f5yIAd9Mo"
PASSWORD = "not-the-real-one"
MODEL = "zai-coding-plan/glm-5.3-flash"


def part_event(part: dict) -> dict:
    return {"type": "message.part.updated", "properties": {"sessionID": OC_SESSION, "part": part, "time": 1}}


def oc_status(kind: str, session: str = OC_SESSION) -> dict:
    return {"type": "session.status", "properties": {"sessionID": session, "status": {"type": kind}}}


def oc_user(message_id: str) -> dict:
    info = {"id": message_id, "role": "user", "sessionID": OC_SESSION, "time": {"created": 1}}
    return {"type": "message.updated", "properties": {"sessionID": OC_SESSION, "info": info}}


def opencode_samples(user: str = "msg_user_1") -> list[dict]:
    """The parts of the research's `opencode run --format json` samples, as the server's stream sends them."""
    prompt = {"id": "prt_0", "messageID": user, "sessionID": OC_SESSION, "type": "text", "text": PROMPT}
    tool = {
        "id": "prt_1",
        "messageID": "msg_10b189074001",
        "sessionID": OC_SESSION,
        "type": "tool",
        "tool": "write",
        "callID": "call_3684bb9b",
        "state": {
            "status": "completed",
            "input": {"content": "hi", "filePath": "/tmp/worktree/hello.txt"},
            "output": "Wrote file successfully.",
            "title": "hello.txt",
            "time": {"start": 1791187539381, "end": 1791187539391},
        },
    }
    step_tokens = {"total": 90146, "input": 90010, "output": 74, "reasoning": 62, "cache": {"write": 0, "read": 0}}
    last_tokens = {"total": 90167, "input": 171, "output": 12, "reasoning": 0, "cache": {"write": 0, "read": 89984}}
    text = "Created `hello.txt` with exactly `hi`."
    asked = {
        "type": "permission.asked",
        "properties": {
            "id": "per_1",
            "sessionID": OC_SESSION,
            "permission": "edit",
            "patterns": ["hello.txt"],
            "metadata": {},
            "always": ["*"],
        },
    }
    return [
        {"type": "server.heartbeat", "properties": {}},
        oc_user(user),
        part_event(prompt),
        oc_status("busy"),
        part_event({"id": "prt_s", "messageID": "msg_10b189074001", "sessionID": OC_SESSION, "type": "step-start"}),
        asked,
        part_event({**tool, "state": {"status": "pending", "input": {}, "raw": ""}}),
        part_event(tool),
        part_event(
            {
                "id": "prt_2",
                "messageID": "msg_10b189074001",
                "sessionID": OC_SESSION,
                "type": "step-finish",
                "reason": "tool-calls",
                "tokens": step_tokens,
                "cost": 0,
            }
        ),
        part_event({"id": "prt_3", "messageID": "msg_2", "sessionID": OC_SESSION, "type": "text", "text": "Cre"}),
        part_event(
            {
                "id": "prt_3",
                "messageID": "msg_2",
                "sessionID": OC_SESSION,
                "type": "text",
                "text": text,
                "time": {"start": 1, "end": 2},
            }
        ),
        part_event(
            {
                "id": "prt_4",
                "messageID": "msg_2",
                "sessionID": OC_SESSION,
                "type": "step-finish",
                "reason": "stop",
                "tokens": last_tokens,
                "cost": 0,
            }
        ),
        oc_status("idle", "ses_someone_else"),
        oc_status("idle"),
    ]


class FakeOpencode:
    """The routes of `opencode serve` the adapter uses, behind Basic auth; ``behaviour(server, body)`` runs for each
    prompt and puts events on the stream."""

    def __init__(self, behaviour, config_model: str | None = None):
        self.behaviour = behaviour
        self.config_model = config_model
        self.requests: list[tuple] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.prompts: asyncio.Queue = asyncio.Queue()
        self.aborted = asyncio.Event()
        self.unauthorized = 0
        self.runner = None
        self.url = None

    def emit(self, *events: dict) -> None:
        for event in events:
            self.events.put_nowait(event)

    async def auth(self, request, handler):
        expected = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            self.unauthorized += 1
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Secure Area"'})
        return await handler(request)

    async def start(self) -> None:
        @web.middleware
        async def auth(request, handler):
            return await self.auth(request, handler)

        app = web.Application(middlewares=[auth])
        app.router.add_get("/config", self.config)
        app.router.add_get("/config/providers", self.providers)
        app.router.add_post("/session", self.create)
        app.router.add_get("/session/{id}", self.get_session)
        app.router.add_get("/event", self.stream)
        app.router.add_post("/session/{id}/prompt_async", self.prompt)
        app.router.add_post("/session/{id}/abort", self.abort)
        app.router.add_post("/permission/{id}/reply", self.reply)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        host, port = self.runner.addresses[0][:2]
        self.url = f"http://{host}:{port}"

    async def stop(self) -> None:
        self.events.put_nowait(None)
        await self.runner.cleanup()

    async def config(self, request):
        return web.json_response({"model": self.config_model} if self.config_model else {})

    async def providers(self, request):
        models = {"glm-5.3-flash": {}, "glm-5.3-highspeed": {}}
        return web.json_response({"providers": [{"id": "zai-coding-plan", "models": models}], "default": {}})

    async def create(self, request):
        body = await request.json()
        self.requests.append(("create", dict(request.query), body))
        return web.json_response(
            {"id": OC_SESSION, "title": body.get("title"), "directory": request.query["directory"]}
        )

    async def get_session(self, request):
        self.requests.append(("get", request.match_info["id"]))
        return web.json_response({"id": request.match_info["id"]})

    async def stream(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(b'data: {"type":"server.connected","properties":{}}\n\n')
        while (event := await self.events.get()) is not None:
            data = json.dumps(event, ensure_ascii=False).encode()
            await response.write(b"data: " + data + b"\n\n")
        return response

    async def prompt(self, request):
        body = await request.json()
        self.requests.append(("prompt", request.match_info["id"], body))
        self.prompts.put_nowait(body)
        asyncio.get_running_loop().create_task(self.behaviour(self, body))
        return web.Response(status=204)

    async def abort(self, request):
        self.requests.append(("abort", request.match_info["id"]))
        self.aborted.set()
        return web.json_response(True)

    async def reply(self, request):
        self.requests.append(("reply", request.match_info["id"], await request.json()))
        return web.json_response(True)


def _opencode(tmp_path, behaviour, *, config_model: str | None = None, **kwargs) -> OpencodeAdapter:
    needs("aiohttp")
    server = FakeOpencode(behaviour, config_model)

    class Under(OpencodeAdapter):
        async def start_server(self):
            await server.start()
            self.fake = server
            return Server(server.url, PASSWORD)

        async def _close(self):
            await super()._close()
            await server.stop()

    return Under(_context(tmp_path, "opencode", **kwargs))


async def _opencode_samples(server: FakeOpencode, body: dict) -> None:
    server.emit(*opencode_samples())


def test_opencode_gets_a_session_with_the_rules_of_opencode_run_and_the_model_named(tmp_path):
    adapter = _opencode(tmp_path, _opencode_samples, model=MODEL)
    events, outcome = asyncio.run(_collect(adapter))
    requests = adapter.fake.requests
    worktree = str(tmp_path / "worktree")
    assert requests[0] == (
        "create",
        {"directory": worktree},
        {"title": "evo-agents run 41: adapters", "permission": opencode_module.RUN_RULES},
    )
    assert requests[1] == (
        "prompt",
        OC_SESSION,
        {
            "parts": [{"type": "text", "text": PROMPT}],
            "model": {"providerID": "zai-coding-plan", "modelID": "glm-5.3-flash"},
            "system": common.HEADLESS_NOTE,
        },
    )
    # Every permission it asks is granted once, as `opencode run --auto` grants it.
    assert requests[2] == ("reply", "per_1", {"reply": "once"})
    assert adapter.fake.unauthorized == 0 and adapter.session_id == OC_SESSION
    assert outcome.completed


def test_opencodes_sample_events_come_out_in_the_hubs_kinds(tmp_path):
    events, outcome = asyncio.run(_collect(_opencode(tmp_path, _opencode_samples, model=MODEL)))
    assert _kinds(events) == [
        "output",  # the permission asked, and granted
        "tool_call",
        "tool_call_update",
        "usage_update",
        "agent_message_chunk",
        "usage_update",
    ]
    assert events[0].body["raw"]["reply"] == "once"
    assert events[1].body == {
        "toolCallId": "call_3684bb9b",
        "title": "hello.txt",
        "kind": "edit",
        "status": "in_progress",
        "rawInput": {"content": "hi", "filePath": "/tmp/worktree/hello.txt"},
    }
    assert events[2].body["status"] == "completed"
    assert events[2].body["content"][0]["content"]["text"] == "Wrote file successfully."
    assert events[3].body["usage"]["input"] == 90010 and events[3].body["reason"] == "tool-calls"
    assert events[4].body == {"content": {"type": "text", "text": "Created `hello.txt` with exactly `hi`."}}
    assert outcome.completed and outcome.summary == "Created `hello.txt` with exactly `hi`."
    assert outcome.usage == {
        "total": 180313,
        "input": 90181,
        "output": 86,
        "reasoning": 62,
        "cache_write": 0,
        "cache_read": 89984,
        "cost": 0,
    }


def test_a_message_while_opencode_is_busy_is_prompted_and_the_turn_waits_for_it(tmp_path):
    async def behaviour(server: FakeOpencode, body: dict) -> None:
        if body["parts"][0]["text"] == PROMPT:
            server.emit(oc_user("msg_user_1"), oc_status("busy"))
            server.emit(
                part_event(
                    {
                        "id": "prt_1",
                        "messageID": "msg_a",
                        "sessionID": OC_SESSION,
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call_1",
                        "state": {"status": "running", "input": {"command": "sleep 10"}, "time": {"start": 1}},
                    }
                )
            )
        else:
            # The idle of the first prompt's loop comes before the second prompt reached the session.
            server.emit(oc_status("idle"))
            await asyncio.sleep(0.2)
            server.emit(oc_user("msg_user_2"), oc_status("busy"))
            text = {"id": "prt_9", "messageID": "msg_b", "sessionID": OC_SESSION, "type": "text", "text": "SECOND"}
            server.emit(part_event({**text, "time": {"start": 1, "end": 2}}), oc_status("idle"))

    async def during(adapter, events) -> None:
        await _until(lambda: any(event.kind == "tool_call" for event in events))
        await adapter.send(MESSAGE)
        await _until(lambda: adapter.finished)
        with pytest.raises(common.AgentFinished):
            await adapter.send("too late")

    adapter = _opencode(tmp_path, behaviour, model=MODEL)
    events, outcome = asyncio.run(_collect(adapter, during))
    prompts = [request[2]["parts"][0]["text"] for request in adapter.fake.requests if request[0] == "prompt"]
    assert prompts == [PROMPT, MESSAGE]
    assert outcome.completed and outcome.summary == "SECOND"


def test_interrupting_opencode_aborts_its_session(tmp_path):
    async def behaviour(server: FakeOpencode, body: dict) -> None:
        server.emit(oc_user("msg_user_1"), oc_status("busy"))
        server.emit(part_event({"id": "prt_t", "messageID": "msg_a", "sessionID": OC_SESSION, "type": "reasoning"}))
        await server.aborted.wait()
        error = {"name": "MessageAbortedError", "data": {"message": "Aborted"}}
        server.emit({"type": "session.error", "properties": {"sessionID": OC_SESSION, "error": error}})
        server.emit(oc_status("idle"))

    async def during(adapter, events) -> None:
        await _until(lambda: any(request[0] == "prompt" for request in adapter.fake.requests))
        await adapter.interrupt()

    adapter = _opencode(tmp_path, behaviour, model=MODEL)
    events, outcome = asyncio.run(_collect(adapter, during))
    assert ("abort", OC_SESSION) in adapter.fake.requests
    assert not outcome.completed and outcome.error == "interrupted"
    assert _kinds(events) == ["output"] and events[0].body["raw"]["type"] == "session.error"


def test_opencode_takes_the_model_of_the_environment_or_its_config_and_refuses_one_it_does_not_list(tmp_path):
    adapter = _opencode(tmp_path, _opencode_samples, config_model="quatmo/qwen3-coder")
    with pytest.raises(RuntimeError, match="opencode lists no model quatmo/qwen3-coder: set EVO_WORKER_OPENCODE_MODEL"):
        asyncio.run(_collect(adapter))
    assert not any(request[0] in ("create", "prompt") for request in adapter.fake.requests)

    adapter = _opencode(
        tmp_path, _opencode_samples, env={"EVO_WORKER_OPENCODE_MODEL": "zai-coding-plan/glm-5.3-highspeed"}
    )
    events, outcome = asyncio.run(_collect(adapter))
    assert adapter.model == ("zai-coding-plan", "glm-5.3-highspeed") and outcome.completed

    adapter = _opencode(tmp_path, _opencode_samples, config_model=MODEL)
    asyncio.run(_collect(adapter))
    assert adapter.model == ("zai-coding-plan", "glm-5.3-flash")


def test_opencode_serve_starts_on_a_free_port_with_a_password_stdin_dev_null_and_its_own_session(tmp_path):
    needs("aiohttp")
    record = tmp_path / "serve.json"
    child = tmp_path / "child.pid"
    program = _script(
        tmp_path / "opencode",
        f"""import json, os, subprocess, sys, time
port = sys.argv[sys.argv.index("--port") + 1]
null = os.stat("/dev/null")
stdin = os.fstat(0)
sleeper = subprocess.Popen(["sleep", "600"])
open({str(child)!r}, "w").write(str(sleeper.pid))
json.dump({{"args": sys.argv[1:], "cwd": os.getcwd(), "password": os.environ.get("OPENCODE_SERVER_PASSWORD"),
           "stdin_is_null": (stdin.st_dev, stdin.st_ino) == (null.st_dev, null.st_ino),
           "leader": os.getsid(0) == os.getpid()}}, open({str(record)!r}, "w"))
print("\\x1b]777;notify;warp://cli-agent;{{}}\\x07", flush=True)
print(f"opencode server listening on http://127.0.0.1:{{port}}", flush=True)
time.sleep(600)
""",
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    async def go():
        server = await opencode_module.start_server(str(program), str(worktree), dict(os.environ))
        seen = json.loads(record.read_text())
        await server.stop()
        return server, seen

    server, seen = asyncio.run(go())
    port = seen["args"][seen["args"].index("--port") + 1]
    assert seen["args"] == ["serve", "--hostname", "127.0.0.1", "--port", port]
    assert server.url == f"http://127.0.0.1:{port}" and server.username == "opencode"
    assert seen["password"] == server.password and len(server.password) >= 32
    assert seen["stdin_is_null"] and seen["leader"] and seen["cwd"] == str(worktree.resolve())
    assert server.process.returncode is not None
    sleeper = int(child.read_text())
    for _ in range(100):  # the server's process group went with it
        try:
            os.kill(sleeper, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(sleeper, signal.SIGKILL)
        raise AssertionError("a process of the server's group outlived it")


def test_opencode_serve_that_exits_before_listening_fails_the_start_with_its_output(tmp_path):
    program = _script(tmp_path / "opencode", "print('Error: no such flag --hostname')\nraise SystemExit(1)\n")
    with pytest.raises(RuntimeError, match="exited 1 before it listened: Error: no such flag"):
        asyncio.run(opencode_module.start_server(str(program), str(tmp_path), dict(os.environ)))


def test_the_event_stream_reads_long_lines_and_events_cut_across_chunks():
    class Content:
        async def iter_any(self):
            event = json.dumps({"type": "x", "properties": {"text": "y" * 200_000}}).encode()
            data = b": comment\n\ndata: " + event + b"\r\n\r\ndata: not json\n\ndata: " + b'{"type":"z"}' + b"\n\n"
            for start in range(0, len(data), 7000):
                yield data[start : start + 7000]

    async def read():
        return [event async for event in opencode_module.sse_events(Content())]

    events = asyncio.run(read())
    assert [event["type"] for event in events] == ["x", "z"] and len(events[0]["properties"]["text"]) == 200_000


# The selftest command


class SelftestAdapter(Adapter):
    """An agent that does what the selftest asks, for the command's own test."""

    runtime = "claude-code"
    binary = "claude"

    @classmethod
    def detect(cls) -> Detection:
        return Detection(True, "2.1.289", None)

    def __init__(self, context):
        super().__init__(context)
        self.queue: asyncio.Queue = asyncio.Queue()

    @property
    def session_id(self):
        return "selftest-session"

    async def start(self):
        (self.context.worktree / "selftest.txt").write_text("ok\n", encoding="utf-8")
        for event in (common.tool_call("t1", "Write", "edit", "pending"), common.message_chunk("DONE"), None):
            self.queue.put_nowait(event)

    async def events(self):
        while (event := await self.queue.get()) is not None:
            yield event

    async def send(self, text):
        pass

    async def stop_at_turn_boundary(self):
        pass

    async def interrupt(self):
        pass

    async def wait(self):
        return Outcome(True, None, {"output_tokens": 3}, "DONE")


def test_selftest_runs_the_adapter_in_a_scratch_repository_and_prints_the_events(monkeypatch, capsys):
    from evo_agents.cli import main

    monkeypatch.setenv(adapter_module.ADAPTERS_VARIABLE, f"claude-code={__name__}:SelftestAdapter")
    assert main(["worker", "selftest", "--runtime", "claude-code"]) == 0
    out = capsys.readouterr().out
    assert ", effort low" in out and "  tool_call: Write (pending)" in out and "  agent_message_chunk: DONE" in out
    assert "2 events (agent_message_chunk 1, tool_call 1), session selftest-session." in out
    assert "Self-test of claude-code passed" in out
    scratch = Path(out.split(" in ", 1)[1].split(",", 1)[0])
    assert not scratch.exists(), "the scratch repository is removed"


def test_selftest_of_an_unavailable_runtime_says_why(monkeypatch, capsys, tmp_path):
    from evo_agents.cli import main

    monkeypatch.setenv("PATH", str(tmp_path))
    assert main(["worker", "selftest", "--runtime", "opencode"]) == 1
    assert "error: opencode is unavailable here: opencode is not on PATH" in capsys.readouterr().err
