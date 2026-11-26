"""The events of a run for ``evo-agents hub run logs``: a page of them (GET .../runs/{id}/events?after=SEQ), a line
each, or its server-sent events followed until the hub sends ``end`` (``follow``), out of ``run_cli``.

A followed stream that stops before ``end`` (a proxy closed it, the hub restarted, or nothing came for
STREAM_TIMEOUT seconds although the hub pings every 15) is opened again with ``Last-Event-ID`` set to the last event
read, so no event is missed or printed twice. After ``end`` it is never opened again. RECONNECTS tries in a row that
bring nothing, not even a ping, end the command with an error. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import urlencode

from evo_agents import __version__
from evo_agents.hub.cli_client import project_path
from evo_agents.hub.client import OPENER, HubError, Unreachable, json_body, origin
from evo_agents.hub.sse import sse_messages

EVENTS_PAGE = 1000  # the most events GET .../events answers at once
STREAM_TIMEOUT = 60.0  # seconds without a byte, pings included, before a followed stream counts as dropped
RECONNECTS = 5  # tries in a row to open a followed stream again that bring no event before giving up
BACKOFF = (1, 2, 4, 8, 15)  # seconds before each of those tries
RETRIED_STATUSES = frozenset({502, 503, 504})  # a proxy in front of a hub that restarts: worth another try
RAW_CHARS = 300  # of an event body shown as JSON

KIND_LABELS = {
    "agent_message_chunk": "agent",
    "agent_thought_chunk": "thought",
    "tool_call": "tool",
    "tool_call_update": "tool",
    "plan": "plan",
    "usage_update": "usage",
    "system": "system",
    "output": "output",
    "user_message": "message",
    "state": "state",
}
LABEL_WIDTH = max(len(label) for label in KIND_LABELS.values())


def runs_path(project: str, run_id: int | None = None, action: str | None = None) -> str:
    """The path of the runs of ``project``, of run ``run_id``, or of its ``action``."""
    path = f"{project_path(project)}/runs"
    if run_id is not None:
        path += f"/{run_id}"
    return path + (f"/{action}" if action else "")


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def clock(value) -> str:
    """An event's time as HH:MM:SS UTC."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return "--:--:--"
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%H:%M:%S")


def event_text(event: dict) -> str:
    """What an event says, in words: the agent's text, a move between states, a tool's name, else its body as JSON."""
    body = event.get("body")
    body = body if isinstance(body, dict) else {}
    kind = event.get("kind")
    if kind == "state":
        text = f"{body.get('from')} -> {body.get('to')} by {body.get('actor')}"
        return text + (f": {body['reason']}" if body.get("reason") else "")
    if isinstance(body.get("text"), str):
        return f"{body.get('from', '?')}: {body['text']}" if kind == "user_message" else body["text"]
    content = body.get("content")
    if isinstance(content, dict) and isinstance(content.get("text"), str):  # the adapters' ACP-shaped chunks
        return content["text"]
    if kind in ("tool_call", "tool_call_update"):
        name = next((body[key] for key in ("title", "name", "kind") if isinstance(body.get(key), str)), None)
        if name:
            status = body.get("status")
            return name + (f" ({status})" if isinstance(status, str) else "")
    return clip(json.dumps(body, ensure_ascii=False, separators=(",", ":")), RAW_CHARS)


def format_event(event: dict) -> str:
    """One event as ``SEQ HH:MM:SS KIND text``; the text's further lines are indented under its first."""
    kind = str(event.get("kind"))
    prefix = f"{event.get('seq', '?'):>5} {clock(event.get('at'))} {KIND_LABELS.get(kind, kind):<{LABEL_WIDTH}} "
    lines = event_text(event).splitlines() or [""]
    indent = " " * len(prefix)
    return (prefix + lines[0]).rstrip() + "".join(f"\n{indent}{line}".rstrip() for line in lines[1:])


def print_event(event: dict) -> None:
    print(format_event(event), flush=True)


def read_events(hub, project: str, run_id: int, after: int = 0, kinds: Iterable[str] = ()) -> dict:
    """Every event of the run after ``after`` (of ``kinds`` only, when given), read a page at a time: the hub's
    answer to a page, holding them all."""
    path = runs_path(project, run_id, "events")
    events: list[dict] = []
    while True:
        query = [("after", after), ("limit", EVENTS_PAGE), *(("kind", kind) for kind in kinds)]
        page = hub.call("GET", f"{path}?{urlencode(query)}")
        events.extend(page["events"])
        if not page["more"] or not page["events"]:
            return {**page, "events": events, "more": False}
        after = page["events"][-1]["seq"]


def _refusal(hub, status: int, payload) -> HubError:
    """The HubError ``Hub.call`` raises for the same answer."""
    if 300 <= status < 400:
        return HubError(f"the hub at {hub.url} answered {status} with a redirect, which is not followed", status)
    message = payload.get("message") if isinstance(payload, dict) else None
    code = payload.get("error") if isinstance(payload, dict) else None
    return HubError(str(message or f"the hub at {hub.url} answered HTTP {status}"), status, code, payload)


@contextmanager
def open_stream(hub, path: str, last_event_id: int | None = None) -> Iterator[Iterable[bytes]]:
    """The lines of the server-sent events at ``path``; ``last_event_id`` resumes after that event. HubError for a
    refusal, Unreachable for a hub that does not answer."""
    headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache", "User-Agent": f"evo-agents/{__version__}"}
    if hub.token:
        headers["Authorization"] = f"Bearer {hub.token}"
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    request = urllib.request.Request(hub.url + path, headers=headers)
    try:
        response = OPENER.open(request, timeout=STREAM_TIMEOUT)
    except urllib.error.HTTPError as exc:
        with exc:
            payload = json_body(exc.read())
        raise _refusal(hub, exc.code, payload) from None
    except urllib.error.URLError as exc:
        refused = isinstance(exc.reason, ConnectionRefusedError)
        raise Unreachable(f"cannot reach {origin(hub.url)}: {exc.reason}", refused) from None
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        reason = (
            f"no answer within {STREAM_TIMEOUT:g}s" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
        )
        raise Unreachable(f"cannot reach {origin(hub.url)}: {reason}") from None
    with response:
        kind = response.headers.get("Content-Type", "")
        if not kind.startswith("text/event-stream"):
            raise HubError(f"the hub at {hub.url} answered {kind or 'no content type'}, not an event stream")
        yield response


def follow(
    hub,
    project: str,
    run_id: int,
    after: int = 0,
    *,
    on_event: Callable[[dict], None] = print_event,
    kinds: frozenset[str] = frozenset(),
    opener=open_stream,
    sleep: Callable[[float], None] = time.sleep,
    warn: Callable[[str], None] | None = None,
) -> dict:
    """Hand each event of the run after ``after`` to ``on_event`` (those of ``kinds``, when given) as the hub streams
    them, until the stream's ``end``; what ``end`` says, ``{state, last_seq}``. A stream that stops before ``end`` is
    opened again after the last event read."""
    warn = warn or (lambda line: print(line, file=sys.stderr, flush=True))
    path = runs_path(project, run_id, "stream")
    first_path = f"{path}?{urlencode({'after': after})}"
    last, opened, tries = after, False, 0
    while True:
        received = False

        def counted(lines: Iterable[bytes]) -> Iterator[bytes]:
            nonlocal received
            for line in lines:
                received = True
                yield line

        try:
            with opener(hub, path if opened else first_path, last if opened else None) as lines:
                opened = True
                for name, data, _ in sse_messages(counted(lines)):
                    try:
                        message = json.loads(data)
                    except ValueError:
                        raise HubError(f"the hub sent a {name} event of run {run_id} that is not JSON") from None
                    if name == "end":
                        return message
                    if name != "message" or not isinstance(message, dict):
                        continue
                    if isinstance(message.get("seq"), int):
                        last = message["seq"]
                    if not kinds or message.get("kind") in kinds:
                        on_event(message)
            reason = "the hub closed the stream before the run ended"
        except HubError as exc:
            if not isinstance(exc, Unreachable) and exc.status not in RETRIED_STATUSES:
                raise
            reason = str(exc)
        except (OSError, http.client.HTTPException) as exc:  # a read that timed out, or a connection cut mid-stream
            reason = f"the stream from {origin(hub.url)} broke off ({exc or type(exc).__name__})"
        if received:  # a stream that brought something, a ping at least, starts the count again
            tries = 0
        if tries >= RECONNECTS:
            raise HubError(
                f"the stream of run {run_id} stopped {tries + 1} times in a row without bringing anything ({reason}); "
                f"`evo-agents hub run logs {run_id} --after {last}` reads what came since"
            )
        delay = BACKOFF[min(tries, len(BACKOFF) - 1)]
        tries += 1
        warn(f"note: {reason}; reading on after event {last} in {delay}s")
        sleep(delay)
