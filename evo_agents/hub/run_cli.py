"""``evo-agents hub run``: dispatch plan steps to your own workers, and follow, steer and end their runs.

``dispatch`` queues one run per step named (POST /v1/projects/{p}/runs), all of them or none, ``plan`` queues a
plan run, one run that does every step of the plan not done yet (POST /v1/projects/{p}/plan-runs), and ``author``
queues an author run, a plan written from your request with the create-exec-plan skill on a worker of yours (POST
/v1/projects/{p}/author-runs, ``evo_agents.hub.author``), or with ``--plan`` a new revision of a plan of the
project. ``list`` and ``show`` read runs of every kind. ``logs``
prints a run's events page by page (GET .../runs/{id}/events?after=SEQ), or with ``--follow`` reads the run's
server-sent events (GET .../runs/{id}/stream) until the hub sends ``end``. ``send`` leaves a message for the run's
agent, and ``cancel``, ``approve``, ``takeover``, ``handback`` and ``rerun`` are the owner's controls.
``credentials`` lists the leases the run got of its owner's secrets and of the hub's GitHub App (GET
.../runs/{id}/credentials, the owner only), never their values. Every command after ``dispatch``, ``plan``,
``author`` and ``list`` takes the id of a run, as ``list`` shows it. A refusal is the hub's message on stderr, such
as why a worker whose owner set it to take runs dispatched from the web only refuses a run dispatched with a token.
``docs/workers.md`` describes the protocol behind them; ``--json`` prints what the hub answered, with the keys declared
next to the flag.

``--model`` names a model as one runtime names it (opencode: provider/model), so ``dispatch`` and ``plan`` take it
only with ``--runtime`` naming that runtime: with ``any`` the claiming worker picks the runtime, and the model might
not be one it knows. The hub itself takes a model with any runtime; this check is the command line's.

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
from datetime import UTC, datetime
from urllib.parse import urlencode

from evo_agents import __version__
from evo_agents.hub.author import AUTHOR_RUNTIMES, AUTHOR_TIMEOUT_CHOICES, DEFAULT_TIMEOUT_H, MAX_REQUEST_BYTES
from evo_agents.hub.cli_client import _client_command, _print_json, _project_path, _signed_in, _table, _when
from evo_agents.hub.client import _OPENER, HubError, Unreachable, _json, _origin
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.plan_cli import _plan_id, _project
from evo_agents.hub.runs import (
    APPROVALS,
    EVENT_KINDS,
    MAX_MODEL_CHARS,
    MODES,
    PLAN_TIMEOUT_CHOICES,
    RUN_STATES,
    RUNTIMES,
    TERMINAL_STATES,
)

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
    "dispatched_via",
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
    "resume_of_run_id",
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
    "budget",
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
    "waiting_since",
    "parked_at",
    "finished_at",
    "request",
)
RUN_LIST_KEYS = ("runs", "total", "counts", "limit", "offset")
EVENTS_KEYS = ("run_id", "state", "last_seq", "events", "more")
MESSAGE_KEYS = ("id", "run_id", "seq", "text", "sent_by", "created_at", "delivered_at")
LEASE_KEYS = ("id", "name", "provider", "kind", "target", "worker", "issued_at", "expires_at", "revoked_at")
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
    """What a run works on: ``step 2 (Title) of plan P``, ``plan P (Title)`` for a plan run, or ``review (Title)`` for
    a review run, which works on no plan."""
    title = f" ({run['title']})" if run.get("title") else ""
    if run.get("kind") == "review":
        return f"review{title}"
    if run.get("kind") == "author":
        return f"author{title}" + (f" of plan {run['plan_id']}" if run.get("plan_id") else "")
    if run.get("kind") == "plan":
        return f"plan {run['plan_id']}{title}"
    return f"step {run['step_key']}{title} of plan {run['plan_id']}"


def _repos(run: dict) -> str:
    """A plan run's repos, each with the branch the plan names for it."""
    return ", ".join(
        entry["repo"] + (f" ({entry['branch']})" if entry.get("branch") else "") for entry in run.get("repos") or ()
    )


def _timeout(run: dict) -> str:
    minutes = run["timeout_min"]
    hours = run.get("kind") in ("plan", "author") and minutes % 60 == 0
    return f"{minutes // 60} h" if hours else f"{minutes} min"


def _model_check(args) -> str | None:
    """Why ``--model`` cannot go with the ``--runtime`` given, or None."""
    if args.model is None:
        return None
    if args.runtime in (None, "any"):
        return (
            "--model names a model as one runtime names it, so it needs --runtime with that runtime "
            f"({', '.join(RUNTIMES)}): with any, the claiming worker picks the runtime"
        )
    if not args.model.strip() or len(args.model) > MAX_MODEL_CHARS or not args.model.isprintable():
        return f"--model takes one line of 1 to {MAX_MODEL_CHARS} characters"
    return None


def _usage_error(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _with_project(args, project: str) -> str:
    """The --project to repeat in a command printed for the person, when the harness did not give the project."""
    return f" --project {project}" if args.project else ""


# Events


def _clock(value) -> str:
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


def _runtime(run: dict) -> str:
    """The runtime a run asked for, with its model when it named one."""
    return run["requested_runtime"] + (f" (model {run['model']})" if run.get("model") else "")


@_client_command
def cmd_dispatch(args) -> int:
    refused = _model_check(args)
    if refused:
        return _usage_error(refused)
    hub, credentials = _signed_in()
    project = _project(args)
    body = {"plan_id": _plan_id(args.plan), "steps": args.steps}
    chosen = {
        "runtime": args.runtime,
        "model": args.model,
        "mode": args.mode,
        "approval": args.approval,
        "timeout_min": args.timeout,
    }
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
            f"Queued run #{run['id']}: {_step(run)}, repo {run['repo']}; runtime {_runtime(run)}, "
            f"{run['mode']}, approval {run['approval']}, timeout {run['timeout_min']} min{pinned}."
        )
    print(f"Follow it with `evo-agents hub run logs {queued[0]['id']} --follow{_with_project(args, project)}`.")
    return 0


@_client_command
def cmd_plan(args) -> int:
    refused = _model_check(args)
    if refused:
        return _usage_error(refused)
    hub, credentials = _signed_in()
    project = _project(args)
    body = {"plan_id": _plan_id(args.plan)}
    chosen = {"runtime": args.runtime, "model": args.model, "mode": args.mode, "timeout_h": args.timeout_h}
    body.update({key: value for key, value in chosen.items() if value is not None})
    if args.worker is not None:
        body["worker_id"] = _worker_id(hub, credentials.login, args.worker)
    run = hub.call("POST", f"{_project_path(project)}/plan-runs", body)
    if args.json:
        _print_json(run)
        return 0
    pinned = f", on worker #{run['pinned_worker_id']} only" if run["pinned_worker_id"] else ""
    print(
        f"Queued plan run #{run['id']}: {_step(run)}, every step not done yet, in {_repos(run)}; runtime "
        f"{_runtime(run)}, {run['mode']}, timeout {_timeout(run)} of agent time{pinned}."
    )
    where = _with_project(args, project)
    print(
        f"Follow it with `evo-agents hub run logs {run['id']} --follow{where}`; the decisions its agent asks you: "
        f"`evo-agents hub decision list --run {run['id']}{where}`."
    )
    return 0


@_client_command
def cmd_author(args) -> int:
    text = sys.stdin.read() if args.request == "-" else args.request
    if not text.strip():
        return _usage_error("the request is empty: say what the plan should achieve")
    if len(text.encode()) > MAX_REQUEST_BYTES:
        return _usage_error(
            f"the request is {len(text.encode())} bytes of UTF-8, over the {MAX_REQUEST_BYTES} it takes"
        )
    hub, credentials = _signed_in()
    project = _project(args)
    body = {"request": text, "worker_id": _worker_id(hub, credentials.login, args.worker)}
    chosen = {"plan_id": args.plan, "runtime": args.runtime, "model": args.model, "timeout_h": args.timeout_h}
    body.update({key: value for key, value in chosen.items() if value is not None})
    run = hub.call("POST", f"{_project_path(project)}/author-runs", body)
    if args.json:
        _print_json(run)
        return 0
    print(
        f"Queued author run #{run['id']}: {_step(run)}, on worker #{run['pinned_worker_id']} over {_repos(run)}; "
        f"runtime {_runtime(run)}, timeout {_timeout(run)} of agent time. It reads only and pushes nothing."
    )
    print(f"Follow it with `evo-agents hub run logs {run['id']} --follow{_with_project(args, project)}`.")
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
            run["kind"],
            run["state"],
            run["plan_id"],
            run["step_key"] or "-",
            _clip(run["title"] or "-", TITLE_CHARS),
            run["runtime"],
            run["worker"] or "-",
            run["dispatched_by"],
            _when(run["queued_at"]),
        )
        for run in listed["runs"]
    ]
    if rows:
        _table(("RUN", "KIND", "STATE", "PLAN", "STEP", "TITLE", "RUNTIME", "WORKER", "BY", "QUEUED (UTC)"), rows)
    counts = ", ".join(f"{count} {state}" for state, count in listed["counts"].items() if count)
    shown = len(rows)
    print(f"{shown} of {listed['total']} run(s) of project {project}" + (f"; by state: {counts}" if counts else ""))
    if listed["offset"] + shown < listed["total"]:
        print(f"More with --offset {listed['offset'] + shown}.")
    return 0


def _describe(run: dict) -> list[tuple[str, str]]:
    """The fields of a run that say something, as (label, text) pairs."""
    plan_run = run["kind"] in ("plan", "review", "judge", "author")  # these have repos too, and no step
    lines = [("state", f"{run['state']}, attempt {run['attempt']} of {run['max_attempts']}")]
    if run["kind"] == "review":
        lines.append(("kind", "review run: the Curator reads the project and proposes changes; it pushes nothing"))
        if run["title"]:
            lines.append(("title", run["title"]))
        lines.append(("project", run["project"]))
    elif run["kind"] == "judge":
        lines.append(
            ("kind", "judge run: the Curator's Judge reads a change and says whether it passes; it pushes nothing")
        )
        if run["title"]:
            lines.append(("title", run["title"]))
    elif run["kind"] == "author":
        lines.append(("kind", "author run: a plan written from your request with create-exec-plan; it pushes nothing"))
        if run["title"]:
            lines.append(("title", run["title"]))
    elif plan_run:
        lines.append(("kind", "plan run: every step of the plan not done yet, in one session"))
        if run["title"]:
            lines.append(("title", run["title"]))
    else:
        lines.append(("step", f"{run['step_key']}" + (f": {run['title']}" if run["title"] else "")))
    if run["kind"] not in ("review", "author") or run.get("plan_id"):
        lines.append(("plan", f"{run['plan_id']} of project {run['project']}, revision {run['plan_revision']}"))
    commit = f", commit {run['commit_sha'][:12]}" if run["commit_sha"] else ""
    if plan_run:
        lines.append(("repos", (_repos(run) or "none") + commit))
    else:
        lines.append(("repo", run["repo"] + (f", branch {run['branch']}" if run["branch"] else "") + commit))
    worker = f"{run['worker']} (#{run['worker_id']})" if run["worker_id"] else "none yet"
    if run["pinned_worker_id"]:
        worker += f", pinned to worker #{run['pinned_worker_id']}"
    runtime = run["runtime"] + (
        f" (asked for {run['requested_runtime']})" if run["runtime"] != run["requested_runtime"] else ""
    )
    runtime += f", model {run['model']}" if run["model"] else ""
    approval = "" if plan_run else f", approval {run['approval']}"
    lines += [
        ("worker", worker),
        ("runtime", f"{runtime}, {run['mode']}{approval}, timeout {_timeout(run)}"),
        ("dispatched", f"by {run['dispatched_by']} at {_when(run['queued_at'])} UTC"),
    ]
    if run["run_seconds"]:
        lines.append(("agent time", f"{run['run_seconds'] // 60} min used of {_timeout(run)}"))
    times = [(name, run[f"{name}_at"]) for name in ("leased", "started", "finished") if run[f"{name}_at"]]
    if times:
        lines.append(("times (UTC)", ", ".join(f"{name} {_when(value)}" for name, value in times)))
    if run["waiting_since"] and run["state"] == "waiting":
        lines.append(("waiting", f"since {_when(run['waiting_since'])} UTC, for the answer to a decision"))
    if run["parked_at"]:
        lines.append(("parked", f"at {_when(run['parked_at'])} UTC, for want of an answer"))
    if run["resume_of_run_id"]:
        lines.append(("resumes", f"run #{run['resume_of_run_id']}, in its session and worktrees"))
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
    if run.get("request"):
        lines.append(("request", run["request"]))
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
    if run["kind"] == "plan":
        print(f"Its decisions: `evo-agents hub decision list --run {run['id']}{_with_project(args, project)}`.")
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


def _lease_state(lease: dict, now: datetime) -> str:
    """``revoked YYYY-MM-DD HH:MM`` (UTC), ``expired``, or ``out`` for a lease the run still holds."""
    if lease["revoked_at"]:
        return f"revoked {_when(lease['revoked_at'])}"
    try:
        ends = datetime.fromisoformat(lease["expires_at"]) if lease["expires_at"] else None
    except ValueError:
        ends = None
    if ends is not None and (ends if ends.tzinfo else ends.replace(tzinfo=UTC)) <= now:
        return "expired"
    return "out"


@_client_command
def cmd_credentials(args) -> int:
    hub, _ = _signed_in()
    leases = hub.call("GET", _runs_path(_project(args), args.run, "credentials"))
    if args.json:
        _print_json(leases)
        return 0
    if not leases:
        print(f"Run #{args.run} got no lease: its worker asked for none, or the hub had nothing for it.")
        return 0
    now = datetime.now(UTC)
    rows = [
        (
            lease["name"],
            lease["provider"],
            lease["target"],
            lease["worker"],
            _when(lease["issued_at"]),
            _when(lease["expires_at"]),
            _lease_state(lease, now),
        )
        for lease in leases
    ]
    _table(("NAME", "PROVIDER", "TARGET", "WORKER", "ISSUED (UTC)", "EXPIRES (UTC)", "STATE"), rows)
    out = sum(row[-1] == "out" for row in rows)
    print(
        f"{len(leases)} lease(s) of run #{args.run}, "
        + (f"{out} still out." if out else "none still out: each was given back, taken back or ended.")
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
    run = hsub.add_parser(
        "run", help="dispatch plan steps, or a whole plan, to your workers, and follow, steer and end their runs"
    )
    rsub = run.add_subparsers(dest="run_command", required=True)
    project_help = "hub project (default: hub.project in the harness.yaml around the current directory)"
    run_help = "the run's id, as `hub run list` shows it"

    def with_project(parser) -> None:
        parser.add_argument("--project", help=project_help)

    def with_run(parser) -> None:
        parser.add_argument("run", metavar="RUN", type=int, help=run_help)

    def with_model(parser) -> None:
        parser.add_argument(
            "--model",
            help="the model, as the runtime names it (opencode: provider/model); needs --runtime with that runtime "
            "(default: the runtime's own choice)",
        )

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
    with_model(dispatch)
    json_option(dispatch, returns_array(*RUN_KEYS, schema="Run"))
    dispatch.set_defaults(func=cmd_dispatch)

    plan = rsub.add_parser(
        "plan",
        help="queue a plan run: one run, on a worker of yours, that does every step of the plan not done yet "
        "(needs writer)",
    )
    plan.add_argument("plan", metavar="PLAN", help="the plan's id")
    with_project(plan)
    plan.add_argument(
        "--worker", help="pin the run to this worker of yours, by id or name (default: any of yours with every repo)"
    )
    plan.add_argument("--runtime", choices=REQUESTED_RUNTIMES, help="default: any, the first the claiming worker has")
    with_model(plan)
    plan.add_argument(
        "--mode", choices=MODES, help="default: headless; interactive starts the agent in a terminal on the worker"
    )
    plan.add_argument(
        "--timeout-h",
        type=int,
        choices=PLAN_TIMEOUT_CHOICES,
        metavar="HOURS",
        help=f"hours of agent time, one of {', '.join(map(str, PLAN_TIMEOUT_CHOICES))} (default 4); waiting for a "
        "decision and parked do not count",
    )
    json_option(plan, RUN)
    plan.set_defaults(func=cmd_plan)

    authored = rsub.add_parser(
        "author",
        help="queue an author run: a plan written from your request with the create-exec-plan skill, on a worker of "
        "yours (needs writer)",
    )
    authored.add_argument(
        "request",
        metavar="REQUEST",
        help=f"what the plan should achieve, at most {MAX_REQUEST_BYTES // 1024} KiB of UTF-8; - reads it from stdin",
    )
    with_project(authored)
    authored.add_argument(
        "--plan",
        metavar="PLAN",
        help="the plan to revise, one you can read; the agent puts it back with its revision (default: a new plan)",
    )
    authored.add_argument(
        "--worker",
        required=True,
        help="the worker of yours to run it on, by id or name; it needs the harness checked out",
    )
    authored.add_argument(
        "--runtime",
        choices=REQUESTED_RUNTIMES,
        help=f"default: {AUTHOR_RUNTIMES[0]}, the one runtime an author run takes; the hub refuses another",
    )
    authored.add_argument("--model", help="the model, as Claude Code names it (default: Claude Code's own choice)")
    authored.add_argument(
        "--timeout-h",
        type=int,
        choices=AUTHOR_TIMEOUT_CHOICES,
        metavar="HOURS",
        help=f"hours of agent time, one of {', '.join(map(str, AUTHOR_TIMEOUT_CHOICES))} (default {DEFAULT_TIMEOUT_H})",
    )
    json_option(authored, RUN)
    authored.set_defaults(func=cmd_author)

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

    credentials = rsub.add_parser(
        "credentials",
        help="the leases a run got: secret or GitHub App, target, worker, when issued, ending and revoked; never the "
        "value (owner only)",
    )
    with_run(credentials)
    with_project(credentials)
    json_option(credentials, returns_array(*LEASE_KEYS, schema="RunLease"))
    credentials.set_defaults(func=cmd_credentials)

    for action, (help_text, said) in CONTROLS.items():
        parser = rsub.add_parser(action, help=f"{help_text} (owner only)")
        with_run(parser)
        with_project(parser)
        json_option(parser, RUN)
        parser.set_defaults(func=_control(action, said))
