"""What a session's transcripts and a run's events say, read into a facet (``evo_agents.hub.facets``).

- ``session_facet`` reads the transcript files of a session, its own (main) first and then its subagents', each a
  Claude Code transcript or, when its first line is a ``session_meta``, a Codex rollout. A Claude Code transcript is
  read as ``evo_agents.hub.digest`` reads it, and more: the hooks (the ``hook_*`` attachments, with their
  ``durationMs``, and the ``stop_hook_summary`` lines), the skills (the Skill tool, and the ``invoked_skills``
  attachments of the skills that came back into the context), the compactions with their trigger and size
  (``compact_boundary``), the cost (the largest ``totalCostUSD`` of the ``cost-state`` lines of the main file), the
  errors of the model's API (``api_error``), and the time of each call, from the line of its call to the line of its
  result; in the main thread, the sources of its context (``facets.CONTEXT_SOURCES``): the files of the
  ``instructions`` attachments (CLAUDE.md, AGENTS.md, a memory index), the blocks of ``mcp_instructions_delta``, the
  ``skill_listing`` (a whole list unless ``isInitial`` is false, and whether the context already held one), the text
  of hooks (``hook_additional_context`` and the content of ``hook_success``), and the text of each skill loaded (the
  ``isMeta`` user line that starts with SKILL_TEXT, named by the Skill call of its ``sourceToolUseID``, else by its
  directory) or brought back by a compaction (the content of ``invoked_skills``). What the person wrote counts in the
  main thread only, a compaction's summary left out. A message of the model
  counts once, with the usage of its last line, as in the digest. A Codex rollout is read through its items and token
  counts, as the digest reads it; the usage of each of its threads (main and subagents) adds up.
- ``run_facet`` reads the events of a run (``docs/workers.md``, "Event kinds"), from its log or from the hub: its tool
  calls and their results, named as the run's tool figures name them (``tool_name``), the owner's messages as the
  person's turns, and the compactions the runtime's raw output says; the usage and the cost come from the run's own
  ``usage``, as its worker reported it.

Standard library only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta

from evo_agents.hub import digest
from evo_agents.hub.digest import USAGE_KEYS
from evo_agents.hub.facets import Tally, count, timestamp

MAIN = "main"  # the session's own transcript (evo_agents.hub.transcripts.MAIN)
NAMED_BY_TITLE = "claude-code"  # the runtime whose title of a call is its tool's name (server.tool_stats)
UNNAMED = "unknown"
ENDED = ("completed", "failed")
HOOK_TYPES = frozenset(
    {"hook_success", "hook_cancelled", "hook_non_blocking_error", "hook_blocking_error", "hook_error_during_execution"}
)
CODEX_TOKENS = {
    "cached_input_tokens": "cache_read_input_tokens",
    "cache_write_input_tokens": "cache_creation_input_tokens",
}
# The keys of a run's usage its worker reports (evo_agents.worker.runtimes.common): the largest context of one request,
# and what the first request read from the cache and wrote to it.
PEAK_CONTEXT = "peak_context_tokens"
FIRST_REQUEST = ("first_request_cache_read_input_tokens", "first_request_cache_creation_input_tokens")
SKILL_TEXT = "Base directory for this skill:"  # how Claude Code starts the text of a skill it loads, then its directory
CLEAR = "<command-name>/clear</command-name>"


def _items(line: dict) -> list[dict]:
    message = line.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if content is None:
        content = line.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [item for item in content if isinstance(item, dict)] if isinstance(content, list) else []


def _text(items: list[dict]) -> str:
    return "\n".join(str(item.get("text") or "") for item in items if item.get("type") == "text" and item.get("text"))


def _result_text(content) -> str:
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def _result_chars(content) -> int:
    if isinstance(content, list):
        return sum(len(str(part.get("text") or "")) for part in content if isinstance(part, dict))
    return len(content) if isinstance(content, str) else 0


# A Claude Code session


class ClaudeReader:
    """The lines of a Claude Code session's transcript files, into ``tally``."""

    def __init__(self, tally: Tally):
        self.tally = tally
        self.usage: dict[str, dict] = {}  # message id: the usage of its last line
        self.contexts: dict[str, int] = {}  # message id of the main thread: the tokens of its context
        self.skill_calls: dict[str, str] = {}  # id of a Skill call: the skill it loads

    def add(self, file: str, number: int, line) -> None:
        if not isinstance(line, dict):
            return
        kind = line.get("type")
        main = file == MAIN and not line.get("isSidechain")
        if main:
            self.tally.sees_context()
        if kind == "system":
            self._system(line, main, f"{file}:{number}")
        elif kind == "attachment":
            self._attachment(line.get("attachment"), main)
        elif kind == "cost-state" and file == MAIN:
            cost = line.get("totalCostUSD")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                self.tally.cost = max(self.tally.cost or 0.0, float(cost))
        elif kind == "user" and line.get("isMeta"):
            if main:
                self._meta(line)
        elif kind in ("user", "assistant") and not line.get("isMeta"):
            at = timestamp(line.get("timestamp"))
            self.tally.message(at)
            if kind == "user":
                self._user(line, at, main, f"{file}:{number}")
            else:
                self._assistant(line, at, main, f"{file}:{number}")

    def _system(self, line: dict, main: bool, where: str) -> None:
        subtype = line.get("subtype")
        if subtype == digest.COMPACT_BOUNDARY and main:
            meta = line.get("compactMetadata") if isinstance(line.get("compactMetadata"), dict) else {}
            self.tally.compaction(str(line.get("uuid") or where), meta.get("trigger"), meta.get("preTokens"))
        elif subtype == "stop_hook_summary":
            for info in line.get("hookInfos") or []:
                if isinstance(info, dict) and "durationMs" in info:
                    self.tally.hook("Stop", info.get("command"), info.get("durationMs"))
        elif subtype == "api_error":
            error = line.get("error") if isinstance(line.get("error"), dict) else {}
            self.tally.api_error(error.get("formatted") or error.get("message") or "")

    def _attachment(self, found, main: bool) -> None:
        if not isinstance(found, dict):
            return
        kind = found.get("type")
        if kind in HOOK_TYPES:
            timed_out = bool(found.get("timedOut"))
            failed = kind != "hook_success" and not timed_out
            name = found.get("hookName") or found.get("hookEvent")
            self.tally.hook(name, found.get("command"), found.get("durationMs"), failed=failed, timed_out=timed_out)
            if main and kind == "hook_success":
                self.tally.context_text("hooks", found.get("content"))
        elif kind == "invoked_skills":
            for skill in found.get("skills") or []:
                if isinstance(skill, dict):
                    self.tally.skill_back(skill.get("name"))
                    if main:
                        self.tally.skill_body(skill.get("name"), skill.get("content"))
        elif main:
            self._context(kind, found)

    def _context(self, kind, found: dict) -> None:
        """A source of the main thread's context (``facets.CONTEXT_SOURCES``) an attachment names."""
        if kind == "instructions":
            for item in found.get("files") or []:
                if isinstance(item, dict):
                    self.tally.load_file(item.get("path"), item.get("type"), item.get("content"))
        elif kind == "mcp_instructions_delta":
            for block in found.get("addedBlocks") or []:
                self.tally.context_text("mcp_instructions", block)
        elif kind == "skill_listing":
            self.tally.skill_list(found.get("content"), found.get("isInitial") is not False)
        elif kind == "hook_additional_context":
            content = found.get("content")
            for text in content if isinstance(content, list) else [content]:
                self.tally.context_text("hooks", text)

    def _meta(self, line: dict) -> None:
        """A line the runtime wrote as the person's in the main thread (``isMeta``): the text of a skill it loaded."""
        text = _text(_items(line))
        if text.startswith(SKILL_TEXT):
            directory = text[len(SKILL_TEXT) :].split("\n", 1)[0].strip().rstrip("/")
            name = self.skill_calls.get(str(line.get("sourceToolUseID") or "")) or directory.rsplit("/", 1)[-1]
            self.tally.skill_body(name, text)

    def _user(self, line: dict, at: datetime | None, main: bool, where: str) -> None:
        items = _items(line)
        if main and not line.get("isCompactSummary"):
            self.tally.turn(_text(items))
            if CLEAR in _text(items):
                self.tally.cleared()
        for item in items:
            if item.get("type") != "tool_result":
                continue
            content = item.get("content", "")
            chars = _result_chars(content) if main else None
            failed = bool(item.get("is_error"))
            self.tally.result(str(item.get("tool_use_id")), at, failed, _result_text(content), chars, where)

    def _assistant(self, line: dict, at: datetime | None, main: bool, where: str) -> None:
        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        key = str(message.get("id") or line.get("uuid") or where)
        model = message.get("model")
        if key not in self.usage:
            self.tally.model(model)
        if isinstance(message.get("usage"), dict):
            self.usage[key] = message["usage"]
            context = digest.context_tokens(message["usage"])
            if main and context is not None and model != digest.SYNTHETIC_MODEL:
                self.contexts[key] = context
        else:
            self.usage.setdefault(key, {})
        for item in _items(line):
            if item.get("type") in ("tool_use", "server_tool_use"):
                self.tally.call(str(item.get("id") or ""), item.get("name") or UNNAMED, item.get("input"), at, where)
                given = item.get("input") if isinstance(item.get("input"), dict) else {}
                if item.get("name") == "Skill" and item.get("id") and isinstance(given.get("skill"), str):
                    self.skill_calls[str(item["id"])] = given["skill"]

    def close(self) -> None:
        for usage in self.usage.values():
            self.tally.add_usage(usage)
        for context in self.contexts.values():
            self.tally.context(context)


# A Codex rollout


def _codex_failed(item: dict) -> bool:
    exit_code = item.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return True
    return str(item.get("status") or "") in ("failed", "declined", "error")


def _codex_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("text"))
    return ""


class CodexReader:
    """The lines of one Codex rollout of a session (its own thread or a subagent's), into ``tally``. Its token counts
    are running totals: the largest of each is the thread's, added to the session's when the file ends."""

    def __init__(self, tally: Tally, file: str):
        self.tally, self.file, self.main = tally, file, file == MAIN
        self.totals = dict.fromkeys(USAGE_KEYS, 0)

    def add(self, file: str, number: int, line) -> None:
        if not isinstance(line, dict):
            return
        at = timestamp(line.get("timestamp"))
        self.tally.moment(at)
        payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
        kind = line.get("type")
        if kind == "turn_context":
            self.tally.model(payload.get("model"))
        elif kind == "event_msg" and payload.get("type") == "token_count":
            self._tokens(payload.get("info") if isinstance(payload.get("info"), dict) else {})
        elif kind == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            self._item(item, at, f"{file}:{number}")

    def _tokens(self, info: dict) -> None:
        last = info.get("last_token_usage")
        if isinstance(last, dict) and self.main:
            self.tally.context(count(last.get("input_tokens")))
        total = info.get("total_token_usage")
        if not isinstance(total, dict):
            return
        cached = count(total.get("cached_input_tokens")) or 0
        found = {"input_tokens": max((count(total.get("input_tokens")) or 0) - cached, 0)}
        found["output_tokens"] = count(total.get("output_tokens")) or 0
        found.update({dst: count(total.get(src)) or 0 for src, dst in CODEX_TOKENS.items()})
        for key, value in found.items():
            self.totals[key] = max(self.totals[key], value)

    def _item(self, item: dict, at: datetime | None, where: str) -> None:
        kind = item.get("type")
        call = str(item.get("id") or where)
        if kind in ("UserMessage", "AgentMessage", "CommandExecution", "McpToolCall", "FileChange", "WebSearch"):
            self.tally.message()
        if kind == "UserMessage" and self.main:
            self.tally.turn(_codex_text(item.get("content")))
        elif kind == "CommandExecution":
            output = str(item.get("aggregated_output") or "")
            self._call(call, "shell", digest.codex_command(item), at, where, item)
            self.tally.result(call, at, _codex_failed(item), output, len(output) if self.main else None, where)
        elif kind == "McpToolCall":
            server, name = item.get("server"), item.get("tool")
            content = item.get("result")
            text = _codex_text(content.get("content")) if isinstance(content, dict) else _codex_text(content)
            self._call(
                call, f"mcp__{server}__{name}" if server else f"mcp__{name}", item.get("arguments"), at, where, item
            )
            self.tally.result(call, at, _codex_failed(item), text, len(text) if self.main else None, where)
        elif kind == "FileChange":
            self._call(call, "apply_patch", {}, at, where, item)
            for path in item.get("changes") or {}:
                if isinstance(path, str) and path:
                    self.tally.edit(path)
            self.tally.result(call, at, _codex_failed(item), "", None, where)
        elif kind == "WebSearch":
            self._call(call, "web_search", {}, at, where, item)
            self.tally.result(call, at, False, "", None, where)
        elif kind == "ContextCompaction" and self.main:
            self.tally.compaction(call, "auto")

    def _call(self, call: str, tool: str, given, at: datetime | None, where: str, item: dict) -> None:
        took = count(item.get("duration_ms"))
        started = at - timedelta(milliseconds=took) if at is not None and took is not None else None
        self.tally.call(call, tool, given, started, where)

    def close(self) -> None:
        self.tally.add_usage(self.totals)


def session_facet(files: Iterable[tuple[str, Iterable[tuple[int, object]]]], runtime: str | None = None) -> dict:
    """The facet of a session whose transcript files are ``files``: each (its key, its lines as (number, JSON value)),
    the session's own (main) first."""
    tally = Tally(runtime)
    claude = ClaudeReader(tally)
    for file, lines in files:
        reader = None
        last = 0
        for number, line in lines:
            last = number
            if reader is None:
                reader = CodexReader(tally, file) if digest.is_codex(line) else claude
            reader.add(file, number, line)
        if isinstance(reader, CodexReader):
            reader.close()
        tally.files.append({"file": file, "lines": last})
    claude.close()
    return tally.body()


# A run


def tool_name(runtime: str | None, title, kind) -> str:
    """The name a run's call goes under, as its tool figures name it (``evo_agents.hub.server.tool_stats``): the title
    for Claude Code, else the call's kind unless it is ``other``, else the title."""
    title, kind = str(title or "").strip(), str(kind or "").strip()
    if runtime == NAMED_BY_TITLE and title:
        return title[: digest.MAX_NAME]
    if kind and kind != "other":
        return kind[: digest.MAX_NAME]
    return (title or kind or UNNAMED)[: digest.MAX_NAME]


def _event_text(body: dict) -> str:
    parts = []
    for item in body.get("content") or []:
        inner = item.get("content") if isinstance(item, dict) else None
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            parts.append(inner["text"])
    if parts:
        return "\n".join(parts)
    raw = body.get("rawOutput")
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True)


def run_usage(usage) -> dict:
    """The tokens of a run's ``usage`` as a facet counts them (``digest.USAGE_KEYS``), from Claude Code's names or
    Codex's (``cached_input_tokens`` inside ``input_tokens``)."""
    usage = usage if isinstance(usage, dict) else {}
    cached = count(usage.get("cached_input_tokens"))
    found = {key: count(usage.get(key)) or 0 for key in USAGE_KEYS}
    if cached is not None and not found["cache_read_input_tokens"]:
        found["cache_read_input_tokens"] = cached
        found["input_tokens"] = max(found["input_tokens"] - cached, 0)
    return found


class RunReader:
    """The events of one run, in seq order, into ``tally``."""

    def __init__(self, tally: Tally, runtime: str | None):
        self.tally, self.runtime = tally, runtime
        self.started: set[str] = set()
        self.ended: set[str] = set()

    def add(self, event) -> None:
        if not isinstance(event, dict):
            return
        body = event.get("body") if isinstance(event.get("body"), dict) else {}
        at, seq, kind = timestamp(event.get("at")), count(event.get("seq")) or 0, event.get("kind")
        self.tally.moment(at)
        if kind in ("tool_call", "tool_call_update"):
            self._tool(kind, body, at, seq)
        elif kind == "agent_message_chunk":
            self.tally.message(at)
        elif kind == "user_message":
            self.tally.message(at)
            self.tally.turn(str(body.get("text") or ""))
        elif kind == "output":
            self._raw(body.get("raw") if isinstance(body.get("raw"), dict) else {}, seq)

    def _tool(self, kind: str, body: dict, at: datetime | None, seq: int) -> None:
        call = str(body.get("toolCallId") or "")
        if not call:
            return
        if kind == "tool_call" and call not in self.started:
            self.started.add(call)
            name = tool_name(self.runtime, body.get("title"), body.get("kind"))
            given = body.get("rawInput")
            if name == "execute" and not isinstance(given, dict):
                given = {"command": body.get("title")}
            if name == "edit" and isinstance(given, dict):
                for change in given.get("changes") or []:
                    if isinstance(change, dict) and isinstance(change.get("path"), str):
                        self.tally.edit(change["path"])
            self.tally.call(call, name, given, at, seq)
        status = body.get("status")
        if status in ENDED and call in self.started and call not in self.ended:
            self.ended.add(call)
            text = _event_text(body)
            self.tally.result(call, at, status == "failed", text, len(text), seq)

    def _raw(self, raw: dict, seq: int) -> None:
        if raw.get("type") == "system" and raw.get("subtype") == digest.COMPACT_BOUNDARY:
            meta = raw.get("compact_metadata") or raw.get("compactMetadata") or {}
            meta = meta if isinstance(meta, dict) else {}
            self.tally.compaction(str(raw.get("uuid") or seq), meta.get("trigger"), meta.get("pre_tokens"))


class RunFacet:
    """The facet of one run, its events added one at a time in seq order (each ``{seq, at, kind, body}``)."""

    def __init__(self, runtime: str | None):
        self.tally = Tally(runtime)
        self.reader = RunReader(self.tally, runtime)
        self.seen = 0

    def add(self, event) -> None:
        self.seen += 1
        self.reader.add(event)

    def body(self, usage=None, cost: float | None = None) -> dict:
        """The facet, with the tokens of the run's ``usage``, its peak context (PEAK_CONTEXT), the context of its first
        request (FIRST_REQUEST) and ``cost``."""
        tally = self.tally
        tally.files.append({"file": "events", "lines": self.seen})
        tally.add_usage(run_usage(usage))
        if isinstance(usage, dict):
            tally.context_peak = count(usage.get(PEAK_CONTEXT)) or None
            tally.context_first = sum(count(usage.get(key)) or 0 for key in FIRST_REQUEST) or None
        tally.cost = cost
        return tally.body()


def run_facet(runtime: str | None, events: Iterable, usage=None, cost: float | None = None) -> dict:
    """The facet of a run of ``runtime`` whose events, in seq order, are ``events``: ``RunFacet``."""
    builder = RunFacet(runtime)
    for event in events:
        builder.add(event)
    return builder.body(usage, cost)
