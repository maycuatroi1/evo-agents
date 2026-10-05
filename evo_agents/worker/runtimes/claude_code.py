"""Claude Code through ``claude-agent-sdk``: a ``ClaudeSDKClient`` in streaming mode, never the CLI's stdout parsed
here.

- The session id is the daemon's: a new UUID for a new run (``session_id``), the run's session when it goes on with
  one (``resume``), so the daemon reports it from the start and a takeover resumes it with ``claude --resume``.
- The agent works in the worktree (``cwd``) with ``permission_mode="bypassPermissions"``, the SDK's form of
  ``--dangerously-skip-permissions``, and the preset system prompt of Claude Code with HEADLESS_NOTE appended
  (``--append-system-prompt``). The owner's settings, hooks, plugins and CLAUDE.md load as for ``claude`` itself.
- The input stream the client is connected with stays open while the agent may take input: the prompt and each of
  the owner's messages go in with ``query()``. Claude Code takes a message that arrives mid-turn at the next tool
  boundary, and may fold it into the same ``result`` (research_notes/worker-runtimes.md of the harness).
- A ``result`` ends the turn: the adapter then closes the input stream, and the CLI exits once it has done what it
  was given. ``stop_at_turn_boundary`` closes it at once, so the turn in progress finishes and the CLI exits;
  ``interrupt`` sends the SDK's interrupt, which stops the turn and its tools, then closes it.
- Whether the turn completed comes from the last ``result`` (``subtype`` ``success``, not ``is_error``), never from
  the exit code: Claude Code exits 0 when it is interrupted.
- The CLI is ``claude`` on PATH, started through the launcher of ``common`` (a session of its own).

Checked with Claude Code 2.1.289 and claude-agent-sdk 0.2.163 (``evo-agents worker selftest --runtime claude-code``).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import shutil
import tempfile
import uuid
from pathlib import Path

from evo_agents.worker.adapter import AgentEvent, Detection, Outcome, RunContext
from evo_agents.worker.runtimes.common import (
    HEADLESS_NOTE,
    INTERRUPT_TIMEOUT,
    AgentFinished,
    QueueAdapter,
    add_numbers,
    cut,
    detect_runtime,
    message_chunk,
    plan,
    raw,
    run_setting,
    thought_chunk,
    tool_call,
    tool_update,
    usage_update,
    which,
    write_launcher,
)

MIN_VERSION = "2.1.289"  # Claude Code
SDK = ("claude-agent-sdk", "0.2.163")
# Variables of a parent Claude Code session that must not reach the agent, which is no child of it.
DROPPED_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
# System messages left out of the log: the CLI's list of slash commands (tens of KiB, twice a session), the start of
# a hook (its response says the same and how it ended), and running estimates of thinking tokens.
SKIPPED_SYSTEM = frozenset({"commands_changed", "hook_started", "thinking_tokens"})
TOOL_KINDS = {
    "Read": "read",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Write": "edit",
    "NotebookEdit": "edit",
    "Bash": "execute",
    "BashOutput": "execute",
    "KillShell": "execute",
    "Grep": "search",
    "Glob": "search",
    "WebFetch": "fetch",
    "WebSearch": "fetch",
    "Task": "think",
    "TodoWrite": "think",
}


def _text_of(content) -> str | None:
    """The text of a tool result's content: a string, or a list of content blocks."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item.get("text") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if parts:
            return "\n".join(parts)
    return str(content)


def _raw_of(message) -> dict:
    """A message of the SDK as the CLI wrote it, as far as the SDK keeps it."""
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        return data
    info = getattr(message, "rate_limit_info", None)
    if info is not None:
        return {"type": "rate_limit_event", "rate_limit_info": getattr(info, "raw", None) or {}}
    if dataclasses.is_dataclass(message):
        return {"type": type(message).__name__, **dataclasses.asdict(message)}
    return {"type": type(message).__name__}


def events_of(message) -> list[AgentEvent]:
    """The hub's events for one message of ``ClaudeSDKClient.receive_messages()``."""
    from claude_agent_sdk import types as sdk

    events: list[AgentEvent] = []
    if isinstance(message, sdk.AssistantMessage):
        for block in message.content:
            if isinstance(block, sdk.TextBlock):
                if block.text:
                    events.append(message_chunk(block.text))
            elif isinstance(block, sdk.ThinkingBlock):
                if block.thinking.strip():  # thinking the API keeps to itself comes with an empty text
                    events.append(thought_chunk(block.thinking))
            elif isinstance(block, (sdk.ToolUseBlock, sdk.ServerToolUseBlock)):
                events.append(
                    tool_call(block.id, block.name, TOOL_KINDS.get(block.name, "other"), "pending", block.input)
                )
                todos = (
                    block.input.get("todos") if block.name == "TodoWrite" and isinstance(block.input, dict) else None
                )
                if isinstance(todos, list):
                    entries = [
                        (str(t.get("content") or ""), t.get("status"), "medium") for t in todos if isinstance(t, dict)
                    ]
                    events.append(plan(entries))
            elif isinstance(block, (sdk.ToolResultBlock, sdk.ServerToolResultBlock)):
                status = "failed" if getattr(block, "is_error", False) else "completed"
                events.append(tool_update(block.tool_use_id, status, _text_of(block.content)))
            else:
                events.append(raw({"type": "assistant", "block": dataclasses.asdict(block)}))
        if message.error:
            events.append(raw({"type": "assistant", "error": message.error}))
        return events
    if isinstance(message, sdk.UserMessage):
        # Tool results come back as user messages; the text of a user message is the prompt or the owner's, which
        # the hub has already.
        if isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, sdk.ToolResultBlock):
                    status = "failed" if block.is_error else "completed"
                    events.append(tool_update(block.tool_use_id, status, _text_of(block.content)))
        return events
    if isinstance(message, sdk.ResultMessage):
        extra = {"numTurns": message.num_turns, "durationMs": message.duration_ms}
        return [usage_update(message.usage or {}, message.total_cost_usd, **extra)]
    data = _raw_of(message)
    if data.get("type") == "system" and data.get("subtype") in SKIPPED_SYSTEM:
        return []
    return [raw(data)]


class ClaudeCodeAdapter(QueueAdapter):
    runtime = "claude-code"
    binary = "claude"

    @classmethod
    def detect(cls) -> Detection:
        return detect_runtime(cls.runtime, cls.binary, MIN_VERSION, packages=(SDK,))

    def __init__(self, context: RunContext):
        super().__init__(context)
        self._session = context.resume_session or str(uuid.uuid4())
        self._client = None
        self._input_open = False
        self._end_input = asyncio.Event()
        self._results: list = []
        self._error: str | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._dir: Path | None = None
        self.options = None

    @property
    def session_id(self) -> str | None:
        return self._session

    def make_client(self, options):
        """The SDK's client; a test hands it a transport of its own."""
        from claude_agent_sdk import ClaudeSDKClient

        return ClaudeSDKClient(options)

    def build_options(self, cli_path: str):
        from claude_agent_sdk import ClaudeAgentOptions

        settings = {
            "cwd": str(self.context.worktree),
            "cli_path": cli_path,
            "permission_mode": "bypassPermissions",
            "system_prompt": {"type": "preset", "preset": "claude_code", "append": HEADLESS_NOTE},
            "env": {key: value for key, value in self.context.env.items() if key not in DROPPED_ENV},
            "stderr": self._stderr.append,
        }
        if self.context.resume_session:
            settings["resume"] = self._session
        else:
            settings["session_id"] = self._session
        for key in ("model", "effort"):
            value = run_setting(self.context, self.runtime, key)
            if value:
                settings[key] = value
        return ClaudeAgentOptions(**settings)

    async def _input(self):
        """The stream the client is connected with: it yields nothing, and its end makes the SDK close the CLI's
        stdin."""
        await self._end_input.wait()
        return
        yield {}  # an async generator

    async def _open(self) -> None:
        program = which(self.binary, self.context.env)
        self._dir = Path(tempfile.mkdtemp(prefix="evo-claude-"))
        self.pid_file = self._dir / "pid"
        self.options = self.build_options(str(write_launcher(self._dir, program)))
        self._client = self.make_client(self.options)
        self._input_open = True
        await self._client.connect(prompt=self._input())
        await self._client.query(self.context.prompt)

    async def _drive(self) -> None:
        from claude_agent_sdk import types as sdk

        try:
            async for message in self._client.receive_messages():
                if isinstance(message, sdk.SystemMessage) and message.subtype == "init":
                    session = message.data.get("session_id")
                    if isinstance(session, str) and session:
                        self._session = session
                self.emit(events_of(message))
                if isinstance(message, sdk.ResultMessage):
                    self._results.append(message)
                    await self._close_input()  # the turn is over: the CLI ends once it has done what it was given
        except Exception as exc:  # the CLI died, or wrote what the SDK cannot read
            self._error = f"{type(exc).__name__}: {exc}"
        self.outcome = self._outcome()

    def _outcome(self) -> Outcome:
        usage: dict = {}
        for result in self._results:
            add_numbers(usage, result.usage)
        if self._results and self._results[-1].total_cost_usd is not None:
            usage["total_cost_usd"] = self._results[-1].total_cost_usd  # the session's, counted by the CLI
        usage = usage or None
        if not self._results:
            why = self._error or "the CLI ended without finishing its turn"
            tail = " | ".join(list(self._stderr)[-3:])
            if tail and not self.interrupted:
                why += f" (stderr: {tail})"
            return Outcome(False, "interrupted" if self.interrupted else cut(f"claude-code: {why}"), usage)
        last = self._results[-1]
        summary = last.result.strip() if isinstance(last.result, str) and last.result.strip() else None
        if self.interrupted:
            return Outcome(False, "interrupted", usage, summary)
        if last.is_error or last.subtype != "success":
            detail = last.result or ", ".join(str(error) for error in last.errors or []) or last.stop_reason
            return Outcome(False, cut(f"claude-code ended its turn with {last.subtype}: {detail}"), usage, summary)
        return Outcome(True, None, usage, summary)

    async def _close_input(self) -> None:
        async with self._lock:
            if self._input_open:
                self._input_open = False
                self._end_input.set()

    async def send(self, text: str) -> None:
        async with self._lock:
            if not self._input_open or self._client is None:
                raise AgentFinished("claude-code has finished its turn and takes no more messages")
            await self._client.query(text)

    async def _interrupt(self) -> None:
        if self._client is not None and self._input_open:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._client.interrupt(), INTERRUPT_TIMEOUT)
        await self._close_input()

    async def _stop_at_boundary(self) -> None:
        await self._close_input()

    async def _close(self) -> None:
        self._input_open = False
        self._end_input.set()
        if self._client is not None:
            await self._client.disconnect()

    def _cleanup(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
