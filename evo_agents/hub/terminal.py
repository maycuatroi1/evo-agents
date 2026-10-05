"""The web terminal's wire format, shared by the api's relay (``evo_agents.hub.server.terminal``) and the worker
daemon. Standard library only. ``docs/workers.md`` describes the relay.

A frame is one binary websocket message of at most MAX_FRAME_BYTES, whose first byte is its type, as in ttyd: INPUT
from the browser (keys typed, text pasted), OUTPUT from the worker (what the terminal printed) and RESIZE from the
browser (the terminal's new size). The bytes after the type are the payload; a RESIZE payload is the columns, then
the rows, each an unsigned 16-bit big-endian integer from 1 to MAX_SIZE.

The browser's first message is the hello, a text message: ``{"csrf": "<the session's X-Evo-CSRF value>", "cols":
120, "rows": 40}``, the size optional (both or neither). Every message after it is a frame.

A socket the hub closes carries one of the CLOSE_* codes, so either end can tell a lost session from a refusal and
from the other end leaving.
"""

from __future__ import annotations

import struct

INPUT = 0  # browser to worker: keys typed, text pasted
OUTPUT = 1  # worker to browser: what the terminal printed
RESIZE = 2  # browser to worker: columns and rows
FROM_BROWSER = frozenset({INPUT, RESIZE})
FROM_WORKER = frozenset({OUTPUT})
MAX_FRAME_BYTES = 64 * 1024  # a whole frame, its type byte included
MAX_SIZE = 1000  # columns or rows of a RESIZE
_SIZE = struct.Struct(">HH")

CLOSE_NORMAL = 1000  # the other end left
CLOSE_UNSUPPORTED = 1003  # a text message after the hello, an empty frame, or a type this end may not send
CLOSE_TOO_BIG = 1009  # a frame over MAX_FRAME_BYTES
CLOSE_UNAVAILABLE = 1011  # the hub could not check the request (its database did not answer)
CLOSE_UNAUTHENTICATED = 4401  # no session or worker token, or one that is revoked, expired or unknown
CLOSE_FORBIDDEN = 4403  # a credential that may not open this terminal
CLOSE_TIMEOUT = 4408  # no hello in time, idle too long, or open too long
CLOSE_BUSY = 4409  # the run's terminal is open in another browser, or its worker's end is connected already
CLOSE_UPGRADE = 4426  # the worker speaks another version of the worker protocol
MAX_REASON_BYTES = 123  # a websocket close reason, as UTF-8 (RFC 6455)


class FrameError(ValueError):
    """A frame or a resize that is not one this format allows; the message says why."""


def frame(kind: int, payload: bytes = b"") -> bytes:
    if kind not in (INPUT, OUTPUT, RESIZE):
        raise FrameError(f"frame type {kind} is not one of input (0), output (1) and resize (2)")
    data = bytes((kind,)) + payload
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError(f"a frame is at most {MAX_FRAME_BYTES} bytes, type included; send it in parts")
    return data


def resize(cols: int, rows: int) -> bytes:
    check_size(cols, rows)
    return frame(RESIZE, _SIZE.pack(cols, rows))


def check_size(cols, rows) -> None:
    for name, value in (("cols", cols), ("rows", rows)):
        if type(value) is not int or not 1 <= value <= MAX_SIZE:
            raise FrameError(f"{name} is a whole number from 1 to {MAX_SIZE}")


def parse(data: bytes) -> tuple[int, bytes]:
    """(type, payload) of a frame as it arrived."""
    if not data:
        raise FrameError("an empty frame has no type")
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError(f"a frame is at most {MAX_FRAME_BYTES} bytes, type included")
    if data[0] not in (INPUT, OUTPUT, RESIZE):
        raise FrameError(f"frame type {data[0]} is not one of input (0), output (1) and resize (2)")
    return data[0], data[1:]


def parse_resize(payload: bytes) -> tuple[int, int]:
    """(cols, rows) of a RESIZE frame's payload."""
    if len(payload) != _SIZE.size:
        raise FrameError("a resize is the columns and the rows, two unsigned 16-bit big-endian integers")
    cols, rows = _SIZE.unpack(payload)
    check_size(cols, rows)
    return cols, rows


def close_reason(text: str) -> str:
    """``text`` cut on a character boundary to what a close frame carries."""
    encoded = text.encode()
    if len(encoded) <= MAX_REASON_BYTES:
        return text
    return encoded[: MAX_REASON_BYTES - 3].decode(errors="ignore") + "..."
