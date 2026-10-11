"""Server-sent events as the hub streams them (text/event-stream): the messages in the lines of a stream, read as the
SSE standard says. Standard library only, like the rest of the client; ``evo-agents hub run logs --follow`` reads a
run's stream with it (``run_cli.follow``)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator


def sse_messages(lines: Iterable[bytes | str]) -> Iterator[tuple[str, str, str | None]]:
    """``(event, data, id)`` of each message in the lines of a text/event-stream, read as the SSE standard says:
    comments skipped, ``data`` lines joined with newlines, a message without data dropped, and ``event`` 'message'
    when the message names none. ``id`` is the last id the stream set. ``retry`` and unknown fields are ignored."""
    event, data, last_id = "", [], None
    for raw in lines:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if text.endswith("\n"):
            text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
        for line in text.split("\r"):
            if not line:
                if data:
                    yield event or "message", "\n".join(data), last_id
                event, data = "", []
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)
            elif name == "id" and "\0" not in value:
                last_id = value
