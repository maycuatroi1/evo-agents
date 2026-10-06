"""``evo-agents hub run``: dispatch plan steps to your own workers, and follow, steer and end their runs.

``dispatch`` queues one run per step named (POST /v1/projects/{p}/runs), all of them or none. ``list`` and ``show``
read runs. ``logs`` prints a run's events page by page (GET .../runs/{id}/events?after=SEQ), or with ``--follow``
reads the run's server-sent events (GET .../runs/{id}/stream) until the hub sends ``end``. ``send`` leaves a message
for the run's agent, and ``cancel``, ``approve``, ``takeover``, ``handback`` and ``rerun`` are the owner's controls.
Every command after ``dispatch`` and ``list`` takes the id of a run, as ``list`` shows it. ``docs/workers.md``
describes the protocol behind them; ``--json`` prints what the hub answered, with the keys declared next to the flag.

A followed stream that stops before ``end`` (a proxy closed it, the hub restarted, or nothing came for
STREAM_TIMEOUT seconds although the hub pings every 15) is opened again with ``Last-Event-ID`` set to the last event
read, so no event is missed or printed twice. After ``end`` it is never opened again. RECONNECTS tries in a row that
bring nothing, not even a ping, end the command with an error.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the
current directory, as ``hub plan`` does. Standard library only, like the rest of the client.
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
from datetime import timezone
from urllib.parse import urlencode

from evo_agents import __version__
from evo_agents.hub.cli_client import _client_command, _print_json, _project_path, _signed_in, _table, _when
from evo_agents.hub.client import _OPENER, HubError, Unreachable, _json, _origin
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.plan_cli import _plan_id, _project
from evo_agents.hub.runs import APPROVALS, EVENT_KINDS, MODES, RUN_STATES, RUNTIMES, TERMINAL_STATES
from evo_agents.isotime import parse_iso

EXIT_USAGE = 2
REQUESTED_RUNTIMES = ("any", *RUNTIMES)
EVENTS_PAGE = 1000  # the most events GET .../events answers at once
STREAM_TIMEOUT = 60.0  # seconds without a byte, pings included, before a followed stream counts as dropped
RECONNECTS = 5  # tries in a row to open a followed stream again that bring no event before giving up
BACKOFF = (1, 2, 4, 8, 15)  # seconds before each of those tries
RETRIED_STATUSES = frozenset({502, 503, 504})  # a proxy in front of a hub that restarts: worth another try
TITLE_CHARS = 40  # of a step's title in the run table
RAW_CHARS = 300  # of an event body shown as JSON

# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
RUN_KEYS = (
    "id",
    "kind",
    "project",
    "plan_id",
    "step_key",
    "title",
    "plan_revision",
    "dispatched_by",
    "worker_id",
    "worker",
    "pinned_worker_id",
    "requested_runtime",
    "runtime",
    "model",
    "mode",
    "approval",
    "timeout_min",
    "run_seconds",
    "attempt",
    "max_attempts",
    "parent_run_id",
    "state",
    "lease_expires_at",
    "session_id",
    "repo",
    "branch",
    "repos",
    "commit_sha",
    "diffstat",
    "verify",
    "evidence",
    "usage",
    "error",
    "log_sha256",
    "diff_sha256",
    "last_seq",
    "cancel_requested_at",
    "takeover_requested_at",
    "handback_requested_at",
    "queued_at",
    "leased_at",
    "started_at",
    "finished_at",
)
RUN_LIST_KEYS = ("runs", "total", "counts", "limit", "offset")
EVENTS_KEYS = ("run_id", "state", "last_seq", "events", "more")
MESSAGE_KEYS = ("id", "run_id", "seq", "text", "sent_by", "created_at", "delivered_at")
RUN = returns_object(*RUN_KEYS, schema="Run")

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


def _runs_path(project: str, run_id: int | None = None, action: str | None = None) -> str:
    path = f"{_project_path(project)}/runs"
    if run_id is not None:
        path += f"/{run_id}"
    return path + (f"/{action}" if action else "")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _worker_id(hub, login: str, worker: str) -> int:
    """The id ``worker`` names: an id as given, else the id of the caller's own worker of that name."""
    if worker.isascii() and worker.isdigit():
        return int(worker)
    for row in hub.call("GET", "/v1/workers"):
        if row["name"] == worker and row["owner"] == login:
            return row["id"]
    raise HubError(f"you have no worker named {worker!r} (revoked ones aside); the Workers page of the hub lists yours")


def _step(run: dict) -> str:
    title = f" ({run['title']})" if run.get("title") else ""
    return f"step {run['step_key']}{title} of plan {run['plan_id']}"


def _with_project(args, project: str) -> str:
    """The --project to repeat in a command printed for the person, when the harness did not give the project."""
    return f" --project {project}" if args.project else ""


# Events


def _clock(value) -> str:
    """An event's time as HH:MM:SS UTC."""
    try:
        moment = parse_iso(value)
    except (TypeError, ValueError):
        return "--:--:--"
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%H:%M:%S")


def event_text(event: dict) -> str:
    """What an event says, in words: the agent's text, a move between states, a tool's name, else its body as JSON."""
    body = event.get("body") if isinstance(event.get("body"), dict) else {}
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
    return _clip(json.dumps(body, ensure_ascii=False, separators=(",", ":")), RAW_CHARS)


def format_event(event: dict) -> str:
    """One event as ``SEQ HH:MM:SS KIND text``; the text's further lines are indented under its first."""
    kind = str(event.get("kind"))
    prefix = f"{event.get('seq', '?'):>5} {_clock(event.get('at'))} {KIND_LABELS.get(kind, kind):<{LABEL_WIDTH}} "
    lines = event_text(event).splitlines() or [""]
    indent = " " * len(prefix)
    return (prefix + lines[0]).rstrip() + "".join(f"\n{indent}{line}".rstrip() for line in lines[1:])


def print_event(event: dict) -> None:
    print(format_event(event), flush=True)


def read_events(hub, project: str, run_id: int, after: int = 0, kinds: Iterable[str] = ()) -> dict:
    """Every event of the run after ``after`` (of ``kinds`` only, when given), read a page at a time: the hub's
    answer to a page, holding them all."""
    path = _runs_path(project, run_id, "events")
    events: list[dict] = []
    while True:
        query = [("after", after), ("limit", EVENTS_PAGE), *(("kind", kind) for kind in kinds)]
        page = hub.call("GET", f"{path}?{urlencode(query)}")
        events.extend(page["events"])
        if not page["more"] or not page["events"]:
            return {**page, "events": events, "more": False}
        after = page["events"][-1]["seq"]


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
        response = _OPENER.open(request, timeout=STREAM_TIMEOUT)
    except urllib.error.HTTPError as exc:
        with exc:
            payload = _json(exc.read())
        raise _refusal(hub, exc.code, payload) from None
    except urllib.error.URLError as exc:
        refused = isinstance(exc.reason, ConnectionRefusedError)
        raise Unreachable(f"cannot reach {_origin(hub.url)}: {exc.reason}", refused) from None
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        reason = (
            f"no answer within {STREAM_TIMEOUT:g}s" if isinstance(exc, TimeoutError) else str(exc) or type(exc).__name__
        )
        raise Unreachable(f"cannot reach {_origin(hub.url)}: {reason}") from None
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
    path = _runs_path(project, run_id, "stream")
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
            reason = f"the stream from {_origin(hub.url)} broke off ({exc or type(exc).__name__})"
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


# Commands


@_client_command
def cmd_dispatch(args) -> int:
    hub, credentials = _signed_in()
    project = _project(args)
    body = {"plan_id": _plan_id(args.plan), "steps": args.steps}
    chosen = {"runtime": args.runtime, "mode": args.mode, "approval": args.approval, "timeout_min": args.timeout}
    body.update({key: value for key, value in chosen.items() if value is not None})
    if args.worker is not None:
        body["worker_id"] = _worker_id(hub, credentials.login, args.worker)
    queued = hub.call("POST", _runs_path(project), body)
    if args.json:
        _print_json(queued)
        return 0
    for run in queued:
        pinned = f", on worker #{run['pinned_worker_id']} only" if run["pinned_worker_id"] else ""
        print(
            f"Queued run #{run['id']}: {_step(run)}, repo {run['repo']}; runtime {run['requested_runtime']}, "
            f"{run['mode']}, approval {run['approval']}, timeout {run['timeout_min']} min{pinned}."
        )
    print(f"Follow it with `evo-agents hub run logs {queued[0]['id']} --follow{_with_project(args, project)}`.")
    return 0


@_client_command
def cmd_list(args) -> int:
    hub, credentials = _signed_in()
    project = _project(args)
    query = [("state", state) for state in args.state or ()]
    if args.plan is not None:
        query.append(("plan_id", _plan_id(args.plan)))
    if args.worker is not None:
        query.append(("worker_id", _worker_id(hub, credentials.login, args.worker)))
    given = {"step": args.step, "dispatched_by": args.by, "q": args.search, "limit": args.limit, "offset": args.offset}
    query += [(key, value) for key, value in given.items() if value is not None]
    listed = hub.call("GET", _runs_path(project) + (f"?{urlencode(query)}" if query else ""))
    if args.json:
        _print_json(listed)
        return 0
    rows = [
        (
            f"#{run['id']}",
            run["state"],
            run["plan_id"],
            run["step_key"],
            _clip(run["title"] or "-", TITLE_CHARS),
            run["runtime"],
            run["worker"] or "-",
            run["dispatched_by"],
            _when(run["queued_at"]),
        )
        for run in listed["runs"]
    ]
    if rows:
        _table(("RUN", "STATE", "PLAN", "STEP", "TITLE", "RUNTIME", "WORKER", "BY", "QUEUED (UTC)"), rows)
    counts = ", ".join(f"{count} {state}" for state, count in listed["counts"].items() if count)
    shown = len(rows)
    print(f"{shown} of {listed['total']} run(s) of project {project}" + (f"; by state: {counts}" if counts else ""))
    if listed["offset"] + shown < listed["total"]:
        print(f"More with --offset {listed['offset'] + shown}.")
    return 0


def _describe(run: dict) -> list[tuple[str, str]]:
    """The fields of a run that say something, as (label, text) pairs."""
    lines = [
        ("state", f"{run['state']}, attempt {run['attempt']} of {run['max_attempts']}"),
        ("step", f"{run['step_key']}" + (f": {run['title']}" if run["title"] else "")),
        ("plan", f"{run['plan_id']} of project {run['project']}, revision {run['plan_revision']}"),
        (
            "repo",
            run["repo"]
            + (f", branch {run['branch']}" if run["branch"] else "")
            + (f", commit {run['commit_sha'][:12]}" if run["commit_sha"] else ""),
        ),
    ]
    worker = f"{run['worker']} (#{run['worker_id']})" if run["worker_id"] else "none yet"
    if run["pinned_worker_id"]:
        worker += f", pinned to worker #{run['pinned_worker_id']}"
    runtime = run["runtime"] + (
        f" (asked for {run['requested_runtime']})" if run["runtime"] != run["requested_runtime"] else ""
    )
    lines += [
        ("worker", worker),
        ("runtime", f"{runtime}, {run['mode']}, approval {run['approval']}, timeout {run['timeout_min']} min"),
        ("dispatched", f"by {run['dispatched_by']} at {_when(run['queued_at'])} UTC"),
    ]
    times = [(name, run[f"{name}_at"]) for name in ("leased", "started", "finished") if run[f"{name}_at"]]
    if times:
        lines.append(("times (UTC)", ", ".join(f"{name} {_when(value)}" for name, value in times)))
    if run["lease_expires_at"] and run["state"] not in TERMINAL_STATES:
        lines.append(("lease until", f"{_when(run['lease_expires_at'])} UTC"))
    asks = [(name, run[f"{name}_requested_at"]) for name in ("cancel", "takeover", "handback")]
    asked = [f"{name} at {_when(value)} UTC" for name, value in asks if value]
    if asked:
        lines.append(("asked", ", ".join(asked)))
    if run["parent_run_id"]:
        lines.append(("retries", f"run #{run['parent_run_id']}"))
    if run["session_id"]:
        lines.append(("session", run["session_id"]))
    stat = run["diffstat"]
    if isinstance(stat, dict):
        parts = [f"{stat[key]} {key}" for key in ("files", "insertions", "deletions") if key in stat]
        lines.append(("diffstat", ", ".join(parts) or json.dumps(stat)))
    for index, result in enumerate(run["verify"] or ()):
        text = (
            f"exit {result.get('exit_code')}: {result.get('command')}"
            if isinstance(result, dict)
            else json.dumps(result, ensure_ascii=False)
        )
        lines.append(("verify" if index == 0 else "", text))
    if run["usage"]:
        lines.append(("usage", _clip(json.dumps(run["usage"], ensure_ascii=False, separators=(",", ":")), RAW_CHARS)))
    if run["error"]:
        lines.append(("error", run["error"]))
    if run["evidence"]:
        lines.append(("evidence", run["evidence"]))
    blobs = [f"{name} {run[f'{name}_sha256'][:12]}" for name in ("log", "diff") if run[f"{name}_sha256"]]
    if blobs:
        lines.append(("uploaded", ", ".join(blobs)))
    lines.append(("events", f"{run['last_seq']}"))
    return lines


@_client_command
def cmd_show(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    run = hub.call("GET", _runs_path(project, args.run))
    if args.json:
        _print_json(run)
        return 0
    print(f"Run #{run['id']}")
    lines = _describe(run)
    width = max(len(label) for label, _ in lines)
    for label, text in lines:
        first, *rest = str(text).splitlines() or [""]
        print(f"  {label:<{width}}  {first}".rstrip())
        for line in rest:
            print(f"  {'':<{width}}  {line}".rstrip())
    if run["last_seq"]:
        print(f"Its events: `evo-agents hub run logs {run['id']}{_with_project(args, project)}`.")
    return 0


@_client_command
def cmd_logs(args) -> int:
    if args.json and args.follow:
        print("error: --json prints the events read as one object, so it does not go with --follow", file=sys.stderr)
        return EXIT_USAGE
    hub, _ = _signed_in()
    project = _project(args)
    after = args.after or 0
    if args.follow:
        ended = follow(hub, project, args.run, after, kinds=frozenset(args.kind or ()))
        print(f"Run #{args.run} ended {ended.get('state')}; its last event is {ended.get('last_seq')}.")
        return 0
    page = read_events(hub, project, args.run, after, args.kind or ())
    if args.json:
        _print_json(page)
        return 0
    for event in page["events"]:
        print_event(event)
    shown = f"{len(page['events'])} event(s) shown, the last of the run is {page['last_seq']}"
    hint = "" if page["state"] in TERMINAL_STATES else "; --follow prints the next ones as they come"
    print(f"Run #{args.run} is {page['state']}: {shown}{hint}.")
    return 0


@_client_command
def cmd_send(args) -> int:
    text = sys.stdin.read() if args.text == "-" else args.text
    hub, _ = _signed_in()
    message = hub.call("POST", _runs_path(_project(args), args.run, "messages"), {"text": text})
    if args.json:
        _print_json(message)
        return 0
    print(
        f"Sent message #{message['id']} to run #{message['run_id']} (event {message['seq']}); "
        "its worker hands it to the agent."
    )
    return 0


def _cancelled(run: dict) -> str:
    if run["state"] == "cancelled":
        return f"Cancelled run #{run['id']}, {_step(run)}."
    return f"Asked the worker of run #{run['id']} ({run['state']}) to stop it; it ends cancelled once the worker does."


def _approved(run: dict) -> str:
    return f"Approved run #{run['id']}: it is done, and so is {_step(run)}."


def _taken_over(run: dict) -> str:
    return (
        f"Asked the worker of run #{run['id']} for a takeover: once the agent's turn ends, the run is interactive and "
        "a person drives the agent in a terminal on the worker."
    )


def _handed_back(run: dict) -> str:
    return f"Asked the worker of run #{run['id']} to hand back: the agent goes on headless in the same session."


def _rerun(run: dict) -> str:
    return f"Queued run #{run['id']}, a rerun of run #{run['parent_run_id']}: {_step(run)}."


CONTROLS = {
    "cancel": (
        "cancel a run: a queued run or one in review at once, a held one once its worker hears of it",
        _cancelled,
    ),
    "approve": ("approve a run in review: the run and its step are done", _approved),
    "takeover": ("ask a run's worker to let a person drive its agent in a terminal on the worker", _taken_over),
    "handback": ("ask a run's worker to let its agent go on headless in the same session", _handed_back),
    "rerun": (
        "queue the step of a run that ended again, with its runtime, mode, approval, timeout and worker",
        _rerun,
    ),
}


def _control(action: str, said: Callable[[dict], str]):
    @_client_command
    def command(args) -> int:
        hub, _ = _signed_in()
        run = hub.call("POST", _runs_path(_project(args), args.run, action))
        if args.json:
            _print_json(run)
            return 0
        print(said(run))
        return 0

    command.__name__ = command.__qualname__ = f"cmd_{action}"
    return command


def register_runs(hsub) -> None:
    run = hsub.add_parser("run", help="dispatch plan steps to your workers, and follow, steer and end their runs")
    rsub = run.add_subparsers(dest="run_command", required=True)
    project_help = "hub project (default: hub.project in the harness.yaml around the current directory)"
    run_help = "the run's id, as `hub run list` shows it"

    def with_project(parser) -> None:
        parser.add_argument("--project", help=project_help)

    def with_run(parser) -> None:
        parser.add_argument("run", metavar="RUN", type=int, help=run_help)

    dispatch = rsub.add_parser(
        "dispatch", help="queue a run of each step named for a worker of yours, all of them or none (needs writer)"
    )
    dispatch.add_argument("plan", metavar="PLAN", help="the plan's id")
    dispatch.add_argument("steps", metavar="STEP", nargs="+", help="a step id (or order); one run each")
    with_project(dispatch)
    dispatch.add_argument(
        "--runtime", choices=REQUESTED_RUNTIMES, help="default: any, the first the claiming worker has"
    )
    dispatch.add_argument(
        "--mode", choices=MODES, help="default: headless; interactive starts the agent in a terminal on the worker"
    )
    dispatch.add_argument(
        "--worker", help="pin the runs to this worker of yours, by id or name (default: any of yours that can take one)"
    )
    dispatch.add_argument(
        "--approval",
        choices=APPROVALS,
        help="default: review, which waits for `hub run approve`; auto marks the step done once every verify command "
        "the worker runs again exits 0",
    )
    dispatch.add_argument("--timeout", type=int, choices=range(5, 241), metavar="MINUTES", help="5 to 240 (default 60)")
    json_option(dispatch, returns_array(*RUN_KEYS, schema="Run"))
    dispatch.set_defaults(func=cmd_dispatch)

    listed = rsub.add_parser("list", help="the runs of a project, newest first, with how many are in each state")
    with_project(listed)
    listed.add_argument(
        "--state",
        action="append",
        choices=RUN_STATES,
        metavar="STATE",
        help=f"only runs in this state, one of {', '.join(RUN_STATES)}; repeat it for several",
    )
    listed.add_argument("--plan", help="only runs of this plan")
    listed.add_argument("--step", help="only runs of this step key")
    listed.add_argument("--worker", help="only runs this worker of yours claimed, by id or name")
    listed.add_argument("--by", metavar="LOGIN", help="only runs this member dispatched")
    listed.add_argument(
        "--search",
        metavar="TEXT",
        help="text in the title, step, plan, repo, branch, worker, login or error, or a run number such as #12",
    )
    listed.add_argument("--limit", type=int, choices=range(1, 201), metavar="N", help="1 to 200 (default 50)")
    listed.add_argument("--offset", type=int, metavar="N", help="skip this many runs, for the next page")
    json_option(listed, returns_object(*RUN_LIST_KEYS, schema="RunList"))
    listed.set_defaults(func=cmd_list)

    show = rsub.add_parser("show", help="one run: state, step, worker, commit, verify results, evidence")
    with_run(show)
    with_project(show)
    json_option(show, RUN)
    show.set_defaults(func=cmd_show)

    logs = rsub.add_parser(
        "logs", help="a run's events: what its agent said and did, and its moves; --follow prints them as they come"
    )
    with_run(logs)
    with_project(logs)
    logs.add_argument(
        "-f", "--follow", action="store_true", help="keep reading the run's live stream until the run ends"
    )
    logs.add_argument("--after", type=int, metavar="SEQ", help="only the events after this one (default 0)")
    logs.add_argument(
        "--kind",
        action="append",
        choices=EVENT_KINDS,
        metavar="KIND",
        help=f"only events of this kind, one of {', '.join(EVENT_KINDS)}; repeat it for several",
    )
    json_option(
        logs,
        returns_object(*EVENTS_KEYS, schema="RunEvents"),
        help="the events as one object, as the hub answers a page of them; not with --follow",
    )
    logs.set_defaults(func=cmd_logs)

    send = rsub.add_parser("send", help="leave a message for a run's agent, which its worker hands over (owner only)")
    with_run(send)
    send.add_argument("text", metavar="TEXT", help="at most 8 KiB; - reads it from stdin")
    with_project(send)
    json_option(send, returns_object(*MESSAGE_KEYS, schema="Message"))
    send.set_defaults(func=cmd_send)

    for action, (help_text, said) in CONTROLS.items():
        parser = rsub.add_parser(action, help=f"{help_text} (owner only)")
        with_run(parser)
        with_project(parser)
        json_option(parser, RUN)
        parser.set_defaults(func=_control(action, said))
