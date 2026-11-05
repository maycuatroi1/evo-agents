"""What the messages of ``claude-agent-sdk`` say, for the Claude Code adapter (``claude_code``): the hub's events of
each (``events_of``), the background commands the agent runs (``Background``), the context its model read
(``context_of``, ``first_request_of``), and an error that reads as the account's usage limit or a failed login
(``limit_of_text``, ``auth_failure_of``). The module docstring of ``claude_code`` tells what Claude Code reports."""

from __future__ import annotations

import base64
import dataclasses
import re
from datetime import UTC, datetime

from evo_agents.hub import digest, outputs
from evo_agents.worker.adapter import AgentEvent, ToolImage
from evo_agents.worker.runtimes.common import (
    first_request,
    message_chunk,
    plan,
    raw,
    thought_chunk,
    tool_call,
    tool_update,
    usage_update,
)

# System messages left out of the log: the CLI's list of slash commands (tens of KiB, twice a session), the start of
# a hook (its response says the same and how it ended), and running estimates of thinking tokens.
SKIPPED_SYSTEM = frozenset({"commands_changed", "hook_started", "thinking_tokens"})
# The text of a turn that ended on the account's usage limit, as Claude Code words it: "Claude AI usage limit
# reached|1791507000" (the reset as a unix time), "5-hour limit reached ∙ resets 3pm", "You've hit your limit".
USAGE_LIMIT = re.compile(r"usage limit|hit your (?:usage )?limit|\blimit reached\b|out of extra usage", re.I)
RESET_STAMP = re.compile(r"\|\s*(\d{9,11})\b")
# The text of a turn that ended because the API refused the account's token, as Claude Code words it: "Failed to
# authenticate. API Error: 401 OAuth access token is invalid." (2.1, run #58 of 2026-10-14), "OAuth token has expired".
AUTH_FAILURE = re.compile(
    r"failed to authenticate|api error: 401\b|oauth (?:access )?token (?:is invalid|has expired|has been revoked)"
    r"|authentication_error|invalid api key",
    re.I,
)
AUTH_CHARS = 300  # of the error kept as the reason
# The statuses of a task that has ended: task_notification says stopped, task_updated killed, for the same end.
TERMINAL_TASK = frozenset({"completed", "failed", "stopped", "killed"})
# The id of a background command in the text of its Bash result.
BACKGROUND_ID = re.compile(r"running in background with ID: ([A-Za-z0-9_-]+)")
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


def text_of(content) -> str | None:
    """The text of a tool result's content: a string, or a list of content blocks, its images left out
    (``tool_result`` takes them out as files)."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if parts:
            return "\n".join(parts)
        rest = [item for item in content if not (isinstance(item, dict) and item.get("type") == "image")]
        return str(rest) if rest else None
    return str(content)


def _image_of(block) -> ToolImage | None:
    """The image of a block of a tool's result, ``{"type": "image", "source": {"type": "base64", "media_type",
    "data"}}``, as Claude Code gives a browser's screenshot or a Read of an image file; None for any other block, and
    for one whose type or bytes do not decode."""
    source = block.get("source") if isinstance(block, dict) and block.get("type") == "image" else None
    if not isinstance(source, dict) or source.get("type") != "base64" or not isinstance(source.get("data"), str):
        return None
    try:
        media_type = outputs.check_media_type(str(source.get("media_type") or ""))
        data = base64.b64decode(source["data"], validate=True)
    except ValueError:  # binascii.Error is one
        return None
    return ToolImage.of(media_type, data) if data else None


def thinking_event(text: str, signature) -> AgentEvent:
    """A thinking block's text: the agent's words when it is a narration (``digest.is_narration``), else a thought."""
    return (message_chunk if digest.is_narration(signature) else thought_chunk)(text)


def tool_result(call_id: str, status: str, content) -> AgentEvent:
    """The ``tool_call_update`` of a tool's result, headless (``events_of``) or from the transcript of a session a
    person drives (``interactive.claude_transcript_events``): its text as before, and each image of it, once per
    SHA-256, a file of the event (``AgentEvent.files``) that its content links to (``outputs.output_link``) instead of
    holding the base64. The run sends those files as outputs of source tool (``runner.outputs``)."""
    event = tool_update(call_id, status, text_of(content))
    found = (_image_of(block) for block in (content if isinstance(content, list) else []))
    images = {image.sha256: image for image in found if image is not None}
    if not images:
        return event
    links = [
        {"type": "content", "content": outputs.output_link(i.sha256, i.name, i.media_type, len(i.data))}
        for i in images.values()
    ]
    body = {**event.body, "content": [*event.body.get("content", []), *links]}
    return AgentEvent(event.kind, body, event.at, tuple(images.values()))


def _true(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


class Background:
    """The commands the agent runs in the background, from what Claude Code reports of them (the module's docstring):
    each Bash call with ``run_in_background`` until its result names its task or says it did not start, and each task
    the CLI reports running in the background until it reports its end."""

    def __init__(self) -> None:
        self.calls: dict[str, str | None] = {}  # tool_use_id -> task id; None until the CLI names the task
        self.running: dict[str, str] = {}  # task id -> description, of the tasks running in the background
        self.ended: dict[str, dict] = {}  # task id -> {description, status, summary, output_file} as the CLI told

    @property
    def pending(self) -> bool:
        """Whether a command the agent started in the background may still run."""
        return bool(self.calls) or bool(self.running)

    def see(self, message) -> list[str]:
        """Follow ``message`` of the SDK; the ids of the background tasks whose end it reports first."""
        from claude_agent_sdk import types as sdk

        if isinstance(message, sdk.AssistantMessage):
            if message.parent_tool_use_id is None:  # a subagent's commands are the subagent's
                for block in message.content:
                    if (
                        isinstance(block, sdk.ToolUseBlock)
                        and block.name == "Bash"
                        and isinstance(block.input, dict)
                        and _true(block.input.get("run_in_background"))
                    ):
                        self.calls[block.id] = None
        elif isinstance(message, sdk.UserMessage):
            self._tool_results(message)
        elif isinstance(message, sdk.SystemMessage) and isinstance(message.data, dict):
            return self._system(message.subtype, message.data)
        return []

    def _tool_results(self, message) -> None:
        from claude_agent_sdk import types as sdk

        if not isinstance(message.content, list):
            return
        blocks = [block for block in message.content if isinstance(block, sdk.ToolResultBlock)]
        extra = message.tool_use_result if isinstance(message.tool_use_result, dict) else {}
        for block in blocks:
            task = extra.get("backgroundTaskId") if len(blocks) == 1 else None
            task = task if isinstance(task, str) and task else None
            if block.tool_use_id in self.calls:
                if task is None:
                    found = BACKGROUND_ID.search(text_of(block.content) or "")
                    task = found.group(1) if found else None
                if block.is_error or task is None:  # it did not start, or ran in the foreground after all
                    del self.calls[block.tool_use_id]
                    continue
                self.calls[block.tool_use_id] = task
            if task is not None and not block.is_error:  # a command the CLI moved to the background, too
                self._start(task, None)

    def _system(self, subtype: str, data: dict) -> list[str]:
        task = data.get("task_id")
        task = task if isinstance(task, str) and task else None
        if subtype == "task_started":
            if task is not None and data.get("is_backgrounded") is True:
                if data.get("tool_use_id") in self.calls:
                    self.calls[data["tool_use_id"]] = task
                self._start(task, data.get("description"))
            return []
        if subtype == "background_tasks_changed":
            listed: dict[str, str | None] = {}
            for item in data.get("tasks") or []:
                if isinstance(item, dict) and isinstance(item.get("task_id"), str) and item["task_id"]:
                    listed[item["task_id"]] = item.get("description")
            for name, description in listed.items():
                self._start(name, description)
            return [name for name in list(self.running) if name not in listed and self._end(name, {})]
        if subtype == "task_notification" and task is not None and data.get("status") in TERMINAL_TASK:
            info = {key: data[key] for key in ("status", "summary", "output_file") if data.get(key)}
            return [task] if self._end(task, info, data.get("tool_use_id")) else []
        if subtype == "task_updated" and task is not None:
            patch = data["patch"] if isinstance(data.get("patch"), dict) else {}
            if patch.get("status") in TERMINAL_TASK:
                return [task] if self._end(task, {"status": patch["status"]}) else []
        return []

    def _start(self, task: str, description) -> None:
        if task in self.ended:
            return
        known = self.running.get(task)
        self.running[task] = description if isinstance(description, str) and description else known or ""

    def _end(self, task: str, info: dict, tool_use_id=None) -> bool:
        """Note the end of ``task``; whether it is the first news of the end of a background command."""
        tracked = task in self.running or task in self.calls.values() or tool_use_id in self.calls
        description = self.running.pop(task, None)
        for call, known in list(self.calls.items()):
            if known == task or call == tool_use_id:
                del self.calls[call]
        if task in self.ended:
            self.ended[task].update(info)
            return False
        if not tracked:  # a command of the foreground, whose task ends with it
            return False
        self.ended[task] = {"description": description or "", **info}
        return True

    def describe(self, task: str) -> str:
        """One line on a background task that ended, for FOLLOW_UP."""
        info = self.ended.get(task, {})
        line = f"- {info.get('summary') or info.get('description') or task} ({info.get('status') or 'ended'})"
        if info.get("output_file"):
            line += f"; its output is in {info['output_file']}"
        return line


def opens_turn(message) -> bool:
    """Whether ``message`` comes from a turn: the CLI's init at the start of one, or the agent's own message."""
    from claude_agent_sdk import types as sdk

    if isinstance(message, (sdk.AssistantMessage, sdk.StreamEvent)):
        return True
    return isinstance(message, sdk.SystemMessage) and message.subtype == "init"


def unix_time(value) -> str | None:
    """A unix time of the CLI as ISO 8601 in UTC; None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def limit_of_text(text: str | None) -> dict | None:
    """The limit an error of the agent's turn names, when it reads as the account's usage limit: ``{status:
    rejected}``, with resets_at when the text carries the reset as a unix time; None otherwise."""
    if not text or not USAGE_LIMIT.search(text):
        return None
    found = RESET_STAMP.search(text)
    resets_at = unix_time(int(found[1])) if found else None
    return {"status": "rejected", **({"resets_at": resets_at} if resets_at else {})}


def auth_failure_of(text: str | None) -> str | None:
    """The error of the agent's turn, on one line, when it reads as its account failing to authenticate (the API
    refused its token); None otherwise."""
    if not text or not AUTH_FAILURE.search(text):
        return None
    return " ".join(text.split())[:AUTH_CHARS]


def _raw_of(message) -> dict:
    """A message of the SDK as the CLI wrote it, as far as the SDK keeps it."""
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        return data
    info = getattr(message, "rate_limit_info", None)
    if info is not None:
        return {"type": "rate_limit_event", "rate_limit_info": getattr(info, "raw", None) or {}}
    if dataclasses.is_dataclass(message) and not isinstance(message, type):
        return {"type": type(message).__name__, **dataclasses.asdict(message)}
    return {"type": type(message).__name__}


def context_of(message) -> int | None:
    """The tokens of the context a message of the model read, as its usage counts them (``digest.context_tokens``:
    input, cache creation and cache read); None for any other message, one of a subagent (``parent_tool_use_id``),
    whose context is its own, and one no model sent."""
    from claude_agent_sdk import types as sdk

    if not isinstance(message, sdk.AssistantMessage) or message.parent_tool_use_id is not None:
        return None
    if message.model == digest.SYNTHETIC_MODEL:
        return None
    return digest.context_tokens(getattr(message, "usage", None))


def first_request_of(message) -> dict | None:
    """The FIRST_REQUEST keys of the run's usage from a message ``context_of`` reads, the first of which is the agent's
    first request: the tokens it read from the cache and wrote to it. None for any other message."""
    if context_of(message) is None:
        return None
    usage = message.usage
    return first_request(usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"))


def _block_events(block) -> list[AgentEvent]:
    """The hub's events for one content block of an ``assistant`` message."""
    from claude_agent_sdk import types as sdk

    if isinstance(block, sdk.TextBlock):
        return [message_chunk(block.text)] if block.text else []
    if isinstance(block, sdk.ThinkingBlock):
        # thinking the API keeps to itself comes with an empty text
        return [thinking_event(block.thinking, block.signature)] if block.thinking.strip() else []
    if isinstance(block, (sdk.ToolUseBlock, sdk.ServerToolUseBlock)):
        events = [tool_call(block.id, block.name, TOOL_KINDS.get(block.name, "other"), "pending", block.input)]
        todos = block.input.get("todos") if block.name == "TodoWrite" and isinstance(block.input, dict) else None
        if isinstance(todos, list):
            entries = [
                (str(t.get("content") or ""), str(t.get("status") or ""), "medium")
                for t in todos
                if isinstance(t, dict)
            ]
            events.append(plan(entries))
        return events
    if isinstance(block, (sdk.ToolResultBlock, sdk.ServerToolResultBlock)):
        status = "failed" if getattr(block, "is_error", False) else "completed"
        return [tool_result(block.tool_use_id, status, block.content)]
    return [raw({"type": "assistant", "block": dataclasses.asdict(block)})]


def events_of(message) -> list[AgentEvent]:
    """The hub's events for one message of ``ClaudeSDKClient.receive_messages()``."""
    from claude_agent_sdk import types as sdk

    events: list[AgentEvent] = []
    if isinstance(message, sdk.AssistantMessage):
        for block in message.content:
            events.extend(_block_events(block))
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
                    events.append(tool_result(block.tool_use_id, status, block.content))
        return events
    if isinstance(message, sdk.ResultMessage):
        # Each model's tokens and cost so far in the session (modelUsage) and the session's id: the hub's ledger of
        # what the run spent by model, and its share of a session it went on in (evo_agents.hub.run_spend).
        models = getattr(message, "model_usage", None) or None
        extra = {"numTurns": message.num_turns, "durationMs": message.duration_ms, "models": models}
        extra["session_id"] = message.session_id or None
        return [usage_update(message.usage or {}, message.total_cost_usd, **extra)]
    data = _raw_of(message)
    if data.get("type") == "system" and data.get("subtype") in SKIPPED_SYSTEM:
        return []
    return [raw(data)]
