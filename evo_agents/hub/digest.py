"""The digest of a Claude Code or Codex session: what the Stop hook of the evo-hub plugin tells the hub about a session,
built on this machine from the session's transcript. A Codex session's transcript is a rollout (``codex exec`` / the
app-server session log); ``_CodexTally`` reads it to the same fields, so one digest shape serves both runtimes.

A session counts once its transcript holds MIN_MESSAGES messages: lines from the person or the model, meta lines left
out, as the ``digest`` of the harness-engineering skill counts them (``harness.py``), so the two agree on which
sessions have a digest. The digest keeps that skill's fields and adds the figures of each tool:

- ``cwd``, ``messages``, ``started_at`` and ``ended_at`` (the first and last of those lines), ``model`` (the one most
  of the model's messages name) and ``models``, and ``usage``, the tokens of every message of the model counted once:
  ``input_tokens``, ``output_tokens``, ``cache_creation_input_tokens`` and ``cache_read_input_tokens``;
- ``tools``: per tool, under ``gen_ai.tool.name`` as OpenTelemetry's GenAI conventions name it, how many calls and how
  many of them failed (a tool result marked as an error);
- ``bash``: the same per program of the Bash calls (``friction.programs``: each part's first word that is not ``cd``
  or a variable, with the subcommand of the tools that have one, ``git push`` or ``python -m pytest``), a command that
  runs several programs counting a call for each; a failure counts for a program when the command ran it alone or its
  output names it (``friction.blame``), and ``bash_kinds`` splits those by kind: ``usage_errors``, ``env_errors`` and
  ``other_errors`` (``friction.error_kind``); ``bash_compound`` counts the commands that ran several programs, and their
  failures that count for none, by kind;
- ``skills``: per skill loaded, through the Skill tool or typed by the person as ``/name`` (``calls``, ``typed`` of
  them, ``failed`` Skill calls), and in the windows from each load to the person's next turn the Bash commands
  (``bash``), their failures by kind, the person's interrupts and corrections (``friction.SkillTally``); a Codex
  session counts a skill only when the person named it, which its rollout marks; ``interrupts`` and ``turns`` are the
  session's (the turns of the person);
- ``user_turns``: what the person wrote, each cut to USER_TURN_CHARS, the first MAX_USER_TURNS of them; text that
  starts with ``<`` (command output, reminders) is not the person's;
- ``commands``: the Bash commands, each once, the most repeated first, and ``repeated_commands`` those run more than
  once with their count;
- ``errors``: the text of each failed tool result, cut to ERROR_CHARS, with its tool and how often it came;
- ``files_read``, how many files the Read tool read, and ``files_edited``, the files the edit tools wrote;
- ``peak_context_tokens``: the largest context one message of the model read, its ``input_tokens`` plus
  ``cache_creation_input_tokens`` plus ``cache_read_input_tokens``, each message counted once; None when no message
  says its usage;
- ``compactions``: how many times the session's context was compacted, the ``compact_boundary`` lines Claude Code
  writes (``{"type": "system", "subtype": "compact_boundary", "uuid": ..., "compactMetadata": {"trigger": "auto"}}``,
  ``trigger`` ``manual`` for /compact; read from real transcripts of Claude Code 2.1.246 to 2.1.293), each uuid
  once, since a resumed session may copy a line;
- ``tool_result_chars``: per tool, the characters of the text of its results the model read, each result once, the
  most first.

The last three leave out the lines of a subagent (``isSidechain``), whose context is its own, and the peak leaves out
the messages no model sent (``<synthetic>``).

The digest goes to the hub only through ``evo_agents.hub.redact`` (the hook does that), and the hub's PUT checks it
against the same limits. ``DigestState`` (~/.evo/hub/digest-state.json) remembers per hub and per session what was
pushed last and what waits for a later Stop. Standard library only: the hook runs on a core install.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.hub import friction
from evo_agents.hub.client import FILE_MODE, write_atomic

# The hub's checks of a digest (``server.digests``) and the tests read these here.
from evo_agents.hub.friction import MAX_NAME as MAX_NAME
from evo_agents.hub.friction import MAX_PROGRAMS as MAX_PROGRAMS
from evo_agents.hub.friction import MAX_SKILLS as MAX_SKILLS
from evo_agents.hub.friction import program as program

MIN_MESSAGES = 6  # a session with fewer is not digested (harness.py digest)
MAX_SESSION_ID = 100
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
MAX_CWD = 4096
MAX_USER_TURNS = 40
USER_TURN_CHARS = 600
MAX_COMMANDS = 60
COMMAND_CHARS = 400
MAX_REPEATED = 20
MAX_ERRORS = 20
ERROR_CHARS = 400
MAX_FILES = 40
PATH_CHARS = 1024
MAX_TOOLS = 200
MAX_MODELS = 20
MAX_COUNT = 10**9
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# The tokens of the context a message of the model read: what it sent, what it wrote to the cache and what it read
CONTEXT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
COMPACT_BOUNDARY = "compact_boundary"  # the subtype of the system line Claude Code writes where it compacted
TOOL_NAME = "gen_ai.tool.name"  # OpenTelemetry GenAI's attribute for the name of a tool
READ_TOOLS = frozenset({"Read"})
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash"})
SKILL_TOOL = "Skill"  # Claude Code's tool that loads a skill, named by its input's ``skill``
SYNTHETIC_MODEL = "<synthetic>"  # what Claude Code writes for a message no model sent
STATE_FILE = "digest-state.json"
STATE_VERSION = 1
KEEP_SECONDS = 90 * 86400  # a session the state remembers, after its last push
MAX_SESSIONS = 500  # sessions the state remembers per hub, the newest

# A Codex rollout (``codex exec`` / the app-server session log a Codex session's transcript_path names): one JSON object
# a line, ``type`` and ``payload``, the first line ``session_meta``. The digest is read through the normalized item
# stream (``event_msg`` of ``item_completed``), the token counts (``token_count``) and the compactions, so a Codex
# session's digest has the shape of a Claude Code one.
CODEX_META = "session_meta"  # the first line of a Codex rollout
CODEX_SHELL_TOOL = "shell"  # a Codex command execution in the tools tally (its programs are in ``bash``)
CODEX_PATCH_TOOL = "apply_patch"  # a Codex file change in the tools tally
CODEX_SEARCH_TOOL = "web_search"  # a Codex web search in the tools tally
CODEX_LC = ("-lc", "-c", "-ic")  # a login shell's flag before the command it runs
# A Codex ``total_token_usage`` key mapped to a digest usage key; ``input_tokens`` is handled apart (cached removed).
CODEX_USAGE = {
    "output_tokens": "output_tokens",
    "cached_input_tokens": "cache_read_input_tokens",
    "cache_write_input_tokens": "cache_creation_input_tokens",
}


def _cut(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# The transcript


def _role(line: dict) -> str:
    return line.get("type") or line.get("role") or ""


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


def _result_text(item: dict) -> str:
    content = item.get("content", "")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


def _result_chars(item: dict) -> int:
    """The characters of the text of a tool result; an image or another part without text counts none."""
    content = item.get("content", "")
    if isinstance(content, list):
        return sum(len(str(part.get("text") or "")) for part in content if isinstance(part, dict))
    return len(content) if isinstance(content, str) else 0


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def context_tokens(usage) -> int | None:
    """The tokens of the context one message of the model read, from its ``usage``: the sum of CONTEXT_KEYS, None
    when it counts none of them."""
    if not isinstance(usage, dict):
        return None
    counts = [found for found in (_count(usage.get(name)) for name in CONTEXT_KEYS) if found is not None]
    return min(sum(counts), MAX_COUNT) if counts else None


def _counted(line) -> bool:
    """A message of the person or the model, as harness.py counts them: meta lines and the rest left out."""
    return isinstance(line, dict) and not line.get("isMeta") and _role(line) in ("user", "assistant")


def read_lines(path: Path):
    """The JSON objects of a transcript, one per line; a line that is not JSON is skipped."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _ranked(counts: Counter) -> list:
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


class _Tally:
    """What a transcript holds, one line at a time."""

    def __init__(self):
        self.messages = 0
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.user_turns: list[str] = []
        self.commands: Counter = Counter()
        self.calls: Counter = Counter()
        self.failed: Counter = Counter()
        self.bash = friction.BashTally()
        self.skills = friction.SkillTally()
        self.errors: Counter = Counter()
        self.read: set[str] = set()
        self.edited: set[str] = set()
        self.tool_uses: dict[str, tuple[str, str | None]] = {}  # tool_use id: (tool, the command of a Bash call)
        self.models: dict[str, str] = {}  # message id: model
        self.usage: dict[str, dict] = {}  # message id: its usage; each line of a message holds the whole
        self.contexts: dict[str, int] = {}  # message id: the tokens of its context, the main thread's
        self.compactions: set[str] = set()  # the uuids of the compact boundaries
        self.result_chars: dict[str, tuple[str, int]] = {}  # tool_use id: (tool, characters of its result)

    def add(self, line) -> None:
        if isinstance(line, dict) and line.get("type") == "system" and line.get("subtype") == COMPACT_BOUNDARY:
            if not line.get("isSidechain"):
                self.compactions.add(str(line.get("uuid") or f"line {len(self.compactions)}"))
            return
        if not _counted(line):
            return
        self.messages += 1
        at = _timestamp(line.get("timestamp"))
        if at is not None:
            self.first = at if self.first is None or at < self.first else self.first
            self.last = at if self.last is None or at > self.last else self.last
        if _role(line) == "user":
            self._user(_items(line), main=not line.get("isSidechain"))
        else:
            self._assistant(line, _items(line))

    def _user(self, items: list[dict], main: bool = True) -> None:
        text = _text(items).strip()
        if text and not text.startswith("<") and len(self.user_turns) < MAX_USER_TURNS:
            self.user_turns.append(_cut(text, USER_TURN_CHARS))
        for item in items:
            if item.get("type") == "tool_result":
                self._result(item, main)
        if main:
            self.skills.person(text)

    def _result(self, item: dict, main: bool) -> None:
        call = str(item.get("tool_use_id"))
        tool, command = self.tool_uses.get(call, ("unknown", None))
        if main:
            self.result_chars[call] = (tool, _result_chars(item))
        if not item.get("is_error"):
            return
        self.failed[tool] += 1
        text = _result_text(item).strip()
        if command is not None:
            self.skills.bash(self.bash.fail(command, text))
        if tool == SKILL_TOOL:
            self.skills.load_failed(call)
        self.errors[(tool, _cut(text, ERROR_CHARS))] += 1

    def _assistant(self, line: dict, items: list[dict]) -> None:
        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        key = str(message.get("id") or line.get("uuid") or self.messages)
        model = message.get("model")
        if isinstance(model, str) and model and model != SYNTHETIC_MODEL:
            self.models[key] = _cut(model, MAX_NAME)
        if isinstance(message.get("usage"), dict):
            self.usage[key] = message["usage"]
        context = context_tokens(message.get("usage"))
        if context is not None and model != SYNTHETIC_MODEL and not line.get("isSidechain"):
            self.contexts[key] = context
        for item in items:
            if item.get("type") in ("tool_use", "server_tool_use"):
                self._tool_use(item)

    def _tool_use(self, item: dict) -> None:
        tool = _cut(item.get("name") or "unknown", MAX_NAME)
        given = item.get("input") if isinstance(item.get("input"), dict) else {}
        self.calls[tool] += 1
        command = None
        if tool in SHELL_TOOLS:
            command = str(given.get("command") or "") or None
            if command:
                self.commands[_cut(command, COMMAND_CHARS)] += 1
                self.bash.call(command)
                self.skills.bash()
        elif tool == SKILL_TOOL:
            self.skills.load(given.get("skill"), typed=False, call_id=str(item.get("id")))
        self.tool_uses[str(item.get("id"))] = (tool, command)
        path = given.get("file_path") or given.get("notebook_path")
        if isinstance(path, str) and path:
            if tool in READ_TOOLS:
                self.read.add(path)
            elif tool in EDIT_TOOLS:
                self.edited.add(_cut(path, PATH_CHARS))

    def digest(self, cwd: str) -> dict:
        totals = dict.fromkeys(USAGE_KEYS, 0)
        for counted in self.usage.values():
            for name in USAGE_KEYS:
                value = counted.get(name)
                if isinstance(value, int) and value >= 0:
                    totals[name] = min(totals[name] + value, MAX_COUNT)
        per_model = _ranked(Counter(self.models.values()))
        commands = _ranked(self.commands)
        chars: Counter = Counter()
        for tool, size in self.result_chars.values():
            chars[tool] = min(chars[tool] + size, MAX_COUNT)
        return {
            "cwd": _cut(cwd, MAX_CWD),
            "messages": min(self.messages, MAX_COUNT),
            "started_at": self.first.isoformat() if self.first else None,
            "ended_at": self.last.isoformat() if self.last else None,
            "model": per_model[0][0] if per_model else None,
            "models": [{"model": name, "messages": n} for name, n in per_model[:MAX_MODELS]],
            "usage": totals,
            "tools": [
                {TOOL_NAME: name, "calls": n, "errors": min(self.failed[name], n)}
                for name, n in _ranked(self.calls)[:MAX_TOOLS]
            ],
            "bash": self.bash.program_figures(),
            "user_turns": self.user_turns,
            "commands": [command for command, _ in commands[:MAX_COMMANDS]],
            "repeated_commands": [{"command": c, "n": n} for c, n in commands if n > 1][:MAX_REPEATED],
            "errors": [{TOOL_NAME: tool, "text": text, "n": n} for (tool, text), n in _ranked(self.errors) if text][
                :MAX_ERRORS
            ],
            "files_read": min(len(self.read), MAX_COUNT),
            "files_edited": sorted(self.edited)[:MAX_FILES],
            "peak_context_tokens": max(self.contexts.values(), default=None),
            "compactions": min(len(self.compactions), MAX_COUNT),
            "tool_result_chars": [{TOOL_NAME: name, "chars": n} for name, n in _ranked(chars)[:MAX_TOOLS]],
            **friction.digest_fields(self.bash, self.skills),
        }


# A Codex session


def is_codex(line) -> bool:
    """True for the first line of a Codex rollout, which a Codex session's transcript is: ``type`` is session_meta."""
    return isinstance(line, dict) and line.get("type") == CODEX_META


def _codex_text(content) -> str:
    """The text of a Codex item's content, a string or a list of ``{type, text}`` (the type in any case)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("text"))
    return ""


def codex_command(item: dict) -> str:
    """The shell command a CommandExecution ran: Codex's parsed form when it gives one, else the command a login shell
    ran after its ``-lc`` flag, else the argv joined."""
    parsed = item.get("parsed_cmd")
    if isinstance(parsed, list):
        cmds = [str(part["cmd"]) for part in parsed if isinstance(part, dict) and part.get("cmd")]
        if cmds:
            return " && ".join(cmds)
    command = item.get("command")
    if isinstance(command, list) and command:
        if len(command) >= 2 and command[-2] in CODEX_LC:
            return str(command[-1])
        return " ".join(str(word) for word in command)
    return str(command or "")


def _codex_failed(item: dict) -> bool:
    """A Codex item that did not succeed: a non-zero exit code, or a failed, declined or error status."""
    exit_code = item.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return True
    return str(item.get("status") or "") in ("failed", "declined", "error")


class _CodexTally:
    """What a Codex rollout holds, one line at a time. The same fields as ``_Tally``, built from the normalized item
    stream instead of a Claude Code transcript; usage comes from the running ``total_token_usage`` (cumulative, so the
    largest per key is the session's) and the peak from the largest ``last_token_usage.input_tokens`` (the context of
    one request, the cached part counted in it, as the Codex runtime measures a run's peak)."""

    def __init__(self):
        self.messages = 0
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.user_turns: list[str] = []
        self.commands: Counter = Counter()
        self.calls: Counter = Counter()
        self.failed: Counter = Counter()
        self.bash = friction.BashTally()
        self.skills = friction.SkillTally()
        self.errors: Counter = Counter()
        self.edited: set[str] = set()
        self.models: Counter = Counter()  # the models of the turns, as a Counter of names
        self.totals: dict = dict.fromkeys(USAGE_KEYS, 0)
        self.peak: int | None = None
        self.compactions: set[str] = set()
        self.chars: Counter = Counter()  # per tool, the characters of its results the model read

    def add(self, line) -> None:
        if not isinstance(line, dict):
            return
        at = _timestamp(line.get("timestamp"))
        if at is not None:
            self.first = at if self.first is None or at < self.first else self.first
            self.last = at if self.last is None or at > self.last else self.last
        kind = line.get("type")
        payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
        if kind == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                self.models[_cut(model, MAX_NAME)] += 1
        elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            named = friction.codex_skill(_codex_text(payload.get("content")))
            if named is not None:
                self.skills.load(named, typed=True)
        elif kind == "event_msg":
            inner = payload.get("type")
            if inner == "token_count":
                self._tokens(payload.get("info") or {})
            elif inner == "item_completed":
                self._item(payload.get("item") or {})
            elif inner == "turn_aborted" and payload.get("reason") == "interrupted":
                self.skills.interrupt()

    def _tokens(self, info: dict) -> None:
        last = info.get("last_token_usage")
        if isinstance(last, dict):
            value = _count(last.get("input_tokens"))
            if value:  # a request that read no context (an aborted or empty turn) does not set the peak, never 0
                self.peak = value if self.peak is None else max(self.peak, value)
        total = info.get("total_token_usage")
        if isinstance(total, dict):
            cached = _count(total.get("cached_input_tokens")) or 0
            fresh = max((_count(total.get("input_tokens")) or 0) - cached, 0)  # as Claude Code counts input apart
            mapped = {"input_tokens": fresh, **{dst: _count(total.get(src)) or 0 for src, dst in CODEX_USAGE.items()}}
            for key, found in mapped.items():
                self.totals[key] = min(max(self.totals[key], found), MAX_COUNT)

    def _item(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "UserMessage":
            self.messages += 1
            text = _codex_text(item.get("content")).strip()
            if text and not text.startswith("<"):
                self.skills.turn(text)
                if len(self.user_turns) < MAX_USER_TURNS:
                    self.user_turns.append(_cut(text, USER_TURN_CHARS))
        elif kind == "AgentMessage":
            self.messages += 1
        elif kind == "CommandExecution":
            self._command(item)
        elif kind == "McpToolCall":
            self._mcp(item)
        elif kind == "FileChange":
            self._file(item)
        elif kind == "WebSearch":
            self.messages += 1
            self.calls[CODEX_SEARCH_TOOL] += 1
        elif kind == "ContextCompaction":
            self.compactions.add(str(item.get("id") or f"item {len(self.compactions)}"))

    def _command(self, item: dict) -> None:
        self.messages += 1
        self.calls[CODEX_SHELL_TOOL] += 1
        failed = _codex_failed(item)
        command = codex_command(item).strip()
        output = str(item.get("aggregated_output") or "")
        if command:
            self.commands[_cut(command, COMMAND_CHARS)] += 1
            self.bash.call(command)
            self.skills.bash()
            if failed:
                code = item.get("exit_code") if isinstance(item.get("exit_code"), int) else None
                self.skills.bash(self.bash.fail(command, output, code))
        self.chars[CODEX_SHELL_TOOL] = min(self.chars[CODEX_SHELL_TOOL] + len(output), MAX_COUNT)
        if failed:
            self.failed[CODEX_SHELL_TOOL] += 1
            self.errors[(CODEX_SHELL_TOOL, _cut(output.strip(), ERROR_CHARS))] += 1

    def _mcp(self, item: dict) -> None:
        self.messages += 1
        server, name = item.get("server"), item.get("tool")
        tool = _cut(f"mcp__{server}__{name}" if server else f"mcp__{name}", MAX_NAME)
        self.calls[tool] += 1
        content = item.get("result")
        text = _codex_text(content.get("content")) if isinstance(content, dict) else _codex_text(content)
        self.chars[tool] = min(self.chars[tool] + len(text), MAX_COUNT)
        if _codex_failed(item):
            self.failed[tool] += 1
            self.errors[(tool, _cut(text.strip(), ERROR_CHARS))] += 1

    def _file(self, item: dict) -> None:
        self.messages += 1
        self.calls[CODEX_PATCH_TOOL] += 1
        changes = item.get("changes")
        if isinstance(changes, dict):
            for path in changes:
                if isinstance(path, str) and path:
                    self.edited.add(_cut(path, PATH_CHARS))
        if _codex_failed(item):
            self.failed[CODEX_PATCH_TOOL] += 1

    def digest(self, cwd: str) -> dict:
        per_model = _ranked(self.models)
        commands = _ranked(self.commands)
        return {
            "cwd": _cut(cwd, MAX_CWD),
            "messages": min(self.messages, MAX_COUNT),
            "started_at": self.first.isoformat() if self.first else None,
            "ended_at": self.last.isoformat() if self.last else None,
            "model": per_model[0][0] if per_model else None,
            "models": [{"model": name, "messages": n} for name, n in per_model[:MAX_MODELS]],
            "usage": dict(self.totals),
            "tools": [
                {TOOL_NAME: name, "calls": n, "errors": min(self.failed[name], n)}
                for name, n in _ranked(self.calls)[:MAX_TOOLS]
            ],
            "bash": self.bash.program_figures(),
            "user_turns": self.user_turns,
            "commands": [command for command, _ in commands[:MAX_COMMANDS]],
            "repeated_commands": [{"command": c, "n": n} for c, n in commands if n > 1][:MAX_REPEATED],
            "errors": [{TOOL_NAME: tool, "text": text, "n": n} for (tool, text), n in _ranked(self.errors) if text][
                :MAX_ERRORS
            ],
            "files_read": 0,  # Codex reads files through the shell, which the commands already hold
            "files_edited": sorted(self.edited)[:MAX_FILES],
            "peak_context_tokens": self.peak,
            "compactions": min(len(self.compactions), MAX_COUNT),
            "tool_result_chars": [{TOOL_NAME: name, "chars": n} for name, n in _ranked(self.chars)[:MAX_TOOLS]],
            **friction.digest_fields(self.bash, self.skills),
        }


def build(transcript: Path, cwd: str) -> dict | None:
    """The digest of the session whose transcript is ``transcript`` and whose directory is ``cwd``; None when it holds
    fewer than MIN_MESSAGES messages or cannot be read. The transcript is read a line at a time, as a Claude Code
    transcript or, when its first line is a Codex ``session_meta``, as a Codex rollout."""
    tally: _Tally | _CodexTally = _Tally()
    decided = False
    try:
        for line in read_lines(transcript):
            if not decided:
                decided = True
                if is_codex(line):
                    tally = _CodexTally()
            tally.add(line)
    except OSError:
        return None
    return tally.digest(cwd) if tally.messages >= MIN_MESSAGES else None


def fingerprint(digest: dict) -> str:
    """The sha256 of ``digest`` as JSON: the same digest, the same fingerprint."""
    return hashlib.sha256(json.dumps(digest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# What was pushed, per hub and session


class DigestState:
    """~/.evo/hub/digest-state.json: per hub and per session, its transcript and directory, the project its digest
    goes to (None for a directory of no project), the fingerprint of the digest pushed last, and whether a newer one
    waits for a later Stop. Advisory: a file that cannot be read counts as empty. Each change reads the file again and
    writes it whole, atomically, so two sessions ending at once lose at most a mark, and a digest is pushed again."""

    def __init__(self, directory: Path, hub_url: str):
        self.path = directory / STATE_FILE
        self.hub_url = hub_url

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": STATE_VERSION, "hubs": {}}
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION or not isinstance(data.get("hubs"), dict):
            return {"version": STATE_VERSION, "hubs": {}}
        return data

    def sessions(self) -> dict[str, dict]:
        held = self._read()["hubs"].get(self.hub_url)
        found = held.get("sessions") if isinstance(held, dict) else None
        return {key: entry for key, entry in (found or {}).items() if isinstance(entry, dict)}

    def get(self, session_id: str) -> dict | None:
        return self.sessions().get(session_id)

    def put(self, session_id: str, entry: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        data = self._read()
        held = data["hubs"].setdefault(self.hub_url, {})
        if not isinstance(held, dict):
            held = data["hubs"][self.hub_url] = {}
        sessions = held.get("sessions") if isinstance(held.get("sessions"), dict) else {}
        sessions[session_id] = {**entry, "at": now}
        kept = sorted(
            (item for item in sessions.items() if isinstance(item[1], dict)),
            key=lambda item: item[1].get("at") or 0,
            reverse=True,
        )
        held["sessions"] = {
            key: value for key, value in kept[:MAX_SESSIONS] if now - (value.get("at") or 0) < KEEP_SECONDS
        }
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(self.path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(), FILE_MODE)
        except OSError:
            pass  # only ever an optimization: the next Stop builds and pushes again

    def waiting(self, besides: str, limit: int) -> list[tuple[str, dict]]:
        """Up to ``limit`` sessions other than ``besides`` whose digest waits for a push, the oldest first."""
        found = [
            (key, entry)
            for key, entry in self.sessions().items()
            if key != besides and entry.get("pending") and isinstance(entry.get("transcript"), str)
        ]
        return sorted(found, key=lambda item: item[1].get("at") or 0)[:limit]
