"""What a local session spent, from Claude Code's transcript, for the run the hub keeps of it (``local_sync``).

Each hook that reads the agent's words reads the transcript's new lines for usage too, from where the last one stopped
(``rec["usage_at"]``, a byte offset for the session's transcript and for each of its subagents' under
``<session id>/subagents/``), from the start of each file: the session's run counts all it spent. It sends:

- for the API calls of the lines read, one ``usage_update`` with each model's tokens added up (``scope`` delta): an
  assistant message is written once a content block with the same usage, so each message id counts once, the last
  one counted kept across hooks (``rec["usage_last"]``); a message of no model (``<synthetic>``) counts nothing;
- for each ``cost-state`` line of the session's transcript, Claude Code's own count of the session so far, one
  ``usage_update`` of its ``totalCostUSD`` and ``modelUsage`` (``scope`` total), which the hub takes as reported.

The hub (``evo_agents.hub.run_spend``) prices the tokens no ``cost-state`` covers yet at list prices. At most
MAX_READ bytes of each file a hook. Standard library only.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from evo_agents.hub import digest

MAX_READ = 1 << 20  # bytes of one file a hook reads
MAX_SUBAGENTS = 50  # subagent transcripts read a hook, the latest first
TOKEN_KEYS = {  # a Claude usage's keys, as modelUsage names them
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "cache_creation_input_tokens": "cacheCreationInputTokens",
}


def _number(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _files(transcript: Path) -> list[Path]:
    """The session's subagents' transcripts (``<session id>/subagents/agent-*.jsonl``, the latest first), then its
    own: a ``cost-state`` of the session's holds the calls of its subagents before it."""
    found = []
    with contextlib.suppress(OSError):
        found = sorted(
            (transcript.with_suffix("") / "subagents").glob("agent-*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:MAX_SUBAGENTS]
    return [*found, transcript]


def _new_lines(path: Path, offset: int) -> tuple[list[bytes], int]:
    """The whole lines of ``path`` after ``offset``, at most MAX_READ bytes, and the offset after them."""
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(MAX_READ)
    except OSError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    return data[: end + 1].splitlines(), offset + end + 1


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _add(models: dict, model: str, usage: dict) -> None:
    figures = models.setdefault(model, {})
    for key, name in TOKEN_KEYS.items():
        figures[name] = figures.get(name, 0) + _number(usage.get(key))
    written, details = _dict(usage.get("cache_creation")), _dict(usage.get("output_tokens_details"))
    hour = _number(written.get("ephemeral_1h_input_tokens"))
    figures["cacheCreation1hInputTokens"] = figures.get("cacheCreation1hInputTokens", 0) + hour
    figures["thinkingTokens"] = figures.get("thinkingTokens", 0) + _number(details.get("thinking_tokens"))


def _cost_state(item: dict, session_id: str) -> dict | None:
    models = item.get("modelUsage")
    cost = item.get("totalCostUSD")
    if not isinstance(models, dict) or not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
        return None
    return {"source": "local", "scope": "total", "session_id": session_id, "cost": cost, "models": models}


def usage_events(rec: dict, transcript: Path, session_id: str) -> list[tuple[str, dict]]:
    """The ``usage_update`` events of what the session spent since the last hook (see the module), moving the offsets
    of ``rec`` on."""
    offsets, last = _dict(rec.get("usage_at")), _dict(rec.get("usage_last"))
    events: list[tuple[str, dict]] = []
    models: dict = {}
    for path in _files(transcript):
        key = "main" if path == transcript else path.name
        lines, offsets[key] = _new_lines(path, int(offsets.get(key) or 0))
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") == "cost-state" and key == "main":
                state = _cost_state(item, session_id)
                if state is not None:
                    if models:  # the calls before it, then Claude Code's count, which holds them
                        events.append(("usage_update", _delta(models, session_id)))
                        models = {}
                    events.append(("usage_update", state))
                continue
            message = item.get("message") if item.get("type") == "assistant" else None
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            model, message_id = message.get("model"), message.get("id")
            if not isinstance(model, str) or not model or model == digest.SYNTHETIC_MODEL:
                continue
            if message_id is not None and message_id == last.get(key):
                continue  # another content block of a message counted already
            last[key] = message_id
            _add(models, model, message["usage"])
    if models:
        events.append(("usage_update", _delta(models, session_id)))
    rec["usage_at"], rec["usage_last"] = offsets, last
    return events


def _delta(models: dict, session_id: str) -> dict:
    kept = {name: {key: value for key, value in figures.items() if value} for name, figures in models.items()}
    return {"source": "local", "scope": "delta", "session_id": session_id, "models": kept}
