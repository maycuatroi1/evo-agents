"""The local session on the hub: what the evo-hub plugin tells the hub about a Claude Code session on this machine.

A session of Claude Code (the CLI, or the Code tab of Claude Desktop) whose directory belongs to a hub project is
registered as a run of kind ``local`` (``evo_agents.hub.server.local_runs``) at its first turn, and the hooks keep it
current:

- ``user-prompt`` (UserPromptSubmit): registers the session (``PUT .../local-runs/<session id>``, which says again that
  it is there when it is) and reports ``running``; a Desktop session whose account now reads otherwise
  (``local_account``) registers again with the new one;
- ``local-tool`` (PostToolUse of every tool): the words the agent wrote since the last hook and the tool that ran, as
  the log of the run, redacted here as the digest is (``evo_agents.hub.redact``, this machine's hub token among the
  values replaced); the files the tool sent and the images of its result wait on this machine as the run's outputs,
  the event holding a reference to each image instead of its base64, and go to the hub from a sender in the
  background (``evo_agents.hub.local_outputs``, ``evo_agents.hub.local_send``);
- ``stop`` (the Stop hook of ``evo_agents.hub.hooks`` calls ``on_stop``): the last words, then ``idle``;
- ``session-end`` (SessionEnd): ``ended``;
- ``local-ask`` (PreToolUse of AskUserQuestion): each question of the dialog is put to the hub as a decision of the
  run (category ``question``, its options keyed 1, 2, .., carrying the files the session sent since the person's last
  message: ``local_outputs.for_question``), so the member can answer it on the web, in Telegram or
  with ``hub decision answer`` too; the hook does not wait, so the dialog opens at once. When the person answers in
  the dialog, ``local-tool`` (PostToolUse of AskUserQuestion) reports it (``.../decisions/<id>/answered``): the first
  answer wins on the hub, and a later one gets 409, which is left alone. An answer the hook cannot report waits in the
  session's file for the next hook that reaches the hub;
- an answer that came from the hub after the session stopped waiting (or while the dialog was open) is in the run's
  inbox, and ``user-prompt`` hands it to the agent as ``additionalContext`` of the person's next message and
  acknowledges it with the next one. The ``ask_member`` MCP tool of the hub holds for an answer itself.

Every hook registers a session it does not know yet, so a Claude that fires only some of these events still shows its
sessions; a hub that never sees SessionEnd ends a run silent for 24 hours. The plan and step of the run are the hub's
to set: a plan write that names the run sets them (``run_for`` gives the id to ``evo_agents.hub.plan_cli`` and to the
MCP proxy), whether it comes from ``hub plan step``, ``evo harness step`` through the hub, or the ``plan_step`` tool.

What this machine keeps is ``~/.evo/hub/local/<session id>.json``: the run's id, the project, the state last sent, the
position in the transcript and the events that wait for the hub (at most MAX_PENDING; the oldest go first). Events
are redacted before they are written there. The files of sessions untouched for KEEP_DAYS are deleted when a session
registers.

It never fails or holds a session, as every hook of the plugin: the exit status is 0 whatever happens; a hub that did
not answer one hook of the machine, or answered with a failure that may pass (``client.transient``: a 502 or a 404 of
the proxy in front of it while it is redeployed, a 503, 429), is left alone for COOLDOWN seconds (a dialog's questions
and answers try it anyway) while events wait on disk for a later hook; one that refused the session is left alone for
REFUSAL_COOLDOWN; a hook past its deadline sends nothing more; PostToolUse asks the hub at
most once every FLUSH_EVERY seconds and waits LOCK_WAIT_TOOL for the session's file at most, so a tool call costs a
file write and, now and then, one short request. A session outside a hub project sends nothing: its first hook asks
the hub which project the directory belongs to (``MemorySync.project_of``, as the digest does) and writes the answer in
the file, and the later hooks stop at that file. ``EVO_HUB_LOCAL_SYNC=0`` (or ``off``) turns all of it off, and the
agent of a worker run (EVO_RUN_ID) never registers one: its run is on the hub already. The digest of the Stop hook is
not touched by either. Standard library only.

The last hook of a turn is Stop, so what it could not send (the hub was down then) has no later hook of its session to
send it: the run would stay ``running`` on the hub until the person's next message, or until the hub ends it silent.
Each session's file keeps the state its last hook wanted, and a Stop that reached the hub then sends, before its
deadline, what up to SWEEP other sessions of the machine left waiting: their events and the end of their turn
(``idle``, ``ended``), for sessions touched in the last SWEEP_HOURS and not refused lately. A session whose hook holds
its file at that moment is left to that hook.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub import digest, hooks, local_account, local_outputs
from evo_agents.hub.client import (
    FILE_MODE,
    Hub,
    HubError,
    hub_dir,
    load_credentials,
    transient,
    write_atomic,
)

try:
    import fcntl
except ImportError:  # Windows: no lock, the writes stay atomic and the hooks of one session rarely meet
    fcntl = None  # type: ignore[assignment]

DISABLE_VARIABLE = "EVO_HUB_LOCAL_SYNC"
SESSION_VARIABLE = "CLAUDE_CODE_SESSION_ID"  # set by Claude Code in the environment of its tools
OFF = frozenset({"0", "off", "false", "no"})
DIRECTORY = "local"  # under ~/.evo/hub
VERSION = 1
KEEP_DAYS = 7
MAX_PENDING = 500  # events kept for a hub that does not answer
BATCH = 100  # events in one request
BATCHES = 3  # requests of events one run sends at most
FLUSH_EVERY = 2.0  # seconds between two requests of PostToolUse
FIRST_READ = 200_000  # bytes of a transcript read from its start when the session registers late; more is history
MAX_READ = 1 << 20  # bytes of a transcript one hook reads
TEXT_CHARS = 4000  # characters of one message of the agent in the log
INPUT_CHARS = 300  # characters of a tool's input in the log
OUTPUT_CHARS = 400  # and of its result
TITLE_CHARS = 120
REFUSAL_COOLDOWN = 600.0  # seconds a hub that refused the session is left alone
# Seconds the sync of a local session leaves a hub that did not answer alone, whichever hook of the machine found it so;
# Stop and SessionStart wait hooks.COOLDOWN. A dialog's questions and answers do not wait at all.
COOLDOWN = 15.0
PLAN_FALLBACK_SECONDS = 12 * 3600  # run_for without a session id takes the newest session of the directory this young
LOCK_WAIT = 3.0
LOCK_WAIT_TOOL = 1.0
# Per hook: the timeout of one request, and the seconds after which the run sends nothing more.
LIMITS = {
    "user-prompt": (4.0, 8.0),
    "tool": (1.5, 3.0),
    "stop": (2.5, 6.0),
    "session-end": (2.0, 3.0),
    "ask": (3.0, 8.0),
    "tool-ask": (3.0, 6.0),  # PostToolUse of AskUserQuestion: it reports the answers of the dialog
}
BUDGET = {
    "user-prompt": 12.0,
    "tool": 8.0,
    "session-end": 4.0,
    "ask": 10.0,
}  # of the commands that are a hook of their own
STATES = {"user-prompt": "running", "tool": "running", "stop": "idle", "session-end": "ended", "ask": "running"}
ENDS = ("idle", "ended")  # what a turn or a session ends in: no later hook of that session sends it again
SWEEP = 3  # other sessions whose waiting events and end of turn one Stop sends at most, after its own
SWEEP_HOURS = 24  # sessions touched longer ago are left as they are: the hub ends a run silent for 24 hours itself
ASK_TOOL = "AskUserQuestion"
MAX_QUESTIONS = 4  # questions of one dialog put to the hub
MAX_ASKS = 5  # dialogs whose answers are still awaited, kept in the session's file
ANSWER_CHARS = 1000  # of one answer given in a dialog, kept until the hub has it
NOTE_CHARS = 4000  # what the inbox hands the agent at one message of the person
QUESTION_CHARS = 2000
LABEL_CHARS = 200
DESCRIPTION_CHARS = 1000
OPTIONS = (2, 6)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """False when EVO_HUB_LOCAL_SYNC turns it off, and for the agent of a worker run."""
    env = os.environ if env is None else env
    if env.get(hooks.RUN_VARIABLE, "").strip():
        return False
    return env.get(DISABLE_VARIABLE, "").strip().lower() not in OFF


def _now() -> float:
    return time.time()


def _cut(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def machine_name() -> str:
    name = socket.gethostname().strip().removesuffix(".local") or "unknown"
    return _cut(" ".join(name.split()), 200)


# The session's file


def _directory() -> Path:
    return hub_dir() / DIRECTORY


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == VERSION else None


@contextlib.contextmanager
def locked(path: Path, wait: float) -> Iterator[bool]:
    """True when this process holds the lock of ``path`` (waiting up to ``wait`` seconds for it); False when another
    does, or when the file cannot be made. Without ``fcntl`` it is always True."""
    if fcntl is None:
        yield True
        return
    fd = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    except OSError:
        yield False
        return
    got = False
    try:
        deadline = time.monotonic() + wait
        while not got:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Store:
    """The file of one session for one hub."""

    def __init__(self, session_id: str, url: str):
        self.url = url
        self.path = _directory() / f"{session_id}.json"
        self.lock = _directory() / f"{session_id}.lock"

    def load(self) -> dict | None:
        rec = _read(self.path)
        return rec if rec is not None and rec.get("hub") == self.url else None

    def save(self, rec: dict) -> None:
        with contextlib.suppress(OSError):  # only ever what waits for the hub
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(self.path, json.dumps(rec, ensure_ascii=False).encode(), FILE_MODE)

    def fresh(self, cwd: str) -> dict:
        return {
            "version": VERSION,
            "hub": self.url,
            "cwd": cwd,
            "project": None,
            "resolved": False,
            "run_id": None,
            "sent": None,
            "wanted": None,
            "seq": 0,
            "events": [],
            "offset": None,
            "flushed": 0.0,
            "touched": _now(),
            "title": None,
            "account": None,
            "refused_at": None,
        }


def prune(now: float) -> None:
    """Delete the files of sessions untouched for KEEP_DAYS days."""
    with contextlib.suppress(OSError):
        for path in _directory().iterdir():
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > KEEP_DAYS * 86400:
                    path.unlink()


# What the plan and MCP writes need: the run of the session that writes


def _live(rec: dict | None, url: str, project: str | None) -> int | None:
    if not rec or rec.get("hub") != url or rec.get("sent") == "ended":
        return None
    run = rec.get("run_id")
    if project is not None and rec.get("project") != project:
        return None
    return run if isinstance(run, int) and run > 0 else None


def live_session(
    url: str, project: str | None, env: Mapping[str, str] | None = None, cwd: str | None = None
) -> tuple[str, dict] | None:
    """(session id, file) of the session that is writing to the hub: the one of CLAUDE_CODE_SESSION_ID, else the
    session of the newest run whose directory is ``cwd``. None when sync is off, the session never registered, ended,
    or is another project's (``project`` None: any). Reads files only; never raises."""
    try:
        env = os.environ if env is None else env
        if not enabled(env):
            return None
        session_id = env.get(SESSION_VARIABLE, "").strip()
        if session_id:
            if not digest.SESSION_ID.fullmatch(session_id):
                return None
            rec = _read(_directory() / f"{session_id}.json")
            return (session_id, rec) if _live(rec, url, project) is not None else None
        where = os.path.abspath(cwd or os.getcwd())
        now, found = _now(), []
        for path in _directory().glob("*.json"):
            rec = _read(path)
            if rec and rec.get("cwd") == where and now - float(rec.get("touched") or 0) < PLAN_FALLBACK_SECONDS:
                if _live(rec, url, project) is not None:
                    found.append((float(rec["touched"]), path.stem, rec))
        newest = max(found, key=lambda entry: entry[0]) if found else None
        return (newest[1], newest[2]) if newest else None
    except Exception:
        return None


def run_for(url: str, project: str | None, env: Mapping[str, str] | None = None, cwd: str | None = None) -> int | None:
    """The local run of the session that is writing to the hub's plans (``live_session``), or None."""
    found = live_session(url, project, env, cwd)
    return found[1]["run_id"] if found else None


# The log


def _tool_input(name: str, data) -> object:
    if not isinstance(data, dict):
        return _cut(data, INPUT_CHARS) if data is not None else None
    if name == "Bash":
        shown = {"command": _cut(data.get("command", ""), INPUT_CHARS)}
        if isinstance(data.get("description"), str):
            shown["description"] = _cut(data["description"], INPUT_CHARS)
        return shown
    return {
        str(k): _cut(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False), INPUT_CHARS)
        for k, v in data.items()
    }


def _text_of(response) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        return "\n".join(_text_of(item) for item in response if item is not None)
    if isinstance(response, dict):
        if response.get("type") == "text" and isinstance(response.get("text"), str):
            return response["text"]
        parts = [response[key] for key in ("stdout", "stderr") if isinstance(response.get(key), str) and response[key]]
        if parts:
            return "\n".join(parts)
        file = response.get("file")
        if isinstance(file, dict) and isinstance(file.get("content"), str):
            return file["content"]
        for key in ("content", "output", "text", "result"):
            if key in response:
                return _text_of(response[key])
        return json.dumps(response, ensure_ascii=False)
    return "" if response is None else str(response)


def tool_events(payload: dict, found: local_outputs.Found | None = None) -> list[tuple[str, dict]]:
    """The call and its result of a PostToolUse payload, as the ACP events the run's trace shows: each image of the
    result (``local_outputs.found_in``) as a reference to its output after the text, never its base64."""
    name = payload.get("tool_name")
    if not isinstance(name, str) or not name:
        return []
    found = local_outputs.found_in(payload) if found is None else found
    call = payload.get("tool_use_id")
    call = call if isinstance(call, str) and call else f"{payload.get('session_id')}-{int(_now() * 1000)}"
    text = " ".join(_text_of(found.response).split())
    content = [{"type": "content", "content": {"type": "text", "text": _cut(text, OUTPUT_CHARS)}}]
    content += [{"type": "content", "content": reference} for reference in found.references()]
    return [
        (
            "tool_call",
            {
                "toolCallId": call,
                "title": name,
                "rawInput": _tool_input(name, found.tool_input),
                "status": "in_progress",
            },
        ),
        ("tool_call_update", {"toolCallId": call, "title": name, "status": "completed", "content": content}),
    ]


def agent_words(rec: dict, transcript: Path) -> list[str]:
    """What the agent wrote in the transcript since the last hook, and move the position on."""
    try:
        size = transcript.stat().st_size
        offset = rec.get("offset")
        if offset is None:
            offset = 0 if size <= FIRST_READ else size
        if size < offset:
            offset = 0
        with transcript.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(MAX_READ)
    except OSError:
        return []
    end = data.rfind(b"\n")
    if end < 0:
        rec["offset"] = offset + (len(data) if len(data) == MAX_READ else 0)
        return []
    rec["offset"] = offset + end + 1
    texts = []
    for line in data[: end + 1].splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        message = item.get("message") if isinstance(item, dict) else None
        if not isinstance(message, dict) or item.get("type") != "assistant" or item.get("isSidechain"):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                if block["text"].strip():
                    texts.append(block["text"].strip())
    return texts


def _append(rec: dict, events: list[tuple[str, dict]], token: str) -> None:
    from evo_agents.hub.redact import redact_data

    stamp = datetime.now(UTC).isoformat()
    for kind, body in events:
        rec["seq"] += 1
        rec["events"].append({"seq": rec["seq"], "at": stamp, "kind": kind, "body": redact_data(body, [token])})
    del rec["events"][:-MAX_PENDING]


# The hub


class Late(Exception):
    """The run is past its deadline: nothing more is sent, and what waits stays on disk."""


class Delivery:
    """One hook's talk with the hub for one session."""

    def __init__(
        self,
        hub: Hub,
        login: str,
        rec: dict,
        sid: str,
        where: Path,
        hook: str,
        state: hooks.HookState,
        payload: dict | None = None,
        deadline: float | None = None,
    ):
        self.hub, self.login, self.rec, self.sid, self.where, self.hook, self.state = (
            hub,
            login,
            rec,
            sid,
            where,
            hook,
            state,
        )
        self.payload = payload or {}
        self.notes: list[str] = []
        self.deadline = (
            deadline if deadline is not None else time.monotonic() + LIMITS[limit_key(hook, self.payload)][1]
        )

    def call(self, method: str, path: str, body: dict | None = None):
        if time.monotonic() > self.deadline:
            raise Late
        return self.hub.call(method, path, body)

    def base(self) -> str:
        return f"/v1/projects/{quote(self.rec['project'], safe='')}/local-runs"

    def register(self, title: str | None) -> None:
        rec = self.rec
        if rec.get("account") is None:
            found = local_account.read_account(self.sid)
            rec["account"] = {"email": found.email, "profile": found.profile}
        body = {"machine": machine_name(), "cwd": rec["cwd"], "claude_email": rec["account"]["email"]}
        if rec["account"].get("profile"):
            body["claude_profile"] = rec["account"]["profile"]
        if rec.get("title") or title:
            body["title"] = rec["title"] = rec.get("title") or title
        view = self.call("PUT", f"{self.base()}/{quote(self.sid, safe='')}", body)
        rec["run_id"] = int(view["id"])
        rec["sent"] = view.get("state")
        if not rec["events"] and isinstance(view.get("last_seq"), int):
            rec["seq"] = view["last_seq"]  # the hub has events of an earlier life of this session
        prune(_now())

    def moved(self) -> bool:
        """Whether a Desktop session's account reads otherwise than the one it registered with (read before Desktop's
        variables were, or by an earlier evo-ak); the next register sends the new one. Other sessions keep theirs."""
        if local_account.desktop_email(os.environ) is None:
            return False
        found = local_account.read_account(self.sid)
        account = {"email": found.email, "profile": found.profile}
        if account == self.rec.get("account"):
            return False
        self.rec["account"] = account
        return True

    def state_post(self, wanted: str) -> None:
        for attempt in range(2):
            try:
                self.call("POST", f"{self.base()}/{self.rec['run_id']}/state", {"state": wanted})
                self.rec["sent"] = wanted
                return
            except HubError as exc:
                if attempt or transient(exc) or exc.status not in (404, 409):
                    raise
                if wanted == "ended":
                    self.rec["sent"] = "ended"  # it ended already, or the hub never had it
                    return
                self.register(None)  # the run ended or is gone: the session registers again
                if wanted == self.rec["sent"]:
                    return

    def flush(self) -> None:
        rec = self.rec
        for _ in range(BATCHES):
            events = rec["events"]
            if not events:
                break
            batch = events[:BATCH]
            answer = self.call("POST", f"{self.base()}/{rec['run_id']}/events", {"events": batch})
            ack = answer.get("ack_seq") if isinstance(answer, dict) else None
            if not isinstance(ack, int):
                break
            if (
                ack < batch[0]["seq"] - 1
            ):  # the hub lacks events this machine dropped: number what is left after its last
                for number, event in enumerate(events, start=ack + 1):
                    event["seq"] = number
                rec["seq"] = ack + len(events)
                continue
            rec["events"] = [e for e in events if e["seq"] > ack]
            rec["flushed"] = _now()

    def ask(self) -> None:
        """Put the questions of the AskUserQuestion dialog about to open to the hub, as decisions of the run, each
        carrying the files the session sent since the person's last message (``local_outputs.for_question``)."""
        rec = self.rec
        data = self.payload.get("tool_input")
        questions = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(questions, list):
            return
        attached = local_outputs.for_question(self.sid, self.hub.url, self.deadline)
        entries = []
        for q in questions[:MAX_QUESTIONS]:
            found = _question(q)
            if found is None:
                continue
            body, keys = found
            view = self._decision({**body, "output_ids": attached} if attached else body)
            if view is None:
                continue  # the hub will not take this question: the dialog still opens
            entries.append({"decision": int(view["id"]), "question": q["question"], "keys": keys})
            rec["sent"] = "waiting"
        if entries:
            asks = rec.setdefault("asks", {})
            asks[str(self.payload.get("tool_use_id") or "last")] = entries
            for stale in list(asks)[:-MAX_ASKS]:
                del asks[stale]

    def _decision(self, body: dict) -> dict | None:
        """The hub's view of the decision ``body`` asks; None when the hub will not take it. A question whose files
        the hub refuses (422: an output of another run, or deleted) is asked again without them."""
        for attempt in (body, {k: v for k, v in body.items() if k != "output_ids"}):
            try:
                return self.call("POST", f"{self.base()}/{self.rec['run_id']}/decisions", attempt)
            except HubError as exc:
                if transient(exc) or exc.status not in (404, 409, 422):
                    raise
                if exc.status != 422 or "output_ids" not in attempt:
                    return None
        return None

    def answered(self) -> None:
        """Dialogs were answered in the session: report each answer to the decision it was put as, the ones an earlier
        hook could not report too (they wait in the session's file)."""
        rec = self.rec
        waiting = rec.get("answers") or {}
        for key in list(waiting):
            self._report(key, waiting[key] if isinstance(waiting[key], dict) else {})
            del waiting[key]

    def _report(self, key: str, given: dict) -> None:
        rec = self.rec
        asks = rec.get("asks") or {}
        entries = asks.get(key) or (asks.get("last") if key != "last" else None) or []
        for entry in entries:
            said = given.get(entry["question"])
            body = {"text": "answered in the session"}
            if isinstance(said, str) and said.strip():
                from evo_agents.hub.redact import redact

                body = {"text": _cut(redact(said.strip()), 1000)}
                if said in entry["keys"]:
                    body = {"option": entry["keys"][said]}
            try:
                self.call("POST", f"{self.base()}/{rec['run_id']}/decisions/{entry['decision']}/answered", body)
            except HubError as exc:
                if transient(exc) or exc.status not in (404, 409, 422):
                    raise  # kept in the file: the next hook tells the hub
        for name in [k for k, v in asks.items() if v is entries]:
            del asks[name]
        if entries:
            rec["sent"] = "running"

    def inbox(self) -> None:
        """What the member answered on the hub since the agent last heard: for the agent, at the person's message."""
        rec = self.rec
        ack = rec.get("inbox_ack")
        try:
            answer = self.call(
                "POST", f"{self.base()}/{rec['run_id']}/inbox", {"ack": ack} if isinstance(ack, int) else None
            )
        except HubError as exc:
            if transient(exc) or exc.status not in (404, 405):
                raise
            return  # a hub that has no inbox for local runs: nothing to hand over
        messages = answer.get("messages") if isinstance(answer, dict) else None
        for message in messages or []:
            if isinstance(message, dict) and isinstance(message.get("text"), str) and message["text"].strip():
                by = message.get("sent_by")
                self.notes.append(f"{by + ': ' if isinstance(by, str) and by else ''}{message['text'].strip()}")
                if isinstance(message.get("id"), int):
                    rec["inbox_ack"] = max(message["id"], rec.get("inbox_ack") or 0)

    def run(self, title: str | None, wanted: str, due: bool) -> None:
        rec = self.rec
        if not rec["resolved"]:
            from evo_agents.hub.memory import MemorySync

            rec["project"] = MemorySync(self.hub, self.login, lock_timeout=hooks.LOCK_WAIT).project_of(self.where)
            rec["resolved"] = True
        if rec["project"] is None:
            rec["events"] = []
            return
        if rec["run_id"] is None or (self.hook == "user-prompt" and self.moved()):
            self.register(title)
        if wanted == "ended" and due:
            self.flush()
        if wanted != rec["sent"]:
            self.state_post(wanted)
        if wanted != "ended" and due:
            self.flush()
        if rec.get("answers"):  # before a new dialog is put: the earlier ones' answers end their wait first
            self.answered()
        if self.hook == "ask":
            self.ask()
        elif self.hook == "user-prompt":
            self.inbox()


def _options(options: list) -> tuple[list[dict], dict[str, str]]:
    """The options of a question as the hub takes them, keyed 1, 2, .., and {label: key}."""
    from evo_agents.hub.redact import redact

    keys, sent = {}, []
    for n, option in enumerate(options[: OPTIONS[1]], start=1):
        label = " ".join(str(option.get("label") if isinstance(option, dict) else option).split())
        if not label:
            continue
        shown = {"key": str(n), "label": _cut(redact(label), LABEL_CHARS)}
        if isinstance(option, dict) and isinstance(option.get("description"), str) and option["description"]:
            shown["description"] = _cut(redact(option["description"]), DESCRIPTION_CHARS)
        keys[label] = shown["key"]
        sent.append(shown)
    return sent, keys


def _question(q) -> tuple[dict, dict[str, str]] | None:
    """(the decision a question of the dialog is put as, {label: key}); None for one the hub cannot take."""
    from evo_agents.hub.redact import redact

    options = q.get("options") if isinstance(q, dict) else None
    if not isinstance(q.get("question") if isinstance(q, dict) else None, str) or not isinstance(options, list):
        return None
    sent, keys = _options(options)
    if len(sent) < OPTIONS[0]:
        return None
    body = {"category": "question", "question": _cut(redact(q["question"]).strip(), QUESTION_CHARS), "options": sent}
    header = q.get("header")
    multi = "Several options may be picked in the session. " if q.get("multiSelect") else ""
    if multi or isinstance(header, str) and header.strip():
        body["context"] = _cut(multi + (redact(header) if isinstance(header, str) else ""), 1000).strip()
    return body, keys


def limit_key(hook: str, payload: dict) -> str:
    """The row of LIMITS a hook runs under: PostToolUse of the AskUserQuestion dialog has more to say than any other."""
    return "tool-ask" if hook == "tool" and payload.get("tool_name") == ASK_TOOL else hook


def _absolute(value) -> str | None:
    return value if isinstance(value, str) and value and os.path.isabs(value) else None


def _title(payload: dict) -> str | None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return None
    from evo_agents.hub.redact import redact

    line = " ".join(redact(prompt).split())
    return _cut(line, TITLE_CHARS) if line else None


def _answer_text(answer) -> str | None:
    """One answer of the dialog as text: the labels a multiSelect question was given (a list) joined by "; ", as a
    label may hold commas, and one picked alone stays that label, so it is still reported as its option."""
    if isinstance(answer, list):
        answer = "; ".join(" ".join(a.split()) for a in answer if isinstance(a, str) and a.strip())
    return _cut(answer, ANSWER_CHARS) if isinstance(answer, str) and answer.strip() else None


def _keep_answers(rec: dict, payload: dict) -> None:
    """Keep the answers the AskUserQuestion dialog of ``payload`` was given until the hub has them."""
    response = payload.get("tool_response")
    given = response.get("answers") if isinstance(response, dict) else None
    given = given if isinstance(given, dict) else {}
    kept = {q: text for q, a in given.items() if isinstance(q, str) and (text := _answer_text(a)) is not None}
    answers = rec.setdefault("answers", {})
    answers[str(payload.get("tool_use_id") or "last")] = kept
    for stale in list(answers)[:-MAX_ASKS]:
        del answers[stale]


def _refused_lately(rec: dict, now: float) -> bool:
    """Whether the hub refused the session of ``rec`` less than REFUSAL_COOLDOWN seconds before ``now``."""
    refused = rec.get("refused_at")
    return isinstance(refused, int | float) and 0 <= now - refused < REFUSAL_COOLDOWN


def _hook_events(hook: str, payload: dict, rec: dict, found: local_outputs.Found) -> list[tuple[str, dict]]:
    """What the hook of ``payload`` adds to the log of ``rec``: the agent's words since the last hook (PostToolUse and
    Stop) and the tool that ran (PostToolUse). At the person's message, the transcript's end is where its turn starts."""
    events = []
    transcript = _absolute(payload.get("transcript_path"))
    if hook in ("tool", "stop") and transcript:
        events += [("agent_message_chunk", {"text": _cut(t, TEXT_CHARS)}) for t in agent_words(rec, Path(transcript))]
    if hook == "tool":
        events += tool_events(payload, found)
    if hook == "user-prompt" and rec.get("offset") is None and transcript:
        with contextlib.suppress(OSError):
            rec["offset"] = os.stat(transcript).st_size  # what is in the transcript now is not this turn's
    return events


def sync(hook: str, payload: dict, hub: Hub, login: str, now: float | None = None) -> list[str]:
    """The work of one hook of the session in ``payload`` against ``hub``; the caller keeps exceptions from the
    session. The answers the member gave on the hub, for the agent (user-prompt only), else an empty list."""
    now = _now() if now is None else now
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not digest.SESSION_ID.fullmatch(session_id):
        return []
    where = hooks.session_dir(payload)
    cwd = _absolute(payload.get("cwd")) or str(where)
    store = Store(session_id, hub.url)
    rec = store.load()
    if (rec is None and hook in ("stop", "session-end")) or (
        rec is not None and rec["resolved"] and rec["project"] is None
    ):
        return []
    asking = limit_key(hook, payload) == "tool-ask"
    wait = LOCK_WAIT_TOOL if hook == "tool" and not asking else LOCK_WAIT
    found = local_outputs.found_in(payload) if hook == "tool" else local_outputs.Found()
    _outputs(hook, session_id, hub, found, payload, now)  # before the lock: a busy session file loses no file
    with locked(store.lock, wait) as got:
        if not got:
            return []
        rec = store.load() or store.fresh(cwd)
        title = _title(payload) if hook == "user-prompt" else None
        _append(rec, _hook_events(hook, payload, rec, found), hub.token)
        if asking:
            _keep_answers(rec, payload)
        rec["touched"] = now
        rec["wanted"] = STATES[hook]
        store.save(rec)
    if _refused_lately(rec, now):
        return []
    state = hooks.HookState(hub_dir())
    if hook != "ask" and not asking and state.down_since(hub.url, now, COOLDOWN) is not None:
        return []
    due = hook != "tool" or asking or now - float(rec.get("flushed") or 0) >= FLUSH_EVERY
    if not due and rec["sent"] == STATES[hook]:
        return []
    with locked(store.lock, 0.0 if hook == "tool" and not asking else wait) as got:
        if not got:
            return []
        rec = store.load() or rec
        delivery = Delivery(hub, login, rec, session_id, where, hook, state, payload)
        try:
            reached = _deliver(delivery, title, STATES[hook], due, now)
        finally:
            store.save(rec)
    if reached and hook == "stop":
        sweep(hub, login, state, session_id, delivery.deadline, now)
    return delivery.notes if hook == "user-prompt" else []


def _outputs(hook: str, session_id: str, hub: Hub, found: local_outputs.Found, payload: dict, now: float) -> None:
    """The outputs' share of a hook (``evo_agents.hub.local_outputs``): a tool's images and files sent wait on this
    machine, the person's message starts a turn, and the sender starts in the background while outputs wait."""
    if hook == "user-prompt":
        local_outputs.new_turn(session_id, hub.url)
    call = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else None
    queued = local_outputs.take(session_id, hub.url, found, hub.token, call)
    if queued or (hook in ("user-prompt", "stop", "session-end") and local_outputs.waiting(session_id, hub.url)):
        local_outputs.kick(hub.url, now)


def _deliver(delivery: Delivery, title: str | None, wanted: str, due: bool, now: float) -> bool:
    """``delivery.run``, with what its outcome means for the session's record and the machine's hook state; True when
    the hub took it all."""
    try:
        delivery.run(title, wanted, due)
    except Late:
        return False
    except HubError as exc:
        if transient(exc):  # no answer, or the hub restarting or busy: any hook tries again after COOLDOWN
            delivery.state.mark_down(delivery.hub.url, _now())
        else:  # a refusal: the token, the role, another member's session
            delivery.rec["refused_at"] = now
        return False
    delivery.rec["refused_at"] = None
    delivery.state.mark_up(delivery.hub.url)
    return True


def _waiting(rec: dict | None, url: str, now: float) -> bool:
    """Whether the session of ``rec`` left events or the end of its turn waiting for the hub at ``url``, in the last
    SWEEP_HOURS, and the hub did not refuse it lately."""
    if not rec or rec.get("hub") != url or rec.get("project") is None or not isinstance(rec.get("run_id"), int):
        return False
    if not 0 <= now - float(rec.get("touched") or 0) < SWEEP_HOURS * 3600 or _refused_lately(rec, now):
        return False
    return bool(rec.get("events")) or (rec.get("wanted") in ENDS and rec.get("wanted") != rec.get("sent"))


def sweep(hub: Hub, login: str, state: hooks.HookState, skip: str, deadline: float, now: float) -> None:
    """Send what up to SWEEP other sessions of this machine left waiting for ``hub``, the newest first, until
    ``deadline`` (``time.monotonic``) or a failure. A session whose file another hook holds is skipped."""
    found = []
    with contextlib.suppress(OSError):
        for path in _directory().glob("*.json"):
            rec = _read(path)
            if path.stem != skip and digest.SESSION_ID.fullmatch(path.stem) and _waiting(rec, hub.url, now):
                found.append((float(rec["touched"]), path.stem))
    for _, sid in sorted(found, reverse=True)[:SWEEP]:
        if time.monotonic() > deadline:
            return
        store = Store(sid, hub.url)
        with locked(store.lock, 0.0) as got:
            rec = store.load() if got else None
            if not _waiting(rec, hub.url, now):
                continue
            wanted = rec["wanted"] if rec.get("wanted") in ENDS else rec["sent"]
            delivery = Delivery(hub, login, rec, sid, Path(rec["cwd"]), "sweep", state, deadline=deadline)
            try:
                reached = _deliver(delivery, None, wanted, True, now)
            finally:
                store.save(rec)
        if not reached:
            return


def hook_run(hook: str, payload: dict) -> list[str]:
    """``sync`` with the credentials of this machine; nothing when sync is off, nobody is signed in, or the hub URL is
    refused. Returns what ``sync`` does. Raises nothing but OutOfTime."""
    if not enabled():
        return []
    try:
        credentials = load_credentials()
    except HubError:  # not signed in, or a file that cannot be read: nothing to send
        return []
    try:
        timeout = LIMITS[limit_key(hook, payload)][0]
        return sync(hook, payload, Hub(credentials.url, credentials.token, timeout=timeout), credentials.login)
    except Exception:
        return []  # the session goes on; what waits stays in its file


def _context(notes: list[str]) -> str:
    """What the agent is told at the person's next message about answers the member gave on the hub."""
    lines = "\n".join(f"- {note}" for note in notes)
    text = (
        "evo-hub: the member answered, on the hub, a question this session asked and stopped waiting for. "
        f"Take it into account:\n{lines}"
    )
    return _cut(text, NOTE_CHARS)


def on_stop(payload: dict) -> None:
    """The Stop hook's share: the last words of the turn and ``idle``."""
    hook_run("stop", payload)


def _command(hook: str) -> Callable:
    def command(args) -> int:
        try:
            with hooks.budget(BUDGET[hook]):
                payload = local_outputs.read_payload() if hook == "tool" else hooks.read_payload()
                if hook == "session-end" and hooks.worker_run() is None:
                    hooks.queue_transcript(payload, ended=True)  # the transcripts' own switch, not this one's
                notes = hook_run(hook, payload)
                if notes:
                    out = {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": _context(notes),
                        }
                    }
                    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
        except hooks.OutOfTime:
            pass
        except BaseException as exc:  # a hook never fails the session
            with contextlib.suppress(OSError, ValueError):  # a closed stderr must not fail the hook either
                print(f"evo-hub: local session not synced: unexpected {type(exc).__name__}", file=sys.stderr)
        return 0

    return command


cmd_hook_user_prompt = _command("user-prompt")
cmd_hook_local_tool = _command("tool")
cmd_hook_session_end = _command("session-end")
cmd_hook_local_ask = _command("ask")
