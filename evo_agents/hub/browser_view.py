"""The live view of a run's browser: its wire format, shared by the api's relay (``evo_agents.hub.server.browser_view``)
and the worker daemon's broker (``evo_agents.worker.browser.live``). Standard library only. ``docs/workers.md``
describes the relay.

A frame is one binary websocket message of at most MAX_FRAME_BYTES, whose first byte is its type; the bytes after it
are the payload, a JSON object unless said otherwise:

- INPUT, viewer to worker: one event the person made (``parse_event`` says which and what each holds).
- SCREEN, worker to viewer: one screencast frame of the tab watched, the length of a JSON header as an unsigned 32-bit
  big-endian integer, the header (``{"seq", "width", "height", "top", "scale"}``: the page's size in CSS pixels, the
  offset of its top and its zoom, as Chromium's screencast metadata gives them), then the JPEG.
- STATE, worker to viewer: the browser as the broker sees it, ``{"mode": "agent"|"human", "tabs": [{"id", "url",
  "title"}], "active": <id>}`` and a ``notice`` once when it refused an event (a URL of another scheme).
- ACK, viewer to worker: ``{"seq": n}``, the viewer drew screen frame n; the worker sends the next one after it.
- VIEW, hub to worker: ``{"viewing": bool, "login": bool}``, whether someone watches, and whether the run waits on a
  login decision, so it opens in human mode.
- LINK, hub to viewer: ``{"worker": bool}``, whether the worker's end is connected; frames pass only while it is.

Coordinates of an event are CSS pixels of the page, as the SCREEN header measures it. The viewer's first message is
the hello, a text message ``{"csrf": "<the session's X-Evo-CSRF value>"}``; every message after it is a frame. A socket
the hub closes carries one of the CLOSE_* codes of ``evo_agents.hub.terminal``.
"""

from __future__ import annotations

import json
import struct

from evo_agents.hub.terminal import FrameError

INPUT = 0  # viewer to worker: an event of the person
SCREEN = 1  # worker to viewer: a screencast frame
STATE = 2  # worker to viewer: mode and tabs
ACK = 3  # viewer to worker: a screen frame drawn
VIEW = 4  # hub to worker: someone watches, or no longer
LINK = 5  # hub to viewer: the worker's end is there, or not
TYPES = (INPUT, SCREEN, STATE, ACK, VIEW, LINK)
FROM_VIEWER = frozenset({INPUT, ACK})
FROM_WORKER = frozenset({SCREEN, STATE})
MAX_FRAME_BYTES = 512 * 1024  # a whole frame, its type byte included; the web terminal keeps its own 64 KiB

AGENT = "agent"  # the agent drives the browser; the person watches and picks the tab watched
HUMAN = "human"  # the person drives it: the run was taken over, or waits on a login decision
MODES = (AGENT, HUMAN)

MOUSE_ACTIONS = ("down", "up", "move")
BUTTONS = ("none", "left", "middle", "right", "back", "forward")
KEY_ACTIONS = ("down", "up")
NAVIGATIONS = ("back", "forward", "reload")
EVENTS = ("mouse", "wheel", "key", "text", "paste", "tab", "open", *NAVIGATIONS)
MAX_COORDINATE = 100_000.0  # CSS pixels, either way
MAX_MODIFIERS = 15  # Alt 1, Ctrl 2, Meta 4, Shift 8
MAX_BUTTONS = 31
MAX_CLICKS = 3
MAX_NAME_CHARS = 64  # a key's key and code, a tab's id
MAX_KEY_TEXT_CHARS = 16  # the text a key types
MAX_URL_CHARS = 8192
_LENGTH = struct.Struct(">I")
_TYPES_TEXT = "input (0), screen (1), state (2), ack (3), view (4) and link (5)"


def frame(kind: int, payload: bytes = b"") -> bytes:
    if kind not in TYPES:
        raise FrameError(f"frame type {kind} is not one of {_TYPES_TEXT}")
    data = bytes((kind,)) + payload
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError(f"a frame is at most {MAX_FRAME_BYTES} bytes, type included")
    return data


def message(kind: int, body: dict) -> bytes:
    """A frame whose payload is ``body`` as compact JSON."""
    return frame(kind, json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())


def parse(data: bytes) -> tuple[int, bytes]:
    """(type, payload) of a frame as it arrived."""
    if not data:
        raise FrameError("an empty frame has no type")
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError(f"a frame is at most {MAX_FRAME_BYTES} bytes, type included")
    if data[0] not in TYPES:
        raise FrameError(f"frame type {data[0]} is not one of {_TYPES_TEXT}")
    return data[0], data[1:]


def body_of(payload: bytes) -> dict:
    """The JSON object of a payload."""
    try:
        body = json.loads(payload)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise FrameError("the payload of this frame is a JSON object")
    return body


def screen(header: dict, jpeg: bytes) -> bytes:
    encoded = json.dumps(header, separators=(",", ":")).encode()
    return frame(SCREEN, _LENGTH.pack(len(encoded)) + encoded + jpeg)


def parse_screen(payload: bytes) -> tuple[dict, bytes]:
    """(header, JPEG) of a SCREEN frame's payload."""
    if len(payload) < _LENGTH.size:
        raise FrameError("a screen frame starts with the length of its header")
    (length,) = _LENGTH.unpack_from(payload)
    end = _LENGTH.size + length
    if end > len(payload):
        raise FrameError("a screen frame's header is longer than the frame")
    return body_of(payload[_LENGTH.size : end]), payload[end:]


def mode_of(body: dict) -> str | None:
    """The mode a STATE frame's body says, None when it says none this format knows."""
    mode = body.get("mode")
    return mode if mode in MODES else None


# The events of INPUT


def _number(event: dict, name: str, *, limit: float = MAX_COORDINATE, default: float | None = None) -> float:
    value = event.get(name, default)
    if type(value) not in (int, float) or not -limit <= value <= limit:
        raise FrameError(f"{event.get('type')}: {name} is a number from {-limit:g} to {limit:g}")
    return float(value)


def _whole(event: dict, name: str, limit: int) -> int:
    value = event.get(name, 0)
    if type(value) is not int or not 0 <= value <= limit:
        raise FrameError(f"{event.get('type')}: {name} is a whole number from 0 to {limit}")
    return value


def _text(event: dict, name: str, limit: int | None = None, *, default: str | None = None) -> str:
    value = event.get(name, default)
    if not isinstance(value, str) or (limit is not None and len(value) > limit):
        bound = "" if limit is None else f" of at most {limit} characters"
        raise FrameError(f"{event.get('type')}: {name} is a text{bound}")
    return value


def _choice(event: dict, name: str, choices: tuple[str, ...], default: str | None = None) -> str:
    value = event.get(name, default)
    if value not in choices:
        raise FrameError(f"{event.get('type')}: {name} is one of {', '.join(choices)}")
    return value


def _mouse(event: dict) -> dict:
    return {
        "type": "mouse",
        "action": _choice(event, "action", MOUSE_ACTIONS),
        "x": _number(event, "x"),
        "y": _number(event, "y"),
        "button": _choice(event, "button", BUTTONS, "none"),
        "buttons": _whole(event, "buttons", MAX_BUTTONS),
        "clicks": _whole(event, "clicks", MAX_CLICKS),
        "modifiers": _whole(event, "modifiers", MAX_MODIFIERS),
    }


def _wheel(event: dict) -> dict:
    return {
        "type": "wheel",
        "x": _number(event, "x"),
        "y": _number(event, "y"),
        "dx": _number(event, "dx", default=0),
        "dy": _number(event, "dy", default=0),
        "modifiers": _whole(event, "modifiers", MAX_MODIFIERS),
    }


def _key(event: dict) -> dict:
    return {
        "type": "key",
        "action": _choice(event, "action", KEY_ACTIONS),
        "key": _text(event, "key", MAX_NAME_CHARS),
        "code": _text(event, "code", MAX_NAME_CHARS, default=""),
        "keyCode": _whole(event, "keyCode", 255),
        "text": _text(event, "text", MAX_KEY_TEXT_CHARS, default=""),
        "modifiers": _whole(event, "modifiers", MAX_MODIFIERS),
    }


def parse_event(payload: bytes) -> dict:
    """The event of an INPUT frame, with only the fields its type has, each checked:

    - ``{"type": "mouse", "action": "down"|"up"|"move", "x", "y", "button", "buttons", "clicks", "modifiers"}``
    - ``{"type": "wheel", "x", "y", "dx", "dy", "modifiers"}``
    - ``{"type": "key", "action": "down"|"up", "key", "code", "keyCode", "text", "modifiers"}``, a key pressed or
      let go; ``text`` is what it types, empty for a key that types nothing
    - ``{"type": "text", "text"}``, text an input method composed, and ``{"type": "paste", "text"}``, text pasted:
      both typed as they are, never as keys
    - ``{"type": "back"}``, ``{"type": "forward"}``, ``{"type": "reload"}`` on the tab watched
    - ``{"type": "tab", "id"}``, watch another tab, and ``{"type": "open", "url"}``, open a URL in the tab watched"""
    event = body_of(payload)
    kind = event.get("type")
    if kind not in EVENTS:
        raise FrameError(f"an input event's type is one of {', '.join(EVENTS)}")
    if kind == "mouse":
        return _mouse(event)
    if kind == "wheel":
        return _wheel(event)
    if kind == "key":
        return _key(event)
    if kind in ("text", "paste"):
        return {"type": kind, "text": _text(event, "text")}
    if kind == "tab":
        return {"type": kind, "id": _text(event, "id", MAX_NAME_CHARS)}
    if kind == "open":
        return {"type": kind, "url": _text(event, "url", MAX_URL_CHARS)}
    return {"type": kind}
