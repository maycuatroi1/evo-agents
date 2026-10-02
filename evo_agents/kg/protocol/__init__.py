"""The ``kg/1`` connector protocol.

A connector writes one JSON object per line on stdout (stderr is for logs only), or, in process,
yields the same objects from a generator. Every run starts with ``hello`` and ends with ``closed``.

Fields beyond the envelope other protocols already have: a revision and content hash on every item,
explicit tombstones, a ``complete`` flag on scoped listings, and deterministic IDs of the form
``<source_id>:<kind>:<native_key>``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from evo_agents.schema import Issue, validate

PROTOCOL = "kg/1"
MAJOR = 1

ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]*:[a-z0-9_.-]+:.+$"
TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
CAPABILITIES = ("list_complete", "tombstones", "rev_exact", "history", "conditional_write")

_LABEL = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "level": {"type": "string"},
        "location": {"type": "string"},
        "integrity": {"enum": ["T", "U"]},
    },
}

SCHEMAS: dict[str, dict] = {
    "hello": {
        "type": "object",
        "required": ["type", "protocol", "connector", "version"],
        "additionalProperties": False,
        "properties": {
            "type": {"const": "hello"},
            "protocol": {"type": "string", "pattern": r"^kg/\d+$"},
            "connector": {"type": "string", "minLength": 1},
            "version": {"type": "string", "minLength": 1},
            "capabilities": {
                "type": "object",
                "additionalProperties": False,
                "properties": {name: {"type": "boolean"} for name in CAPABILITIES},
            },
        },
    },
    "item": {
        "type": "object",
        "required": ["type", "id", "kind", "rev", "rev_time", "hash"],
        "additionalProperties": False,
        "properties": {
            "type": {"const": "item"},
            "id": {"type": "string", "pattern": ID_PATTERN},
            "kind": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]*$"},
            "rev": {"type": "string", "minLength": 1},
            "rev_time": {"type": "string", "pattern": TIME_PATTERN},
            "rev_exact": {"type": "boolean"},
            "hash": {"type": "string", "pattern": HASH_PATTERN},
            "uri": {"type": "string"},
            "parent": {"type": "string", "pattern": ID_PATTERN},
            "title": {"type": "string"},
            "body": {
                "type": "object",
                "required": ["format", "text"],
                "additionalProperties": False,
                "properties": {
                    "format": {"enum": ["markdown", "text", "yaml", "json", "code", "html"]},
                    "text": {"type": "string"},
                    "language": {"type": "string"},
                },
            },
            "fragments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["anchor", "text", "hash"],
                    "additionalProperties": False,
                    "properties": {
                        "anchor": {"type": "string", "minLength": 1},
                        "title": {"type": "string"},
                        "level": {"type": "integer", "minimum": 0},
                        "parent": {"type": "string"},
                        "span": {"type": "string"},
                        "text": {"type": "string"},
                        "hash": {"type": "string", "pattern": HASH_PATTERN},
                    },
                },
            },
            "props": {"type": "object"},
            "label": _LABEL,
        },
    },
    "tombstone": {
        "type": "object",
        "required": ["type", "id"],
        "additionalProperties": False,
        "properties": {
            "type": {"const": "tombstone"},
            "id": {"type": "string", "pattern": ID_PATTERN},
            "rev": {"type": "string"},
            "rev_time": {"type": "string", "pattern": TIME_PATTERN},
        },
    },
    "listing": {
        "type": "object",
        "required": ["type", "scope", "complete", "count"],
        "additionalProperties": False,
        "properties": {
            "type": {"const": "listing"},
            "scope": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_.-]*:"},
            "complete": {"type": "boolean"},
            "count": {"type": "integer", "minimum": 0},
        },
    },
    "state": {
        "type": "object",
        "required": ["type", "cursor"],
        "additionalProperties": False,
        "properties": {"type": {"const": "state"}, "cursor": {}},
    },
    "error": {
        "type": "object",
        "required": ["type", "failure", "message"],
        "additionalProperties": False,
        "properties": {
            "type": {"const": "error"},
            "failure": {"enum": ["config", "transient", "system"]},
            "id": {"type": "string", "pattern": ID_PATTERN},
            "message": {"type": "string"},
        },
    },
    "log": {
        "type": "object",
        "required": ["type", "level", "message"],
        "additionalProperties": False,
        "properties": {
            "type": {"const": "log"},
            "level": {"enum": ["debug", "info", "warning", "error"]},
            "message": {"type": "string"},
        },
    },
    "closed": {
        "type": "object",
        "required": ["type", "status"],
        "additionalProperties": False,
        "properties": {
            "type": {"const": "closed"},
            "status": {"enum": ["ok", "error"]},
            "exception": {"type": "string"},
        },
    },
}


# ---------------------------------------------------------------------------------------------
# Hashing


def canonical_json(value) -> bytes:
    """The byte form every hash in kg/1 is computed over. Connectors in other languages must match it:
    keys sorted, no insignificant whitespace, UTF-8 without escaping non-ASCII characters."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fragment_hash(fragment: dict) -> str:
    content = {k: fragment[k] for k in ("anchor", "title", "level", "parent", "text") if k in fragment}
    return sha256(canonical_json(content))


def item_hash(item: dict) -> str:
    """Hash of everything an item says about its content. Spans and nested hashes are excluded: they
    describe where content sits, not what it is."""
    content = {k: item[k] for k in ("kind", "title", "uri", "parent", "body", "props") if k in item}
    if "fragments" in item:
        content["fragments"] = [
            {k: f[k] for k in ("anchor", "title", "level", "parent", "text") if k in f} for f in item["fragments"]
        ]
    return sha256(canonical_json(content))


def finalize_item(item: dict) -> dict:
    """Fill in fragment and item hashes. Connectors call this right before emitting an item."""
    out = dict(item)
    out["type"] = "item"
    if "fragments" in out:
        out["fragments"] = [{**f, "hash": fragment_hash(f)} for f in out["fragments"]]
    out["hash"] = item_hash(out)
    return out


def hello(connector: str, version: str, **capabilities: bool) -> dict:
    unknown = set(capabilities) - set(CAPABILITIES)
    if unknown:
        raise ValueError(f"unknown capabilities: {sorted(unknown)}")
    return {
        "type": "hello",
        "protocol": PROTOCOL,
        "connector": connector,
        "version": version,
        "capabilities": capabilities,
    }


def source_of(item_id: str) -> str:
    return item_id.split(":", 1)[0]


# ---------------------------------------------------------------------------------------------
# Stream checking


def validate_message(message) -> list[Issue]:
    if not isinstance(message, dict):
        return [Issue("", "message is not a JSON object")]
    kind = message.get("type")
    schema = SCHEMAS.get(kind)
    if schema is None:
        return [Issue("type", f"unknown message type {kind!r}")]
    return validate(message, schema)


@dataclass
class StreamChecker:
    """Checks the order and consistency of one run's messages, beyond each message's own schema."""

    source_id: str | None = None
    issues: list[Issue] = field(default_factory=list)
    hello: dict | None = None
    closed: dict | None = None
    seen: dict[str, str] = field(default_factory=dict)  # id -> "item" | "tombstone"
    error_ids: set[str] = field(default_factory=set)
    listings: list[dict] = field(default_factory=list)
    position: int = 0
    refused: bool = False

    def _add(self, message: str, path: str = "") -> None:
        self.issues.append(Issue(path or f"message[{self.position}]", message))

    def feed(self, message: dict) -> list[Issue]:
        before = len(self.issues)
        problems = validate_message(message)
        for p in problems:
            self.issues.append(Issue(f"message[{self.position}].{p.path}".rstrip("."), p.message, p.severity))
        kind = message.get("type") if isinstance(message, dict) else None

        if self.refused:
            self._add("run refused after an unsupported hello")
        if self.closed is not None:
            self._add("message after closed")
        if self.hello is None and kind != "hello":
            self._add("first message must be hello")
        if kind == "hello":
            if self.hello is not None:
                self._add("second hello")
            self.hello = message
            proto = str(message.get("protocol", ""))
            major = proto.split("/")[-1]
            if not major.isdigit() or int(major) != MAJOR:
                self._add(f"unsupported protocol {proto!r}; this reader speaks {PROTOCOL}")
                self.refused = True
        elif kind in ("item", "tombstone"):
            item_id = message.get("id", "")
            if self.source_id and source_of(item_id) != self.source_id:
                self._add(f"id {item_id!r} does not belong to source {self.source_id!r}")
            if item_id in self.seen:
                self._add(f"id {item_id!r} emitted twice in one run")
            self.seen[item_id] = kind
            if kind == "item" and not problems:
                anchors = [f["anchor"] for f in message.get("fragments", [])]
                if len(anchors) != len(set(anchors)):
                    self._add(f"duplicate fragment anchors in {item_id!r}")
                for f in message.get("fragments", []):
                    if f.get("hash") != fragment_hash(f):
                        self._add(f"fragment {f.get('anchor')!r} of {item_id!r} has a wrong hash")
                if message.get("hash") != item_hash(message):
                    self._add(f"item {item_id!r} has a wrong content hash")
        elif kind == "error" and message.get("id"):
            self.error_ids.add(message["id"])
        elif kind == "listing":
            scope = message.get("scope", "")
            if self.source_id and source_of(scope) != self.source_id:
                self._add(f"listing scope {scope!r} does not belong to source {self.source_id!r}")
            emitted = sum(1 for i, k in self.seen.items() if k == "item" and i.startswith(scope))
            if message.get("complete") and message.get("count") != emitted:
                self._add(f"listing {scope!r} reports {message.get('count')} item(s) but the run emitted {emitted}")
            self.listings.append(message)
        elif kind == "closed":
            self.closed = message
        self.position += 1
        return self.issues[before:]

    def finish(self) -> list[Issue]:
        before = len(self.issues)
        if self.hello is None:
            self.issues.append(Issue("", "no hello"))
        if self.closed is None:
            self.issues.append(Issue("", "stream ended without closed: the run counts as failed"))
        return self.issues[before:]

    @property
    def complete_run(self) -> bool:
        return self.closed is not None and self.closed.get("status") == "ok"
