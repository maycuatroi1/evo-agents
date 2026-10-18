"""The ``plugin/1`` protocol between the hub's plugin host and the backend of an isolated plugin (docs/plugins.md).

The backend is a process the host starts: JSON-RPC 2.0, one JSON object per line in UTF-8, the host's requests on
the backend's stdin and the backend's responses on its stdout; stderr is for logs only, and anything else on stdout
breaks the protocol. Every request carries an id and gets exactly one response with that id; the backend answers
in any order. plugin/1 has no request from the backend to the host: a backend that needs the hub calls its HTTP API
under /v1/plugin-api/ with the token ``initialize`` gives it, which carries only what the projects that enabled the
plugin granted.

Methods, each with the schema of its params and of its result (``PARAMS``, ``RESULTS``):

- ``initialize``, first and once: the protocol, the plugin's id and version, the hub's URL and the plugin's token, the
  effective config of the hub (``instance``) and of each project that enabled it (``projects``), secrets included,
  and what each project granted. The result names the protocol and the plugin back, and the MCP tools it serves
  with their input schemas;
- ``ping``: an empty object back, while the backend serves;
- ``route``, ``tool``, ``job``, ``event``, ``channel``: one call to a contribution to the extension point of the same
  name in ``POINT_METHODS`` (an HTTP route under /v1/x/<plugin>/, an MCP tool, a periodic job, an event, a channel of
  notifications), naming the contribution and the project. Their params are open objects, so a later minor version
  adds keys without a new major. A ``job`` names the projects that enable the plugin (``projects``); an ``event``
  names its kind (one of EVENT_KINDS) and an ``event`` or ``channel`` the hub's id of the delivery (``delivery``),
  which stays the same when the hub delivers it again: the hub delivers at least once, so a backend that must act
  once keeps the ids it has seen;
- ``shutdown``: an empty object back, then the backend exits; the host kills it when it does not.

Errors are JSON-RPC's: -32700 for a line that is not JSON, -32600 for a message that is not a request, -32601 for a
method the backend does not serve, -32602 for params it refuses, -32603 for its own failure; -32000 to -32099 are
the backend's to give. A backend that cannot read a line answers -32700 with id null and keeps serving.

Standard library only, besides ``evo_agents.schema``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from evo_agents.schema import errors, validate

PROTOCOL = "plugin/1"
MAJOR = 1
JSONRPC = "2.0"
MAX_LINE = 4 * 1024 * 1024  # bytes of one message, its newline included
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
POINT_METHODS = {"routes": "route", "mcp_tools": "tool", "jobs": "job", "events": "event", "channels": "channel"}
BACKEND_POINTS = tuple(POINT_METHODS)  # the extension points an isolated plugin serves from its backend
EVENT_KINDS = ("run.state", "chat.message", "plan.step")  # what an events contribution may take (``on``)
NAME = {"type": "string", "pattern": r"^[a-z][a-z0-9_-]{0,62}$"}
PROJECT = {"type": "string", "minLength": 1, "maxLength": 100}
CALLER = {
    "type": "object",
    "properties": {"login": {"type": "string"}, "project": PROJECT},
}

_OBJECT = {"type": "object"}
_PLUGIN = {
    "type": "object",
    "required": ["id", "version"],
    "properties": {"id": {"type": "string"}, "version": {"type": "string"}},
}
_TOOL = {
    "type": "object",
    "required": ["name", "input_schema"],
    "properties": {
        "name": NAME,
        "description": {"type": "string", "maxLength": 2000},
        "input_schema": {"type": "object", "required": ["type"], "properties": {"type": {"const": "object"}}},
    },
}
PARAMS: dict[str, dict] = {
    "initialize": {
        "type": "object",
        "required": ["protocol", "plugin", "hub", "config", "grants"],
        "properties": {
            "protocol": {"const": PROTOCOL},
            "plugin": _PLUGIN,
            "hub": {
                "type": "object",
                "required": ["url", "token", "version"],
                "properties": {"url": {"type": "string"}, "token": {"type": "string"}, "version": {"type": "string"}},
            },
            "config": {
                "type": "object",
                "required": ["instance", "projects"],
                "properties": {"instance": _OBJECT, "projects": _OBJECT},
            },
            "grants": _OBJECT,
        },
    },
    "ping": _OBJECT,
    "shutdown": _OBJECT,
    "route": {
        "type": "object",
        "required": ["route", "request", "caller"],
        "properties": {
            "route": NAME,
            "request": {
                "type": "object",
                "required": ["method", "path"],
                "properties": {
                    "method": {"type": "string"},
                    "path": {"type": "string"},
                    "query": _OBJECT,
                    "headers": _OBJECT,
                    "body": {"type": "string", "description": "base64"},
                },
            },
            "caller": CALLER,
        },
    },
    "tool": {
        "type": "object",
        "required": ["tool", "arguments", "caller"],
        "properties": {"tool": NAME, "arguments": _OBJECT, "caller": CALLER},
    },
    "job": {
        "type": "object",
        "required": ["job"],
        "properties": {"job": NAME, "projects": {"type": "array", "items": PROJECT}},
    },
    "event": {
        "type": "object",
        "required": ["event", "project", "payload"],
        "properties": {
            "event": NAME,
            "project": PROJECT,
            "payload": _OBJECT,
            "kind": {"enum": list(EVENT_KINDS)},
            "delivery": {"type": "integer", "minimum": 1},
        },
    },
    "channel": {
        "type": "object",
        "required": ["channel", "project", "notification"],
        "properties": {
            "channel": NAME,
            "project": PROJECT,
            "notification": _OBJECT,
            "delivery": {"type": "integer", "minimum": 1},
        },
    },
}
RESULTS: dict[str, dict] = {
    "initialize": {
        "type": "object",
        "required": ["protocol", "plugin"],
        "properties": {
            "protocol": {"type": "string", "pattern": r"^plugin/[0-9]+$"},
            "plugin": _PLUGIN,
            "tools": {"type": "array", "items": _TOOL},
        },
    },
    "ping": _OBJECT,
    "shutdown": _OBJECT,
    "route": {
        "type": "object",
        "required": ["status"],
        "properties": {
            "status": {"type": "integer", "minimum": 100, "maximum": 599},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "body": {"type": "string", "description": "base64"},
        },
    },
    "tool": {
        "type": "object",
        "required": ["content"],
        "properties": {
            "content": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["type", "text"],
                    "properties": {"type": {"const": "text"}, "text": {"type": "string"}},
                },
            },
            "is_error": {"type": "boolean"},
        },
    },
    "job": _OBJECT,
    "event": _OBJECT,
    "channel": {"type": "object", "required": ["delivered"], "properties": {"delivered": {"type": "boolean"}}},
}
METHODS = tuple(PARAMS)


class ProtocolError(ValueError):
    """A line or a message that breaks plugin/1."""


@dataclass(frozen=True)
class RemoteError(Exception):
    """The error a backend answered a request with."""

    code: int
    message: str

    def __str__(self) -> str:
        return f"{self.message} (code {self.code})"


def request(request_id: int, method: str, params: dict) -> bytes:
    """One request as the line the host writes."""
    message = {"jsonrpc": JSONRPC, "id": request_id, "method": method, "params": params}
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def parse_line(line: bytes) -> dict:
    """The response a backend wrote on one line; ProtocolError saying why it is none."""
    if len(line) > MAX_LINE:
        raise ProtocolError(f"a line of {len(line)} bytes, over the {MAX_LINE} plugin/1 allows")
    try:
        message = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"not JSON: {exc}") from None
    if not isinstance(message, dict):
        raise ProtocolError(f"{type(message).__name__}, not an object")
    problem = envelope_problem(message)
    if problem:
        raise ProtocolError(problem)
    return message


def envelope_problem(message: dict) -> str | None:
    """Why ``message`` is not a JSON-RPC 2.0 response, or None."""
    if message.get("jsonrpc") != JSONRPC:
        return 'no "jsonrpc": "2.0"'
    if "method" in message:
        return f"a request or notification ({message['method']!r}); plugin/1 has none from the backend"
    if "id" not in message or not isinstance(message["id"], (int, str, type(None))):
        return "a response without an id"
    if ("result" in message) == ("error" in message):
        return "a response carries either a result or an error"
    if "error" in message:
        error = message["error"]
        if not isinstance(error, dict) or not isinstance(error.get("code"), int):
            return "an error without an integer code"
        if not isinstance(error.get("message"), str):
            return "an error without a message"
    return None


def schema_problems(schema: dict, value) -> list[str]:
    return [f"{issue.path or '<root>'}: {issue.message}" for issue in errors(validate(value, schema))]


def result_problems(method: str, result) -> list[str]:
    """What is wrong with ``result`` as the result of ``method``."""
    return schema_problems(RESULTS[method], result)


def params_problems(method: str, params) -> list[str]:
    """What is wrong with ``params`` as the params of ``method``; a method plugin/1 does not have is one."""
    if method not in PARAMS:
        return [f"plugin/1 has no method {method!r}"]
    return schema_problems(PARAMS[method], params)


def outcome(message: dict):
    """The result of a response; RemoteError for an error."""
    if "error" in message:
        error = message["error"]
        raise RemoteError(error["code"], str(error["message"])[:2000])
    return message["result"]
