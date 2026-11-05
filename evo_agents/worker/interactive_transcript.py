"""The session's log while a person drives it (``evo_agents.worker.interactive``): the records a runtime appends to
its transcript or rollout, mapped to the hub's events, and the text a terminal printed."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import glob
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

from evo_agents.worker.adapter import AgentEvent
from evo_agents.worker.runtimes import claude_events, common

log = logging.getLogger("evo_agents.worker")

POLL = 0.5  # seconds between looks at a transcript
PANE_POLL = 1.0  # and at the terminal's own output
MAX_READ = 4 * 1024 * 1024  # bytes of a transcript read at once
MAX_LINE = 16 * 1024 * 1024  # a transcript line longer than this is skipped
PANE_EVENT_CHARS = 16 * 1024  # characters of terminal text in one output event
# Records of Claude Code's transcript that repeat what others say or keep its own state.
CLAUDE_SKIPPED = frozenset(
    {"attachment", "last-prompt", "atis-latch", "queue-operation", "file-history-snapshot", "cost-state", "summary"}
)


def home_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or os.path.expanduser("~"))


def worktree_folders(path: Path) -> list[str]:
    """``path`` as given and as resolved: a runtime may key a folder by either."""
    names = [str(path)]
    resolved = os.path.realpath(path)
    if resolved not in names:
        names.append(resolved)
    return names


# Where Claude Code keeps its state


def claude_config_dir(env: Mapping[str, str]) -> Path:
    """Where Claude Code keeps its settings, sessions and transcripts: CLAUDE_CONFIG_DIR, else ``~/.claude``."""
    value = env.get("CLAUDE_CONFIG_DIR")
    return Path(value).expanduser() if value else home_dir(env) / ".claude"


def claude_state_path(env: Mapping[str, str]) -> Path:
    """Claude Code's own state file, which holds ``projects[<folder>].hasTrustDialogAccepted``: ``.claude.json`` in
    CLAUDE_CONFIG_DIR when that is set, else ``~/.claude.json``."""
    value = env.get("CLAUDE_CONFIG_DIR")
    return (Path(value).expanduser() if value else home_dir(env)) / ".claude.json"


# The session's log while a person drives it


class JsonlFollower:
    """The records a runtime appends to a JSONL file, from ``offset`` on; a line not yet ended waits for its end."""

    def __init__(self, path: Path | None = None, offset: int = 0):
        self.path = path
        self.offset = offset
        self._partial = b""

    def read(self) -> list[dict]:
        if self.path is None:
            return []
        records: list[dict] = []
        while True:
            try:
                with open(self.path, "rb") as handle:
                    size = os.fstat(handle.fileno()).st_size
                    if size < self.offset:  # replaced or cut: start again
                        self.offset, self._partial = 0, b""
                    handle.seek(self.offset)
                    data = handle.read(MAX_READ)
            except OSError:
                return records
            if not data:
                return records
            self.offset += len(data)
            lines = (self._partial + data).split(b"\n")
            self._partial = lines.pop()
            if len(self._partial) > MAX_LINE:
                self._partial = b""
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
            if len(data) < MAX_READ:
                return records


async def follow_jsonl(
    locate: Callable[[], tuple[Path, int] | None],
    events_of: Callable[[dict], list[AgentEvent]],
    stop: asyncio.Event,
    poll: float = POLL,
) -> AsyncIterator[AgentEvent]:
    """The events of the records appended to the file ``locate`` finds (with the offset to start at), until ``stop``
    is set; a last read follows it."""
    follower = JsonlFollower()
    while True:
        stopping = stop.is_set()
        if follower.path is None:
            found = await asyncio.to_thread(locate)
            if found is not None:
                follower.path, follower.offset = found
        for record in await asyncio.to_thread(follower.read):
            try:
                events = events_of(record)
            except Exception:  # a record of a shape not seen before must not end the log
                log.debug("a transcript record was not mapped", exc_info=True)
                continue
            for event in events:
                yield event
        if stopping:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), poll)


_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: cursor moves, colours, modes
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: titles, hyperlinks
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS, SOS, PM, APC
    r"|\x1b[()*+][0-9A-Za-z]"  # character sets
    r"|\x1b[@-Z\\-_=>78]"  # two-byte sequences
)
_BLANK_LINES = re.compile(r"\n{3,}")


def terminal_text(text: str) -> str:
    """What a terminal printed, as text: escape sequences and control characters removed, lines ended by newlines."""
    text = _ESCAPES.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())
    lines = text.split("\n")
    lines[:-1] = [line.rstrip() for line in lines[:-1]]  # the last line may go on in the next read
    return _BLANK_LINES.sub("\n\n", "\n".join(lines))


def _complete(text: str) -> tuple[str, str]:
    """``text`` cut before an escape sequence that has not ended yet: (what can be cleaned now, what waits)."""
    start = text.rfind("\x1b")
    if start < 0 or len(text) - start > 256:
        return text, ""
    tail = text[start:]
    if _ESCAPES.match(tail) or (len(tail) >= 2 and tail[1] not in "[]PX^_()*+"):
        return text, ""
    return text[:start], tail


async def follow_pane(path: Path, stop: asyncio.Event, poll: float = PANE_POLL) -> AsyncIterator[AgentEvent]:
    """``output`` events ``{"terminal": text}`` of what tmux pipe-pane appends to ``path``, until ``stop`` is set."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    offset = 0
    waiting = ""
    while True:
        stopping = stop.is_set()
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(MAX_READ)
        except OSError:
            data = b""
        offset += len(data)
        text, waiting = _complete(waiting + decoder.decode(data, final=stopping))
        if stopping:
            text, waiting = text + waiting, ""
        cleaned = terminal_text(text)
        for start in range(0, len(cleaned), PANE_EVENT_CHARS):
            piece = cleaned[start : start + PANE_EVENT_CHARS]
            if piece.strip():
                yield AgentEvent("output", {"terminal": piece})
        if stopping and len(data) < MAX_READ:
            return
        if len(data) < MAX_READ:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), poll)


def claude_transcript_events(record: dict) -> list[AgentEvent]:
    """The hub's events for one record of Claude Code's transcript (``<session>.jsonl``): the assistant's text (a
    narration too), thinking and tool calls, the results of the tools, and what the person typed (``output``)."""
    kind = record.get("type")
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if kind == "assistant":
        return _claude_assistant_events(content)
    if kind == "user":
        return [] if record.get("isMeta") else _claude_user_events(content)
    if kind in CLAUDE_SKIPPED:
        return []
    return [common.raw(record)]


def _claude_assistant_events(content) -> list[AgentEvent]:
    """The events of an assistant record's content: text, thinking, and tool calls (a TodoWrite's plan too)."""
    events: list[AgentEvent] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and block.get("text"):
            events.append(common.message_chunk(str(block["text"])))
        elif block_type == "thinking" and str(block.get("thinking") or "").strip():
            events.append(claude_events.thinking_event(str(block["thinking"]), block.get("signature")))
        elif block_type in ("tool_use", "server_tool_use"):
            name = str(block.get("name") or "tool")
            tool_input = block.get("input")
            kind_of = claude_events.TOOL_KINDS.get(name, "other")
            events.append(common.tool_call(str(block.get("id") or ""), name, kind_of, "pending", tool_input))
            todos = tool_input.get("todos") if name == "TodoWrite" and isinstance(tool_input, dict) else None
            if isinstance(todos, list):
                entries = [
                    (str(t.get("content") or ""), str(t.get("status") or ""), "medium")
                    for t in todos
                    if isinstance(t, dict)
                ]
                events.append(common.plan(entries))
    return events


def _claude_user_events(content) -> list[AgentEvent]:
    """The events of a user record's content: the tools' results, and what the person typed."""
    if isinstance(content, str):
        return [common.raw({"type": "user", "text": content})] if content.strip() else []
    events: list[AgentEvent] = []
    typed = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            status = "failed" if block.get("is_error") else "completed"
            events.append(claude_events.tool_result(str(block.get("tool_use_id") or ""), status, block.get("content")))
        elif block.get("type") == "text" and str(block.get("text") or "").strip():
            typed.append(str(block["text"]))
    if typed:
        events.append(common.raw({"type": "user", "text": "\n".join(typed)}))
    return events


CODEX_SHELL_TOOLS = frozenset({"shell", "exec_command", "local_shell", "container.exec", "unified_exec"})


def _codex_text(output) -> str | None:
    if isinstance(output, str):
        return output
    if isinstance(output, dict) and isinstance(output.get("output"), str):
        return output["output"]
    if isinstance(output, list):
        parts = [item["text"] for item in output if isinstance(item, dict) and isinstance(item.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


def codex_rollout_events(record: dict) -> list[AgentEvent]:
    """The hub's events for one record of a Codex rollout (``rollout-*-<thread>.jsonl``): the assistant's messages
    and reasoning, the tool calls and their output, token counts, what the person typed (``output``) and an aborted
    turn (``output``). Records that repeat these (``event_msg`` items, agent messages) or hold the thread's settings
    are left out."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []
    kind, item = record.get("type"), payload.get("type")
    if kind == "response_item":
        if item == "message":
            if payload.get("role") != "assistant":
                return []  # the person's own words come as event_msg user_message; the rest is context
            texts = [
                str(part.get("text"))
                for part in payload.get("content") or []
                if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text")
            ]
            return [common.message_chunk("\n".join(texts))] if texts else []
        if item == "reasoning":
            texts = [
                str(part.get("text"))
                for part in payload.get("summary") or []
                if isinstance(part, dict) and part.get("text")
            ]
            text = "\n".join(texts)
            return [common.thought_chunk(text)] if text.strip() else []
        if item in ("function_call", "custom_tool_call"):
            name = str(payload.get("name") or "tool")
            arguments = payload.get("arguments") if item == "function_call" else {"input": payload.get("input")}
            if isinstance(arguments, str):
                with contextlib.suppress(ValueError):
                    arguments = json.loads(arguments)
            tool_kind = "execute" if name in CODEX_SHELL_TOOLS else "edit" if name == "apply_patch" else "other"
            return [common.tool_call(str(payload.get("call_id") or ""), name, tool_kind, "in_progress", arguments)]
        if item in ("function_call_output", "custom_tool_call_output"):
            call_id = str(payload.get("call_id") or "")
            return [common.tool_update(call_id, "completed", _codex_text(payload.get("output")))]
        if item in ("local_shell_call", "web_search_call"):
            return [common.raw(record)]
        return []
    if kind == "event_msg":
        if item == "user_message":
            text = payload.get("message")
            return [common.raw({"type": "user_message", "message": text})] if isinstance(text, str) else []
        if item == "token_count" and isinstance(payload.get("info"), dict):
            info = payload["info"]
            usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
            return [
                common.usage_update(
                    usage, last=info.get("last_token_usage"), contextWindow=info.get("model_context_window")
                )
            ]
        if item == "turn_aborted":
            return [common.raw(record)]
    return []


def claude_transcript(env: Mapping[str, str], session_id: str) -> Path | None:
    """Claude Code's transcript of session ``session_id``, ``<config>/projects/<the folder, mangled>/<ID>.jsonl`` in
    the config dir of ``env`` (``claude_config_dir``), found by the session's id; None when there is none."""
    pattern = str(claude_config_dir(env) / "projects" / "*" / f"{glob.escape(session_id)}.jsonl")
    found = sorted(glob.glob(pattern))
    return Path(found[0]) if found else None


def codex_rollout(env: Mapping[str, str], session_id: str) -> Path | None:
    """Codex's rollout of thread ``session_id``, ``$CODEX_HOME/sessions/Y/M/D/rollout-*-<ID>.jsonl`` (``codex_home``
    of ``env``), the latest when there are several; None when there is none."""
    sessions = codex_home(env) / "sessions"
    pattern = str(sessions / "*" / "*" / "*" / f"rollout-*-{glob.escape(session_id)}.jsonl")
    found = sorted(glob.glob(pattern))
    return Path(found[-1]) if found else None


def codex_home(env: Mapping[str, str]) -> Path:
    value = env.get("CODEX_HOME")
    return Path(value).expanduser() if value else home_dir(env) / ".codex"
