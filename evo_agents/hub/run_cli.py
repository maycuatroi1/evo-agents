"""``evo-agents hub run``: dispatch plan steps to your own workers, and follow, steer and end their runs.

``dispatch`` queues one run per step named (POST /v1/projects/{p}/runs), all of them or none, ``plan`` queues a plan
run, one run that does every step of the plan not done yet (POST /v1/projects/{p}/plan-runs), in one agent session or
with ``--session-per-step`` in a new session for each step, or with ``--system SLUG`` hands the plan to an agent
system of the project instead (POST /v1/projects/{p}/plans/{id}/system-dispatch, ``run_cli_options``), and
``author`` queues an author run, a plan written from your request with the create-exec-plan skill on the pool of its
agent (POST /v1/projects/{p}/author-runs, ``evo_agents.hub.author``), or with ``--plan`` a new revision of a plan of
the project. ``list`` and ``show`` read runs
of every kind, the chat runs of group chats among them. ``logs`` prints a run's events page by page (GET
.../runs/{id}/events?after=SEQ), or with ``--follow`` reads the run's server-sent events (GET .../runs/{id}/stream)
until the hub sends ``end``; ``watch`` waits until a run's state, an open decision or a plan's step changes
(``run_cli_watch``). ``send`` leaves a message for the run's agent, files too with ``--attach``
(``run_cli_send``), and ``cancel``, ``approve``, ``takeover``, ``handback``, ``rerun`` and ``access`` are the owner's
controls (``access`` in ``run_cli_access``); ``outputs`` lists the files its agent made (``run_cli_outputs``).
``credentials`` lists the run's leases of its owner's secrets and the hub's GitHub App, the owner only, never a
value. Every command after
``dispatch``, ``plan``, ``author`` and ``list`` takes the id of a run, as ``list`` shows it. None of them picks the
machine: an agent in pool mode runs on its own pool, and the hub refuses a worker, pool, label or taint for it. A refusal is the hub's
message on stderr, such as why a worker whose owner set it to take runs dispatched from the web only refuses a run
dispatched with a token. ``dispatch``, ``plan``, ``author`` and ``rerun`` take ``--account``, an account picked by hand,
and all but ``rerun`` ``--agent``, an agent profile (``evo_agents.hub.run_cli_options``). ``docs/workers.md`` describes
the protocol behind them; ``--json`` prints what the hub answered, with the keys declared next to the flag.

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
from evo_agents.hub.attach_cli import describe
from evo_agents.hub.author import MAX_REQUEST_BYTES
from evo_agents.hub.chats import chat_link
from evo_agents.hub.cli_client import client_command, print_json, project_path, signed_in, table, when
from evo_agents.hub.client import OPENER, HubError, Unreachable, json_body, origin
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.plan_cli import checked_plan_id, project_of_args
from evo_agents.hub.run_cli_access import register_access
from evo_agents.hub.run_cli_candidates import register_candidates
from evo_agents.hub.run_cli_keys import RUN_KEYS
from evo_agents.hub.run_cli_options import (
    account_option,
    as_me_body,
    author_options,
    browser_body,
    dispatch_options,
    kind_text,
    list_options,
    plan_options,
    rerun_body,
    rerun_options,
    system_conflicts,
    usage_error,
)
from evo_agents.hub.run_cli_outputs import register_outputs
from evo_agents.hub.run_cli_send import register_send
from evo_agents.hub.run_cli_spend import spend_lines, usd
from evo_agents.hub.run_cli_watch import register_watch
from evo_agents.hub.run_cli_workers import worker_id as _worker_id
from evo_agents.hub.runs import (
    EVENT_KINDS,
    MAX_MODEL_CHARS,
    RUNTIMES,
    STEADY_GAP_SECONDS,
    STEADY_SECONDS,
    TERMINAL_STATES,
)
from evo_agents.hub.sse import sse_messages

EXIT_USAGE = 2
EVENTS_PAGE = 1000  # the most events GET .../events answers at once
STREAM_TIMEOUT = 60.0  # seconds without a byte, pings included, before a followed stream counts as dropped
RECONNECTS = 5  # tries in a row to open a followed stream again that bring no event before giving up
BACKOFF = (1, 2, 4, 8, 15)  # seconds before each of those tries
RETRIED_STATUSES = frozenset({502, 503, 504})  # a proxy in front of a hub that restarts: worth another try
TITLE_CHARS = 40  # of a step's title in the run table
RAW_CHARS = 300  # of an event body shown as JSON
PROJECT_HELP = "hub project (default: hub.project in the harness.yaml around the current directory)"

# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
RUN_LIST_KEYS = ("runs", "total", "counts", "limit", "offset")
EVENTS_KEYS = ("run_id", "state", "last_seq", "events", "more")
CHAT_KEYS = ("run_id", "state", "status", "owner", "plan_id", "plan_revision", "messages", "more")
LEASE_KEYS = ("id", "name", "provider", "kind", "target", "worker", "issued_at", "expires_at", "revoked_at")
LEASE_KEYS += ("profile", "profile_id", "profile_version")
RUN = returns_object(*RUN_KEYS, schema="Run")
DISPATCHED_KEYS = ("system", "system_revision", "trace_id", "plan_id", "plan_revision", "chat_id", "message_id")
DISPATCHED_KEYS += ("run_id",)
PLAN_RUN = returns_object(
    *RUN_KEYS,
    schema="Run",
    variants=(("--system", returns_object(*DISPATCHED_KEYS, schema="SystemDispatched")),),
)

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
    path = f"{project_path(project)}/runs"
    if run_id is not None:
        path += f"/{run_id}"
    return path + (f"/{action}" if action else "")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


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


def _author_on(run: dict) -> str:
    """Where an author run goes: its pinned worker, or any its selector fits (an agent in pool mode: its pool)."""
    if run.get("pinned_worker_id"):
        return f"on worker #{run['pinned_worker_id']}"
    asked = run.get("selector") or []
    return f"on a worker with {', '.join(asked)}" if asked else "on any worker of yours that can take it"


def _repos(run: dict) -> str:
    """A plan run's repos, each with the branch the plan names for it."""
    return ", ".join(
        entry["repo"] + (f" ({entry['branch']})" if entry.get("branch") else "") for entry in run.get("repos") or ()
    )


def _timeout(run: dict) -> str:
    minutes = run["timeout_min"]
    hours = run.get("kind") in ("plan", "author") and minutes % 60 == 0
    return f"{minutes // 60} h" if hours else f"{minutes} min"


def _warn(run: dict, args, project: str) -> None:
    """On stderr, the warning the hub gave with a run it queued: no worker can take it now, and why."""
    if run.get("warning"):
        where = _with_project(args, project)
        print(f"Warning: {run['warning']}. More: `evo-agents hub run candidates {run['id']}{where}`.", file=sys.stderr)


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


# Commands


def _as_you(run: dict) -> str:
    """What a queued run says of ``as_owner``: nothing for a run that does not run as its owner."""
    return "; it runs as you, secrets, tokens and workers aside" if run.get("as_owner") else ""


def _runtime(run: dict) -> str:
    """The runtime a run asked for, with its model when it named one."""
    return run["requested_runtime"] + (f" (model {run['model']})" if run.get("model") else "")


@client_command
def cmd_dispatch(args) -> int:
    refused = _model_check(args)
    if refused:
        return usage_error(refused)
    hub, _ = signed_in()
    project = project_of_args(args)
    body = {"plan_id": checked_plan_id(args.plan), "steps": args.steps, **browser_body(args)}
    body.update(as_me_body(args))
    chosen = {
        "runtime": args.runtime,
        "model": args.model,
        "mode": args.mode,
        "approval": args.approval,
        "timeout_min": args.timeout,
        "account": args.account,
    }
    body.update({key: value for key, value in {**chosen, "profile": args.agent}.items() if value is not None})
    queued = hub.call("POST", _runs_path(project), body)
    if args.json:
        print_json(queued)
        return 0
    for run in queued:
        pinned = f", on worker #{run['pinned_worker_id']} only" if run["pinned_worker_id"] else ""
        print(
            f"Queued run #{run['id']}: {_step(run)}, repo {run['repo']}; runtime {_runtime(run)}, "
            f"{run['mode']}, approval {run['approval']}, timeout {run['timeout_min']} min{pinned}{_as_you(run)}."
        )
        _warn(run, args, project)
    print(f"Follow it with `evo-agents hub run logs {queued[0]['id']} --follow{_with_project(args, project)}`.")
    return 0


@client_command
def cmd_plan(args) -> int:
    conflicts = system_conflicts(args)
    if conflicts:
        return usage_error(
            f"--system hands the plan to the system, whose agents run as it sets them, so it does not go with "
            f"{', '.join(conflicts)}"
        )
    if args.system is not None:
        return _plan_in_system(args)
    refused = _model_check(args)
    if refused:
        return usage_error(refused)
    hub, _ = signed_in()
    project = project_of_args(args)
    body = {"plan_id": checked_plan_id(args.plan), **browser_body(args), **as_me_body(args)}
    each = True if args.session_per_step else None  # sent only when asked: an older hub would ignore the field
    chosen = {"runtime": args.runtime, "model": args.model, "mode": args.mode, "timeout_h": args.timeout_h}
    chosen.update(account=args.account, profile=args.agent)
    body.update({key: value for key, value in {**chosen, "session_per_step": each}.items() if value is not None})
    run = hub.call("POST", f"{project_path(project)}/plan-runs", body)
    if args.json:
        print_json(run)
        return 0
    pinned = f", on worker #{run['pinned_worker_id']} only" if run["pinned_worker_id"] else ""
    each = ", each in a new session" if run.get("session_per_step") else ""
    print(
        f"Queued plan run #{run['id']}: {_step(run)}, every step not done yet{each}, in {_repos(run)}; runtime "
        f"{_runtime(run)}, {run['mode']}, timeout {_timeout(run)} of agent time{pinned}{_as_you(run)}."
    )
    _warn(run, args, project)
    where = _with_project(args, project)
    print(
        f"Follow it with `evo-agents hub run logs {run['id']} --follow{where}`; the decisions its agent asks you: "
        f"`evo-agents hub decision list --run {run['id']}{where}`."
    )
    return 0


def _plan_in_system(args) -> int:
    """``hub run plan PLAN --system SLUG``: hand the plan to the system's entry agent (POST .../system-dispatch)."""
    hub, _ = signed_in()
    project = project_of_args(args)
    plan_id = checked_plan_id(args.plan)
    path = f"{project_path(project)}/plans/{plan_id}/system-dispatch"
    handed = hub.call("POST", path, {"system": args.system})
    if args.json:
        print_json(handed)
        return 0
    link = origin(hub.url) + chat_link(handed["chat_id"], handed["message_id"])
    print(
        f"Handed plan {handed['plan_id']} (revision {handed['plan_revision']}) to system {handed['system']} "
        f"(revision {handed['system_revision']}): trace #{handed['trace_id']}, run #{handed['run_id']} of its entry "
        f"agent, in your chat #{handed['chat_id']} with it ({link})."
    )
    print(f"Follow it with `evo-agents hub run logs {handed['run_id']} --follow{_with_project(args, project)}`.")
    return 0


@client_command
def cmd_author(args) -> int:
    text = sys.stdin.read() if args.request == "-" else args.request
    if not text.strip():
        return usage_error("the request is empty: say what the plan should achieve")
    if len(text.encode()) > MAX_REQUEST_BYTES:
        return usage_error(
            f"the request is {len(text.encode())} bytes of UTF-8, over the {MAX_REQUEST_BYTES} it takes",
        )
    hub, _ = signed_in()
    project = project_of_args(args)
    body = {"request": text}
    chosen = {"plan_id": args.plan, "runtime": args.runtime, "model": args.model, "timeout_h": args.timeout_h}
    chosen.update(account=args.account, profile=args.agent)
    body.update({key: value for key, value in chosen.items() if value is not None})
    run = hub.call("POST", f"{project_path(project)}/author-runs", body)
    if args.json:
        print_json(run)
        return 0
    print(
        f"Queued author run #{run['id']}: {_step(run)}, {_author_on(run)} over {_repos(run)}; "
        f"runtime {_runtime(run)}, timeout {_timeout(run)} of agent time. It reads only and pushes nothing."
    )
    _warn(run, args, project)
    print(f"Follow it with `evo-agents hub run logs {run['id']} --follow{_with_project(args, project)}`.")
    return 0


@client_command
def cmd_list(args) -> int:
    hub, credentials = signed_in()
    project = project_of_args(args)
    query = [("state", state) for state in args.state or ()]
    if args.plan is not None:
        query.append(("plan_id", checked_plan_id(args.plan)))
    if args.worker is not None:
        query.append(("worker_id", _worker_id(hub, credentials.login, args.worker)))
    given = {
        "step": args.step,
        "dispatched_by": args.by,
        "kind": args.kind,
        "account": args.account,
        "q": args.search,
        "limit": args.limit,
        "offset": args.offset,
    }
    query += [(key, value) for key, value in given.items() if value is not None]
    listed = hub.call("GET", _runs_path(project) + (f"?{urlencode(query)}" if query else ""))
    if args.json:
        print_json(listed)
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
            when(run["queued_at"]),
            usd((run.get("spend") or {}).get("cost_usd")),
        )
        for run in listed["runs"]
    ]
    if rows:
        heads = ("RUN", "KIND", "STATE", "PLAN", "STEP", "TITLE", "RUNTIME", "WORKER", "BY", "QUEUED (UTC)", "COST")
        table(heads, rows)
    counts = ", ".join(f"{count} {state}" for state, count in listed["counts"].items() if count)
    shown = len(rows)
    print(f"{shown} of {listed['total']} run(s) of project {project}" + (f"; by state: {counts}" if counts else ""))
    if listed["offset"] + shown < listed["total"]:
        print(f"More with --offset {listed['offset'] + shown}.")
    return 0


def _what(run: dict, hub_url: str) -> list[tuple[str, str]]:
    """What the run does: its kind and title, or its step; its project or plan; a chat run's chat and message."""
    kind, plan = run["kind"], f"{run['plan_id']} of project {run['project']}, revision {run['plan_revision']}"
    if kind == "step":
        return [("step", f"{run['step_key']}" + (f": {run['title']}" if run["title"] else "")), ("plan", plan)]
    each = "a new session for each step" if run.get("session_per_step") else "one session"
    said = kind_text(run) or f"plan run: every step of the plan not done yet, in {each}"
    lines = [("kind", said)] + ([("title", run["title"])] if run["title"] else [])
    if kind == "review":
        lines.append(("project", run["project"]))
    if kind == "chat":
        link = origin(hub_url) + chat_link(run["chat_id"], run["chat_message_id"]) if run.get("chat_id") else "-"
        lines.append(("chat", f"#{run.get('chat_id')}, message #{run.get('chat_message_id')}, hop {run.get('hop')}"))
        lines.append(("message", link))
    if kind not in ("review", "author", "chat", "task", "local", "judge", "command") or run.get("plan_id"):
        lines.append(("plan", plan))
    return lines


def _describe_local(run: dict, hub_url: str) -> list[tuple[str, str]]:
    """A local run has no worker, repos, attempts or lease: its machine, directory and account, and what it did."""
    lines = [("state", run["state"]), *_what(run, hub_url)]
    where = f"{run['machine']}, {run['cwd']}"
    lines += [
        ("machine", where),
        ("account", run["claude_email"] + (f" ({run['claude_profile']})" if run["claude_profile"] else "")),
    ]
    lines.append(("dispatched", f"by {run['dispatched_by']} at {when(run['queued_at'])} UTC"))
    if run["last_activity_at"]:
        lines.append(("last activity", f"{when(run['last_activity_at'])} UTC"))
    if run["finished_at"]:
        lines.append(("ended", f"{when(run['finished_at'])} UTC"))
    if run["waiting_since"] and run["state"] == "waiting":
        lines.append(("waiting", f"since {when(run['waiting_since'])} UTC, for the answer to a decision"))
    if run.get("overlap_wait") and run["state"] == "queued":
        wait = run["overlap_wait"]
        who = ", ".join(f"#{number}" for number in wait["run_ids"])
        lines.append(("waits for", f"run {who} to end: it edited {', '.join(wait['paths'])}, which this step names"))
    if run["session_id"]:
        lines.append(("session", run["session_id"]))
    lines.append(("events", f"{run['last_seq']}"))
    return lines


def _describe(run: dict, hub_url: str = "") -> list[tuple[str, str]]:
    """The fields of a run that say something, as (label, text) pairs; ``_describe_local`` says those of a local run."""
    lines = [("state", f"{run['state']}, attempt {run['attempt']} of {run['max_attempts']}"), *_what(run, hub_url)]
    lines += _placement(run)
    lines += _moves(run)
    lines += _results(run)
    lines.append(("events", f"{run['last_seq']}"))
    return lines


def _placement(run: dict) -> list[tuple[str, str]]:
    """Where and how the run works: its repos or repo with the commit, its worker, its runtime, who dispatched it."""
    plan_run = run["kind"] != "step"  # every other kind has repos, and no step
    commit = f", commit {run['commit_sha'][:12]}" if run["commit_sha"] else ""
    if plan_run:
        lines = [("repos", (_repos(run) or "none") + commit)]
    else:
        lines = [("repo", run["repo"] + (f", branch {run['branch']}" if run["branch"] else "") + commit)]
    worker = f"{run['worker']} (#{run['worker_id']})" if run["worker_id"] else "none yet"
    if run["pinned_worker_id"]:
        worker += f", pinned to worker #{run['pinned_worker_id']}"
    runtime = run["runtime"] + (
        f" (asked for {run['requested_runtime']})" if run["runtime"] != run["requested_runtime"] else ""
    )
    runtime += f", model {run['model']}" if run["model"] else ""
    approval = "" if plan_run else f", approval {run['approval']}"
    runtime = f"{runtime}, {run['mode']}{approval}, timeout {_timeout(run)}"
    if run["kind"] == "command":  # its runtime and mode are of record only: the machine's shell runs the command
        runtime = f"none, the machine's shell runs the command, timeout {_timeout(run)}"
    return lines + [
        ("worker", worker),
        ("runtime", runtime),
        ("dispatched", f"by {run['dispatched_by']} at {when(run['queued_at'])} UTC{_as_owner(run)}"),
    ]


def _moves(run: dict) -> list[tuple[str, str]]:
    """How the run went so far: its agent time and times, waits, lease, the owner's asks, the run it retries, its
    session."""
    lines = []
    if run["run_seconds"]:
        lines.append(("agent time", f"{run['run_seconds'] // 60} min used of {_timeout(run)}"))
    times = [(name, run[f"{name}_at"]) for name in ("leased", "started", "finished") if run[f"{name}_at"]]
    if times:
        lines.append(("times (UTC)", ", ".join(f"{name} {when(value)}" for name, value in times)))
    if run["waiting_since"] and run["state"] == "waiting":
        lines.append(("waiting", f"since {when(run['waiting_since'])} UTC, for the answer to a decision"))
    if run["parked_at"]:
        lines.append(("parked", f"at {when(run['parked_at'])} UTC, for want of an answer"))
    if run["resume_of_run_id"]:
        lines.append(("resumes", f"run #{run['resume_of_run_id']}, in its session and worktrees"))
    if run["lease_expires_at"] and run["state"] not in TERMINAL_STATES:
        lines.append(("lease until", f"{when(run['lease_expires_at'])} UTC"))
    asks = [(name, run[f"{name}_requested_at"]) for name in ("cancel", "takeover", "handback")]
    asked = [f"{name} at {when(value)} UTC" for name, value in asks if value]
    if asked:
        lines.append(("asked", ", ".join(asked)))
    if run["parent_run_id"]:
        lines.append(("retries", f"run #{run['parent_run_id']}"))
    if run.get("steady_wait") and run["state"] == "queued":
        lines.append(("waits for", _steady_wait(run)))
    if run["session_id"]:
        lines.append(("session", run["session_id"]))
    return lines


def _verify_text(result) -> str:
    if isinstance(result, dict):
        return f"exit {result.get('exit_code')}: {result.get('command')}"
    return json.dumps(result, ensure_ascii=False)


def _results(run: dict) -> list[tuple[str, str]]:
    """What the run left: its diffstat, verify results, usage, error and cause, evidence, request and uploads."""
    lines = []
    stat = run["diffstat"]
    if isinstance(stat, dict):
        parts = [f"{stat[key]} {key}" for key in ("files", "insertions", "deletions") if key in stat]
        lines.append(("diffstat", ", ".join(parts) or json.dumps(stat)))
    for index, result in enumerate(run["verify"] or ()):
        lines.append(("verify" if index == 0 else "", _verify_text(result)))
    lines += spend_lines(run.get("spend"))
    if run["usage"]:
        lines.append(("usage", _clip(json.dumps(run["usage"], ensure_ascii=False, separators=(",", ":")), RAW_CHARS)))
    texts = [("error", run["error"]), ("cause", run.get("failure_cause")), ("evidence", run["evidence"])]
    texts.append(("request", run.get("request")))
    lines += [(label, text) for label, text in texts if text]
    blobs = [f"{name} {run[f'{name}_sha256'][:12]}" for name in ("log", "diff") if run[f"{name}_sha256"]]
    if blobs:
        lines.append(("uploaded", ", ".join(blobs)))
    return lines


def _as_owner(run: dict) -> str:
    """What `run show` says of ``as_owner`` after who dispatched the run."""
    return "; it runs as them on the hub's API, secrets, tokens and workers aside" if run.get("as_owner") else ""


def _steady_wait(run: dict) -> str:
    """Why a queued attempt waits for the worker the run before it was lost on, and until when, in three lines."""
    wait = run["steady_wait"]
    worker = f"worker {wait['worker']} (#{wait['worker_id']}), which lost run #{run['parent_run_id']}, to be steady"
    rule = f"once its heartbeats have come for {STEADY_SECONDS} s, none more than {STEADY_GAP_SECONDS} s late"
    steady = f"from {_clock(wait['steady_at'])} UTC if they go on" if wait.get("steady_at") else "it sends none now"
    if run["pinned_worker_id"] == wait["worker_id"]:
        others = "the run is pinned to that worker, so no other takes it"
    else:
        others = "any other worker that may take it takes it now"
    return f"{worker}\nit takes this attempt only {rule}: {steady}\n{others}"


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    run = hub.call("GET", _runs_path(project, args.run))
    if args.json:
        print_json(run)
        return 0
    print(f"Run #{run['id']}")
    lines = (_describe_local if run["kind"] == "local" else _describe)(run, hub.url)
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


@client_command
def cmd_logs(args) -> int:
    if args.json and args.follow:
        print("error: --json prints the events read as one object, so it does not go with --follow", file=sys.stderr)
        return EXIT_USAGE
    hub, _ = signed_in()
    project = project_of_args(args)
    after = args.after or 0
    if args.follow:
        ended = follow(hub, project, args.run, after, kinds=frozenset(args.kind or ()))
        print(f"Run #{args.run} ended {ended.get('state')}; its last event is {ended.get('last_seq')}.")
        return 0
    page = read_events(hub, project, args.run, after, args.kind or ())
    if args.json:
        print_json(page)
        return 0
    for event in page["events"]:
        print_event(event)
    shown = f"{len(page['events'])} event(s) shown, the last of the run is {page['last_seq']}"
    hint = "" if page["state"] in (*TERMINAL_STATES, "ended") else "; --follow prints the next ones as they come"
    print(f"Run #{args.run} is {page['state']}: {shown}{hint}.")
    return 0


CHAT_STATUS_WORDS = {
    "working": "the agent works on its turn",
    "waiting": "it waits for your reply: `evo-agents hub run send {run} TEXT`, or end it with "
    "`evo-agents hub run finish {run}`",
    "ended": "the chat is over",
}


@client_command
def cmd_chat(args) -> int:
    hub, _ = signed_in()
    chat = hub.call("GET", _runs_path(project_of_args(args), args.run, "chat"))
    if args.json:
        print_json(chat)
        return 0
    if chat["more"]:
        print("(older messages left out)")
    for message in chat["messages"]:
        who = "agent" if message["author"] == "agent" else message["login"] or "owner"
        print(f"[{when(message['at'])}] {who} (run #{message['run_id']}):")
        print(message["text"].rstrip())
        if message.get("attachments"):
            print(f"(files: {describe(message['attachments'])})")
        print()
    plan = f"; plan {chat['plan_id']} at revision {chat['plan_revision']}" if chat["plan_id"] else ""
    said = CHAT_STATUS_WORDS[chat["status"]].format(run=chat["run_id"])
    print(f"{len(chat['messages'])} message(s); run #{chat['run_id']} is {chat['state']}: {said}{plan}.")
    return 0


def _lease_state(lease: dict, now: datetime) -> str:
    """``revoked YYYY-MM-DD HH:MM`` (UTC), ``expired``, or ``out`` for a lease the run still holds."""
    if lease["revoked_at"]:
        return f"revoked {when(lease['revoked_at'])}"
    try:
        ends = datetime.fromisoformat(lease["expires_at"]) if lease["expires_at"] else None
    except ValueError:
        ends = None
    if ends is not None and (ends if ends.tzinfo else ends.replace(tzinfo=UTC)) <= now:
        return "expired"
    return "out"


@client_command
def cmd_credentials(args) -> int:
    hub, _ = signed_in()
    leases = hub.call("GET", _runs_path(project_of_args(args), args.run, "credentials"))
    if args.json:
        print_json(leases)
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
            when(lease["issued_at"]),
            when(lease["expires_at"]),
            _lease_state(lease, now),
        )
        for lease in leases
    ]
    table(("NAME", "PROVIDER", "TARGET", "WORKER", "ISSUED (UTC)", "EXPIRES (UTC)", "STATE"), rows)
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


def _finished(run: dict) -> str:
    if run["state"] == "done":
        return f"Ended the chat of run #{run['id']}: it is done."
    return (
        f"Ended the chat of run #{run['id']} ({run['state']}): its worker ends it done once the agent's turn is over."
    )


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
    "finish": (
        "end the chat of an author run: it ends done, at once when parked, else once its agent's turn is over",
        _finished,
    ),
}


def _control(action: str, said: Callable[[dict], str]):
    @client_command
    def command(args) -> int:
        hub, _ = signed_in()
        run = hub.call("POST", _runs_path(project_of_args(args), args.run, action), rerun_body(args))
        if args.json:
            print_json(run)
            return 0
        print(said(run))
        return 0

    command.__name__ = command.__qualname__ = f"cmd_{action}"
    return command


def _project_option(parser) -> None:
    parser.add_argument("--project", help=PROJECT_HELP)


def _run_argument(parser) -> None:
    parser.add_argument("run", metavar="RUN", type=int, help="the run's id, as `hub run list` shows it")
    _project_option(parser)


def _model_option(parser) -> None:
    parser.add_argument(
        "--model",
        help="the model, as the runtime names it (opencode: provider/model); needs --runtime with that runtime "
        "(default: the runtime's own choice)",
    )


def _register_dispatch(rsub) -> None:
    dispatch = rsub.add_parser(
        "dispatch",
        help="queue a run of each step named for the pool of its agent, all of them or none (needs writer)",
    )
    dispatch.add_argument("plan", metavar="PLAN", help="the plan's id")
    dispatch.add_argument("steps", metavar="STEP", nargs="+", help="a step id (or order); one run each")
    _project_option(dispatch)
    dispatch_options(dispatch)
    _model_option(dispatch)
    account_option(dispatch)
    json_option(dispatch, returns_array(*RUN_KEYS, schema="Run"))
    dispatch.set_defaults(func=cmd_dispatch)


def _register_plan(rsub) -> None:
    plan = rsub.add_parser(
        "plan",
        help="queue a plan run: one run, on the pool of its agent, that does every step of the plan "
        "not done yet, or with --system hand the plan to an agent system of the project (needs writer)",
    )
    plan.add_argument("plan", metavar="PLAN", help="the plan's id")
    _project_option(plan)
    plan_options(plan, _model_option)
    json_option(plan, PLAN_RUN)
    plan.set_defaults(func=cmd_plan)


def _register_author(rsub) -> None:
    authored = rsub.add_parser(
        "author",
        help="queue an author run: a plan written from your request with the create-exec-plan skill, on the pool of "
        "its agent (needs writer)",
    )
    authored.add_argument(
        "request",
        metavar="REQUEST",
        help=f"what the plan should achieve, at most {MAX_REQUEST_BYTES // 1024} KiB of UTF-8; - reads it from stdin",
    )
    _project_option(authored)
    author_options(authored)
    json_option(authored, RUN)
    authored.set_defaults(func=cmd_author)


def _register_list_and_show(rsub) -> None:
    listed = rsub.add_parser("list", help="the runs of a project, newest first, with how many are in each state")
    _project_option(listed)
    list_options(listed)
    json_option(listed, returns_object(*RUN_LIST_KEYS, schema="RunList"))
    listed.set_defaults(func=cmd_list)

    show = rsub.add_parser("show", help="one run: state, step, worker, commit, verify results, evidence")
    _run_argument(show)
    json_option(show, RUN)
    show.set_defaults(func=cmd_show)


def _register_logs(rsub) -> None:
    logs = rsub.add_parser(
        "logs", help="a run's events: what its agent said and did, and its moves; --follow prints them as they come"
    )
    _run_argument(logs)
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


def _register_chat_and_credentials(rsub) -> None:
    chat = rsub.add_parser(
        "chat", help="the chat of an author run: its agent's messages and its owner's, oldest first, and whose turn"
    )
    _run_argument(chat)
    json_option(chat, returns_object(*CHAT_KEYS, schema="RunChat"))
    chat.set_defaults(func=cmd_chat)

    credentials = rsub.add_parser(
        "credentials",
        help="the leases a run got: secret or GitHub App, target, worker, when issued, ending and revoked; never the "
        "value (owner only)",
    )
    _run_argument(credentials)
    json_option(credentials, returns_array(*LEASE_KEYS, schema="RunLease"))
    credentials.set_defaults(func=cmd_credentials)


def _register_controls(rsub) -> None:
    for action, (help_text, said) in CONTROLS.items():
        parser = rsub.add_parser(action, help=f"{help_text} (owner only)")
        _run_argument(parser)
        if action == "rerun":
            rerun_options(parser)
        json_option(parser, RUN)
        parser.set_defaults(func=_control(action, said))


def register_runs(hsub) -> None:
    run = hsub.add_parser(
        "run",
        help="dispatch plan steps, or a whole plan, to the pool of their agent, and follow, steer and end their runs",
    )
    rsub = run.add_subparsers(dest="run_command", required=True)
    _register_dispatch(rsub)
    _register_plan(rsub)
    _register_author(rsub)
    _register_list_and_show(rsub)
    _register_logs(rsub)
    register_watch(rsub, _project_option)
    register_send(rsub, _run_argument, project_of_args)
    _register_chat_and_credentials(rsub)
    register_candidates(rsub, _run_argument, project_of_args)
    register_outputs(rsub, _run_argument, project_of_args)
    register_access(rsub, _run_argument, project_of_args, RUN)
    _register_controls(rsub)
