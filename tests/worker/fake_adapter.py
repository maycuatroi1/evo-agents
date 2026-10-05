"""An adapter for the daemon's tests, which the daemon loads through EVO_WORKER_ADAPTERS in place of the claude-code
one. It follows the interface of ``evo_agents.worker.adapter`` and runs no agent: a scenario does what an agent
would, step by step, and the events it sends are the ones the runtimes were seen to print (the samples of
research_notes/worker-runtimes.md in the harness, Claude Code's stream-json), translated to the hub's event kinds.

EVO_FAKE_SCENARIOS names a JSON file ``{"<step key>": [action, ...]}``; a run follows the actions of its step:

- ``{"samples": true}``: send the sample events.
- ``{"chunks": [first, last]}``: send agent_message_chunk events with the texts ``chunk <n>``, n from first to last.
- ``{"write": {"path": "text"}}``: write files in the worktree.
- ``{"commit": "message"}``: commit every change, as an agent that commits its work.
- ``{"git": [args]}``: run any git command in the worktree, such as ``["checkout", "--detach"]``.
- ``{"touch": "/abs/path"}``: create a file outside the worktree, for the test to see how far the run got.
- ``{"wait_for": "/abs/path"}``: wait until that file exists (an interrupt ends the wait).
- ``{"sleep": seconds}``.
- ``{"result": {...}}``: write .evo-run/result.json.
- ``{"fail": "why"}``: end the turn as failed.

The messages ``send`` hands the agent are written, one per line, to the file EVO_FAKE_MESSAGES names.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from pathlib import Path

from evo_agents.worker.adapter import Adapter, AgentEvent, Detection, Outcome

VERSION = "2.1.289"

# Claude Code's stream-json, as research_notes/worker-runtimes.md shows it (cut short there, as here).
SAMPLES = [
    {
        "type": "system",
        "subtype": "init",
        "cwd": "/private/tmp/claude-501/...",
        "session_id": "a09b829c-1d84-4959-8101-826c49e52197",
        "model": "claude-opus-5-5",
        "permissionMode": "bypassPermissions",
        "claude_code_version": "2.1.289",
        "tools": ["..."],
    },
    {
        "type": "assistant",
        "message": {
            "id": "msg_011CfijSPpZfcGJqChCsSKr2",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01P69eEarUZUJB9qpZopfcYy",
                    "name": "Bash",
                    "input": {"command": "sleep 8 && echo slept", "description": "..."},
                }
            ],
            "stop_reason": None,
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 16832,
                "cache_read_input_tokens": 21553,
                "output_tokens": 6,
            },
        },
        "parent_tool_use_id": None,
        "session_id": "a09b829c-...",
    },
    {
        "type": "system",
        "subtype": "task_started",
        "task_id": "bnpfn9v0a",
        "tool_use_id": "toolu_01P69eEarUZUJB9qpZopfcYy",
        "task_type": "local_bash",
        "is_backgrounded": False,
        "session_id": "a09b829c-...",
    },
    {
        "type": "system",
        "subtype": "task_notification",
        "task_id": "bnpfn9v0a",
        "tool_use_id": "toolu_01P69eEarUZUJB9qpZopfcYy",
        "status": "completed",
        "session_id": "a09b829c-...",
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": "toolu_01P69eEarUZUJB9qpZopfcYy",
                    "type": "tool_result",
                    "content": "slept",
                    "is_error": False,
                }
            ],
        },
        "parent_tool_use_id": None,
        "session_id": "a09b829c-...",
    },
    {
        "type": "assistant",
        "message": {
            "id": "msg_011CfijTKZ7YpEdGdYexbQbq",
            "role": "assistant",
            "content": [{"type": "text", "text": "SECOND"}],
            "stop_reason": None,
            "usage": {"...": "..."},
        },
        "session_id": "a09b829c-...",
    },
    {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": "allowed",
            "rateLimitType": "five_hour",
            "resetsAt": 1791199800,
            "unifiedWindows": {"five_hour": {"utilization": 0.03}, "seven_day": {"utilization": 0.22}},
        },
        "session_id": "...",
    },
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "result": "SECOND",
        "stop_reason": "end_turn",
        "terminal_reason": "completed",
        "session_id": "a09b829c-...",
        "duration_ms": 16966,
        "total_cost_usd": 0.2041276,
        "usage": {
            "input_tokens": 4,
            "cache_creation_input_tokens": 23003,
            "cache_read_input_tokens": 59938,
            "output_tokens": 405,
        },
    },
]


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def translate(raw: dict) -> list[AgentEvent]:
    """The hub's events for one line of stream-json, the way a claude-code adapter maps them."""
    kind = raw.get("type")
    if kind == "assistant":
        events = []
        for block in raw["message"]["content"]:
            if block.get("type") == "text":
                events.append(AgentEvent("agent_message_chunk", {"content": text_block(block["text"])}))
            elif block.get("type") == "thinking":
                events.append(AgentEvent("agent_thought_chunk", {"content": text_block(block["thinking"])}))
            elif block.get("type") == "tool_use":
                body = {
                    "toolCallId": block["id"],
                    "title": block["name"],
                    "kind": "execute",
                    "status": "pending",
                    "rawInput": block["input"],
                }
                events.append(AgentEvent("tool_call", body))
        return events
    if kind == "user":
        events = []
        for block in raw["message"]["content"]:
            if block.get("type") == "tool_result":
                content = [{"type": "content", "content": {"type": "text", "text": str(block.get("content"))}}]
                status = "failed" if block.get("is_error") else "completed"
                body = {"toolCallId": block["tool_use_id"], "status": status, "content": content}
                events.append(AgentEvent("tool_call_update", body))
        return events
    if kind == "result":
        return [AgentEvent("usage_update", {"usage": raw["usage"], "cost": {"amount": raw["total_cost_usd"]}})]
    return [AgentEvent("output", {"raw": raw})]


class FakeAdapter(Adapter):
    runtime = "claude-code"
    binary = "claude"

    @classmethod
    def detect(cls) -> Detection:
        return Detection(True, VERSION, None)

    def __init__(self, context):
        super().__init__(context)
        self._session = str(uuid.uuid4())
        self._queue: asyncio.Queue = asyncio.Queue()
        self._interrupted = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._outcome = Outcome(False, "the fake agent did not run")
        path = context.env.get("EVO_FAKE_SCENARIOS")
        scenarios = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        self.actions = scenarios.get(str(context.run.get("step_key")), [])
        self.messages_path = context.env.get("EVO_FAKE_MESSAGES")

    @property
    def session_id(self) -> str | None:
        return self._session

    async def start(self) -> None:
        self._task = asyncio.create_task(self._work())

    async def send(self, text: str) -> None:
        if self.messages_path:
            with open(self.messages_path, "a", encoding="utf-8") as handle:
                handle.write(text.replace("\n", " ") + "\n")
        await self._queue.put(AgentEvent("agent_message_chunk", {"content": text_block(f"got: {text}")}))

    async def stop_at_turn_boundary(self) -> None:
        pass

    async def interrupt(self) -> None:
        self._interrupted.set()

    async def events(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def wait(self) -> Outcome:
        if self._task is not None:
            await self._task
        return self._outcome

    async def _pause(self, seconds: float) -> bool:
        """Sleep; whether an interrupt came first."""
        try:
            await asyncio.wait_for(self._interrupted.wait(), seconds)
        except asyncio.TimeoutError:
            return False
        return True

    def _git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.context.worktree), *args], check=True, capture_output=True)

    async def _work(self) -> None:
        worktree = self.context.worktree
        summary = None
        usage = None
        try:
            for action in self.actions:
                if self._interrupted.is_set():
                    self._outcome = Outcome(False, "interrupted")
                    return
                if action.get("samples"):
                    for raw in SAMPLES:
                        for event in translate(raw):
                            await self._queue.put(event)
                        if raw["type"] == "result":
                            summary, usage = raw["result"], {"total_cost_usd": raw["total_cost_usd"], **raw["usage"]}
                elif "chunks" in action:
                    first, last = action["chunks"]
                    for number in range(first, last + 1):
                        chunk = {"content": text_block(f"chunk {number}")}
                        await self._queue.put(AgentEvent("agent_message_chunk", chunk))
                        await asyncio.sleep(0)
                elif "write" in action:
                    for name, text in action["write"].items():
                        path = worktree / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text, encoding="utf-8")
                elif "commit" in action:
                    self._git("add", "--all")
                    self._git("commit", "--quiet", "-m", action["commit"])
                elif "git" in action:
                    self._git(*action["git"])
                elif "touch" in action:
                    Path(action["touch"]).write_text(str(self.context.run_id), encoding="utf-8")
                elif "wait_for" in action:
                    target = Path(action["wait_for"])
                    while not target.exists():
                        if await self._pause(0.05):
                            self._outcome = Outcome(False, "interrupted")
                            return
                elif "sleep" in action:
                    if await self._pause(float(action["sleep"])):
                        self._outcome = Outcome(False, "interrupted")
                        return
                elif "result" in action:
                    path = worktree / ".evo-run" / "result.json"
                    path.parent.mkdir(exist_ok=True)
                    path.write_text(json.dumps(action["result"]), encoding="utf-8")
                elif "fail" in action:
                    self._outcome = Outcome(False, action["fail"])
                    return
            self._outcome = Outcome(True, None, usage, summary)
        except Exception as exc:  # the test sees it as a failed turn
            self._outcome = Outcome(False, f"the fake agent failed: {type(exc).__name__}: {exc}")
        finally:
            await self._queue.put(None)


def environment(scenarios: Path, messages: Path | None = None) -> dict[str, str]:
    """What the daemon's environment needs for this adapter."""
    env = {
        "EVO_WORKER_ADAPTERS": "claude-code=tests.worker.fake_adapter:FakeAdapter",
        "EVO_FAKE_SCENARIOS": str(scenarios),
    }
    if messages is not None:
        env["EVO_FAKE_MESSAGES"] = str(messages)
    return env
