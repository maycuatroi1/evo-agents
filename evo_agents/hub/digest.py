"""The digest of a Claude Code session: what the Stop hook of the evo-hub plugin tells the hub about a session, built
on this machine from the session's transcript.

A session counts once its transcript holds MIN_MESSAGES messages: lines from the person or the model, meta lines left
out, as the ``digest`` of the harness-engineering skill counts them (``harness.py``), so the two agree on which
sessions have a digest. The digest keeps that skill's fields and adds the figures of each tool:

- ``cwd``, ``messages``, ``started_at`` and ``ended_at`` (the first and last of those lines), ``model`` (the one most
  of the model's messages name) and ``models``, and ``usage``, the tokens of every message of the model counted once:
  ``input_tokens``, ``output_tokens``, ``cache_creation_input_tokens`` and ``cache_read_input_tokens``;
- ``tools``: per tool, under ``gen_ai.tool.name`` as OpenTelemetry's GenAI conventions name it, how many calls and how
  many of them failed (a tool result marked as an error);
- ``bash``: the same per program of the Bash calls (``program``: the command's first word that is not ``cd`` or a
  variable, with the subcommand of the tools that have one, ``git push`` or ``python -m pytest``);
- ``user_turns``: what the person wrote, each cut to USER_TURN_CHARS, the first MAX_USER_TURNS of them; text that
  starts with ``<`` (command output, reminders) is not the person's;
- ``commands``: the Bash commands, each once, the most repeated first, and ``repeated_commands`` those run more than
  once with their count;
- ``errors``: the text of each failed tool result, cut to ERROR_CHARS, with its tool and how often it came;
- ``files_read``, how many files the Read tool read, and ``files_edited``, the files the edit tools wrote.

The digest goes to the hub only through ``evo_agents.hub.redact`` (the hook does that), and the hub's PUT checks it
against the same limits. ``DigestState`` (~/.evo/hub/digest-state.json) remembers per hub and per session what was
pushed last and what waits for a later Stop. Standard library only: the hook runs on a core install.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.hub.client import FILE_MODE, write_atomic

MIN_MESSAGES = 6  # a session with fewer is not digested (harness.py digest)
MAX_SESSION_ID = 100
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
MAX_NAME = 200  # characters of a tool, program or model name
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
MAX_PROGRAMS = 100
MAX_MODELS = 20
MAX_COUNT = 10**9
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
TOOL_NAME = "gen_ai.tool.name"  # OpenTelemetry GenAI's attribute for the name of a tool
READ_TOOLS = frozenset({"Read"})
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash"})
SYNTHETIC_MODEL = "<synthetic>"  # what Claude Code writes for a message no model sent
# Programs whose first argument names what they do.
SUBCOMMANDS = frozenset(
    {"git", "gh", "docker", "kubectl", "npm", "pnpm", "yarn", "uv", "pip", "cargo", "go", "make", "evo-agents", "evo"}
)
VALUED_OPTIONS = {"git": ("-C", "-c", "--git-dir", "--work-tree"), "docker": ("--context", "-H")}
SKIPPED_PROGRAMS = frozenset({"cd", "pushd", "export", "source", ".", "set", "time", "env", "sudo", "nohup"})
STATE_FILE = "digest-state.json"
STATE_VERSION = 1
KEEP_SECONDS = 90 * 86400  # a session the state remembers, after its last push
MAX_SESSIONS = 500  # sessions the state remembers per hub, the newest


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


def program(command: str) -> str | None:
    """The program a Bash command runs: the first word that is not ``cd``, a variable or a wrapper, with the subcommand
    of the programs that take one; None for a command without one."""
    for part in re.split(r"&&|\|\||;|\||\n", command):
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            words = part.split()
        while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or words[0] in SKIPPED_PROGRAMS):
            skipped = words.pop(0)
            if skipped in ("cd", "pushd", "source", "."):
                words = []  # its argument is a directory or a file, not a program
        if not words:
            continue
        name = os.path.basename(words[0]) or words[0]
        given = words[1:]
        while given and given[0] in VALUED_OPTIONS.get(name, ()):
            given = given[2:]  # an option and its value, before the subcommand
        rest = [word for word in given if not word.startswith("-")]
        if name.startswith("python") and len(words) > 2 and words[1] == "-m":
            return _cut(f"{name} -m {words[2]}", MAX_NAME)
        if name in SUBCOMMANDS and rest:
            return _cut(f"{name} {rest[0]}", MAX_NAME)
        return _cut(name, MAX_NAME)
    return None


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
        self.bash_calls: Counter = Counter()
        self.bash_failed: Counter = Counter()
        self.errors: Counter = Counter()
        self.read: set[str] = set()
        self.edited: set[str] = set()
        self.tool_uses: dict[str, tuple[str, str | None]] = {}  # tool_use id: (tool, program of a Bash call)
        self.models: dict[str, str] = {}  # message id: model
        self.usage: dict[str, dict] = {}  # message id: its usage; each line of a message holds the whole

    def add(self, line) -> None:
        if not _counted(line):
            return
        self.messages += 1
        at = _timestamp(line.get("timestamp"))
        if at is not None:
            self.first = at if self.first is None or at < self.first else self.first
            self.last = at if self.last is None or at > self.last else self.last
        if _role(line) == "user":
            self._user(_items(line))
        else:
            self._assistant(line, _items(line))

    def _user(self, items: list[dict]) -> None:
        text = _text(items).strip()
        if text and not text.startswith("<") and len(self.user_turns) < MAX_USER_TURNS:
            self.user_turns.append(_cut(text, USER_TURN_CHARS))
        for item in items:
            if item.get("type") != "tool_result" or not item.get("is_error"):
                continue
            tool, ran = self.tool_uses.get(str(item.get("tool_use_id")), ("unknown", None))
            self.failed[tool] += 1
            if ran is not None:
                self.bash_failed[ran] += 1
            self.errors[(tool, _cut(_result_text(item).strip(), ERROR_CHARS))] += 1

    def _assistant(self, line: dict, items: list[dict]) -> None:
        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        key = str(message.get("id") or line.get("uuid") or self.messages)
        model = message.get("model")
        if isinstance(model, str) and model and model != SYNTHETIC_MODEL:
            self.models[key] = _cut(model, MAX_NAME)
        if isinstance(message.get("usage"), dict):
            self.usage[key] = message["usage"]
        for item in items:
            if item.get("type") in ("tool_use", "server_tool_use"):
                self._tool_use(item)

    def _tool_use(self, item: dict) -> None:
        tool = _cut(item.get("name") or "unknown", MAX_NAME)
        given = item.get("input") if isinstance(item.get("input"), dict) else {}
        self.calls[tool] += 1
        ran = None
        if tool in SHELL_TOOLS:
            command = str(given.get("command") or "")
            if command:
                self.commands[_cut(command, COMMAND_CHARS)] += 1
                ran = program(command) or "unknown"
                self.bash_calls[ran] += 1
        self.tool_uses[str(item.get("id"))] = (tool, ran)
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
            "bash": [
                {"program": name, "calls": n, "errors": min(self.bash_failed[name], n)}
                for name, n in _ranked(self.bash_calls)[:MAX_PROGRAMS]
            ],
            "user_turns": self.user_turns,
            "commands": [command for command, _ in commands[:MAX_COMMANDS]],
            "repeated_commands": [{"command": c, "n": n} for c, n in commands if n > 1][:MAX_REPEATED],
            "errors": [{TOOL_NAME: tool, "text": text, "n": n} for (tool, text), n in _ranked(self.errors) if text][
                :MAX_ERRORS
            ],
            "files_read": min(len(self.read), MAX_COUNT),
            "files_edited": sorted(self.edited)[:MAX_FILES],
        }


def build(transcript: Path, cwd: str) -> dict | None:
    """The digest of the session whose transcript is ``transcript`` and whose directory is ``cwd``; None when it holds
    fewer than MIN_MESSAGES messages or cannot be read. The transcript is read a line at a time."""
    tally = _Tally()
    try:
        for line in read_lines(transcript):
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
