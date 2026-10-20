"""What the model of the night's history run reads of a session or a run (``evo_agents.hub.history``): an excerpt of
its transcript, cut around its errors and the person's corrections rather than handed whole, since a model reading a
long trace finds its faults badly (TRAIL, Deshpande et al. 2025). Pure functions over JSON values, standard library
only.

A transcript is read one line at a time into entries (``Entry``): what the person wrote, what the agent said, each tool
call and its result, and the errors of the model's API, each with where it came (``L<line>`` of the session's own
transcript file, ``#<seq>`` of a run's event) and its text cut to ENTRY_CHARS. A tool result that failed and an error of
the API are anchors, and so is a turn of the person that reads as a correction (``review.is_correction``). A Claude Code
transcript is read through its user and assistant lines, a Codex rollout through its items, a run through its events.

``Cutter`` keeps an excerpt within EXCERPT_CHARS while the lines go by, holding no more than it may show: the whole
transcript while it fits; else its first HEAD entries, each anchor with WINDOW entries before it and after it, the
earliest anchors first, and its last TAIL entries, with ``[... n lines left out ...]`` where entries were left out.

The same readers give the entries of each line with a longer text (``chars``: its start and its end, where a failed
command says why), for the words the hub indexes for search and the lines a reader asks for around one
(``evo_agents.hub.history_index``, ``evo_agents.hub.server.history_search``).
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from evo_agents.hub import digest, review

EXCERPT_CHARS = 16_000  # of one item's excerpt
ENTRY_CHARS = 400  # of one entry's text
ANCHOR_CHARS = 1_000  # of an anchor's text
HEAD = 3  # entries at the start: the task
TAIL = 3  # entries at the end: how it ended
WINDOW = 2  # entries before and after an anchor
MAX_KEPT = 400  # entries a cut excerpt holds while it reads, at most
MAIN = "main"  # the session's own transcript file (evo_agents.hub.transcripts.MAIN)
INPUT_KEYS = ("command", "file_path", "path", "pattern", "query", "url", "description", "prompt", "skill")


@dataclass(frozen=True)
class Entry:
    """One thing of a transcript: where it came, who (user, agent, call, result, error), its text, and whether it is
    an anchor (an error or a correction)."""

    where: str
    role: str
    text: str
    anchor: bool = False

    def render(self) -> str:
        return f"{self.where} {self.role}: {self.text}"


def _cut(text, limit: int, ends: bool = False) -> str:
    """``text`` on one line within ``limit``: its start, and with ``ends`` its start and its end."""
    line = " ".join(str(text or "").split())
    if len(line) <= limit:
        return line
    if ends:
        tail = limit // 4
        return line[: limit - tail - 5].rstrip() + " ... " + line[-tail:].lstrip()
    return line[: limit - 3].rstrip() + "..."


def entry(where: str, role: str, text, *, anchor: bool = False, chars: int | None = None) -> Entry | None:
    """An entry, its text within ENTRY_CHARS (ANCHOR_CHARS for an anchor), or its start and end within ``chars``."""
    text = _cut(text, chars, ends=True) if chars else _cut(text, ANCHOR_CHARS if anchor else ENTRY_CHARS)
    return Entry(where, role, text, anchor) if text else None


def _brief(given) -> str:
    """What a tool call was given, in a line: its command, path, pattern or query, else its input as JSON."""
    if isinstance(given, str):
        return given
    if not isinstance(given, Mapping):
        return ""
    for key in INPUT_KEYS:
        if isinstance(given.get(key), str) and given[key].strip():
            return given[key]
    return json.dumps(given, ensure_ascii=False, sort_keys=True, default=str)


def _texts(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, Mapping))
    return ""


class Cutter:
    """The excerpt of one transcript, its entries added one at a time (``add``), within EXCERPT_CHARS."""

    def __init__(self, limit: int = EXCERPT_CHARS):
        self.limit = limit
        self.count = 0  # entries seen
        self.anchors = 0
        self.whole: list[tuple[int, Entry]] | None = []  # every entry while they fit
        self.whole_chars = 0
        self.head: list[tuple[int, Entry]] = []
        self.before: deque = deque(maxlen=WINDOW)
        self.tail: deque = deque(maxlen=TAIL)
        self.kept: dict[int, Entry] = {}  # the anchors and their windows
        self.after = 0  # entries still to keep after the last anchor
        self.kept_anchors = 0

    def add(self, item: Entry | None) -> None:
        if item is None:
            return
        index = self.count
        self.count += 1
        self.anchors += item.anchor
        if self.whole is not None:
            self.whole.append((index, item))
            self.whole_chars += len(item.render()) + 1
            if self.whole_chars > self.limit:
                self.whole = None
        if len(self.head) < HEAD:
            self.head.append((index, item))
        elif item.anchor and len(self.kept) < MAX_KEPT:
            self.kept.update(self.before)
            self.kept[index] = item
            self.kept_anchors += 1
            self.after = WINDOW
        elif self.after and len(self.kept) < MAX_KEPT:
            self.kept[index] = item
            self.after -= 1
        self.before.append((index, item))
        self.tail.append((index, item))

    @property
    def cut(self) -> bool:
        return self.whole is None

    def text(self) -> str:
        """The excerpt: every entry while they fit, else the head, the windows of the anchors that fit, the earliest
        first, and the tail."""
        if self.whole is not None:
            return "\n".join(item.render() for _, item in self.whole)
        fixed = dict(self.head) | dict(self.tail)
        used = sum(len(item.render()) + 1 for item in fixed.values())
        windows: list[int] = []
        for index, item in sorted(self.kept.items()):
            if index in fixed:
                continue
            size = len(item.render()) + 1
            if used + size > self.limit:
                break
            windows.append(index)
            used += size
        while True:  # the marks of the cuts take room too: the latest windows give it back
            text = _rendered(fixed | {index: self.kept[index] for index in windows})
            if len(text) <= self.limit or not windows:
                return text
            windows.pop()


def _rendered(chosen: dict[int, Entry]) -> str:
    lines, last = [], -1
    for index in sorted(chosen):
        if index > last + 1:
            lines.append(f"[... {index - last - 1} lines left out ...]")
        lines.append(chosen[index].render())
        last = index
    return "\n".join(lines)


# A Claude Code transcript


class ClaudeEntries:
    """The entries of the lines of a Claude Code session's transcript file, each text within ``chars`` when given."""

    def __init__(self, chars: int | None = None, *, sidechain: bool = False):
        self.tools: dict[str, str] = {}  # the id of a call: its tool
        self.chars = chars
        self.sidechain = sidechain  # read the lines of a subagent too (its file is a sidechain throughout)

    def read(self, number: int, line) -> list[Entry | None]:
        if not isinstance(line, dict) or line.get("isMeta") or (line.get("isSidechain") and not self.sidechain):
            return []
        kind, where = line.get("type"), f"L{number}"
        if kind == "system" and line.get("subtype") == "api_error":
            error = line.get("error") if isinstance(line.get("error"), dict) else {}
            text = error.get("formatted") or error.get("message")
            return [entry(where, "api error", text, anchor=True, chars=self.chars)]
        if kind == "system" and line.get("subtype") == digest.COMPACT_BOUNDARY:
            return [entry(where, "system", "the context was compacted")]
        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        content = message.get("content")
        items = [{"type": "text", "text": content}] if isinstance(content, str) else content
        items = [item for item in items or [] if isinstance(item, dict)]
        if kind == "user" and not line.get("isCompactSummary"):
            return self._user(items, where)
        if kind == "assistant":
            return self._assistant(items, where)
        return []

    def _user(self, items: list[dict], where: str) -> list[Entry | None]:
        found = []
        for item in items:
            if item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                if text and not text.startswith("<"):
                    found.append(entry(where, "user", text, anchor=review.is_correction(text), chars=self.chars))
            elif item.get("type") == "tool_result":
                tool = self.tools.get(str(item.get("tool_use_id")), "tool")
                failed = bool(item.get("is_error"))
                role = f"{tool} failed" if failed else f"{tool} result"
                found.append(entry(where, role, _texts(item.get("content")), anchor=failed, chars=self.chars))
        return found

    def _assistant(self, items: list[dict], where: str) -> list[Entry | None]:
        found = []
        for item in items:
            if item.get("type") == "text":
                found.append(entry(where, "agent", item.get("text"), chars=self.chars))
            elif item.get("type") in ("tool_use", "server_tool_use"):
                tool = str(item.get("name") or "tool")
                self.tools[str(item.get("id"))] = tool
                found.append(entry(where, f"call {tool}", _brief(item.get("input")) or "-", chars=self.chars))
        return found


# A Codex rollout


def _codex_failed(item: dict) -> bool:
    code = item.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return True
    return str(item.get("status") or "") in ("failed", "declined", "error")


def codex_entries(number: int, line, chars: int | None = None) -> list[Entry | None]:
    """The entries of one line of a Codex rollout, each text within ``chars`` when given."""
    if not isinstance(line, dict) or line.get("type") != "event_msg" or not isinstance(line.get("payload"), dict):
        return []
    payload = line["payload"]
    if payload.get("type") != "item_completed":
        return []
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    kind, where = item.get("type"), f"L{number}"
    if kind == "UserMessage":
        text = _texts(item.get("content")).strip()
        return [entry(where, "user", text, anchor=review.is_correction(text), chars=chars)]
    if kind == "AgentMessage":
        return [entry(where, "agent", _texts(item.get("content")), chars=chars)]
    failed = _codex_failed(item)
    if kind == "CommandExecution":
        output = str(item.get("aggregated_output") or "")
        return [
            entry(where, "call shell", digest.codex_command(item), chars=chars),
            entry(where, "shell failed" if failed else "shell result", output, anchor=failed, chars=chars),
        ]
    if kind == "McpToolCall":
        result = item.get("result")
        text = _texts(result.get("content")) if isinstance(result, dict) else _texts(result)
        tool = f"{item.get('server')}/{item.get('tool')}"
        return [
            entry(where, f"call {tool}", _brief(item.get("arguments")) or "-", chars=chars),
            entry(where, f"{tool} failed" if failed else f"{tool} result", text, anchor=failed, chars=chars),
        ]
    if kind == "FileChange":
        paths = ", ".join(path for path in item.get("changes") or {} if isinstance(path, str))
        role = "call apply_patch failed" if failed else "call apply_patch"
        return [entry(where, role, paths or "-", anchor=failed, chars=chars)]
    return []


def session_excerpt(lines: Iterable[tuple[int, object]], limit: int = EXCERPT_CHARS) -> Cutter:
    """The excerpt of a session's own transcript file, its lines as (number, JSON value): a Codex rollout when its
    first line says so, else a Claude Code transcript."""
    cutter, claude, codex = Cutter(limit), ClaudeEntries(), None
    for number, line in lines:
        if codex is None:
            codex = digest.is_codex(line)
        for item in codex_entries(number, line) if codex else claude.read(number, line):
            cutter.add(item)
    return cutter


# A run


def _event_text(body: dict) -> str:
    parts = []
    for item in body.get("content") or []:
        inner = item.get("content") if isinstance(item, dict) else None
        if isinstance(inner, dict) and isinstance(inner.get("text"), str):
            parts.append(inner["text"])
    return "\n".join(parts) if parts else _brief(body.get("rawOutput"))


class RunEntries:
    """The entries of a run's events, in seq order (each ``{seq, at, kind, body}``), into a Cutter (``add``, as
    ``facet_readers.RunFacet`` takes them), or into ``cutter``, anything with ``add``, each text within ``chars``
    when given."""

    def __init__(self, limit: int = EXCERPT_CHARS, *, chars: int | None = None, cutter=None):
        self.cutter = Cutter(limit) if cutter is None else cutter
        self.chars = chars
        self.chunks: list[str] = []
        self.chunk_at = ""
        self.ended: set[str] = set()

    def _entry(self, where: str, role: str, text, *, anchor: bool = False) -> Entry | None:
        return entry(where, role, text, anchor=anchor, chars=self.chars)

    def _flush(self) -> None:
        if self.chunks:
            self.cutter.add(self._entry(self.chunk_at, "agent", "".join(self.chunks)))
            self.chunks = []

    def add(self, event) -> None:
        if not isinstance(event, dict):
            return
        body = event.get("body") if isinstance(event.get("body"), dict) else {}
        kind, where = event.get("kind"), f"#{event.get('seq')}"
        if kind == "agent_message_chunk":
            content = body.get("content") if isinstance(body.get("content"), dict) else {}
            if not self.chunks:
                self.chunk_at = where
            self.chunks.append(str(content.get("text") or ""))
            return
        self._flush()
        if kind == "user_message":
            text = str(body.get("text") or "")
            self.cutter.add(self._entry(where, "user", text, anchor=review.is_correction(text)))
        elif kind == "tool_call" and body.get("toolCallId"):
            title = body.get("title") or body.get("kind") or "tool"
            self.cutter.add(self._entry(where, f"call {_cut(title, 80)}", _brief(body.get("rawInput")) or "-"))
        elif kind == "tool_call_update" and body.get("status") in ("completed", "failed"):
            call = str(body.get("toolCallId") or "")
            if call and call not in self.ended:
                self.ended.add(call)
                failed = body.get("status") == "failed"
                role = "tool failed" if failed else "tool result"
                self.cutter.add(self._entry(where, role, _event_text(body), anchor=failed))
        elif kind == "system" and isinstance(body.get("text"), str):
            self.cutter.add(self._entry(where, "worker", body["text"], anchor=bool(body.get("cause"))))

    def close(self) -> Cutter:
        self._flush()
        return self.cutter
