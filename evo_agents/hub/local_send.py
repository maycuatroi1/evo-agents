"""``evo-agents hub send``: the outputs of local sessions (``evo_agents.hub.local_outputs``) to their runs on the hub.

- ``hub send FILE... [--caption TEXT]``, for the agent of a Claude Code session of a hub project, a CLI session above
  all, which has no SendUserFile: each file becomes an output of source ``sent`` of the session's local run, at once
  when the hub answers, else it waits on this machine like the outputs of the hooks. The session is ``--session``, else
  the one of CLAUDE_CODE_SESSION_ID (Claude Code sets it for its tools), else the newest session of the working
  directory, as a plan write finds its run (``local_sync.live_session``).
- ``hub send --queued [--wait SECONDS]``, which the hooks start in the background (``local_outputs.kick``): it sends
  what every session of this machine queued, then looks again before it ends, so an output a hook queued meanwhile
  does not wait for the next hook. One sender runs at a time per machine.

Per session, the sender reads the session's run and project from its local session file, copies each file sent on
purpose next to the queue under its SHA-256 (a text file redacted with ``redact_data``, this machine's hub token among
the values replaced, as the run's events are), reads the project's switch of tool images (``GET .../output-settings``;
off, the images taken are deleted unsent), then sends the outputs in batches of MAX_BATCH: the presigned uploads of
the bytes the project lacks (``POST .../output-uploads``), the PUTs, and the outputs (``POST .../outputs``). A batch
the hub refuses goes again output by output, and an output it still refuses (a limit, the switch, a name) is dropped
and named in the log. A hub or blob store that does not answer, a 5xx or a refused token ends the run and keeps every
output on this machine, and a hub that did not answer holds the hooks back for ``local_outputs.COOLDOWN`` seconds; a
session the hub does not know (404, 403), or of no hub project, loses its queue. Each run that did something writes
one line to ``send.log`` (names and counts, never a file's content). The queues of sessions untouched for KEEP_DAYS
are deleted. Standard library only.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub import hooks, local_outputs, local_sync, outputs
from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.client import (
    FILE_MODE,
    Hub,
    HubError,
    hub_dir,
    load_credentials,
    put_presigned,
    transient,
    write_atomic,
)
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.local_outputs import Queue

LOG_FILE = "send.log"
MAX_LOG_BYTES = 256 * 1024
KEEP_DAYS = 7
PASSES = 20  # passes over the queues one run makes at most
REQUEST_TIMEOUT = 30.0
RECORD_TIMEOUT = 120.0  # the hub reads every upload back to hash it before it records
PUT_ATTEMPTS = 3
EDIT_WAIT = 10.0  # seconds the sender waits for a queue's lock
CHUNK = 1 << 20
CLI_LOCK_WAIT = 30.0  # seconds ``hub send FILE`` waits for a sender of the hooks
SEND_KEYS = ("project", "run_id", "session_id", "outputs", "queued")
GONE = (403, 404)  # the hub does not know the session's run, or not as this member's
NO_RUN, REFUSED, UNREGISTERED = "no hub project", "refused lately", "not registered yet"
REGISTER_WAIT = 20.0  # seconds a run waits for a session to register, whose first hook queued outputs


class Stopped(Exception):
    """The hub or its blob store did not answer, failed, or refused the token: the run ends, and what waits stays."""


@dataclass
class Report:
    """What one run of the sender did."""

    recorded: dict[str, dict] = field(default_factory=dict)  # key of the item: the output the hub answered
    sessions: set[str] = field(default_factory=set)
    unregistered: set[str] = field(default_factory=set)  # sessions whose outputs wait for the session to register
    dropped: list[str] = field(default_factory=list)  # "name: why" of each output dropped
    stopped: str | None = None

    def line(self, waiting: int) -> str:
        text = f"{len(self.recorded)} output(s) recorded for {len(self.sessions)} session(s); {waiting} waiting"
        if self.dropped:
            text += f"; dropped {len(self.dropped)}: " + "; ".join(self.dropped[:5])
        return text + (f"; stopped: {self.stopped}" if self.stopped else "")


def log_line(text: str) -> None:
    """One line in ``send.log``, which keeps its newer half once past MAX_LOG_BYTES."""
    base = local_outputs.directory()
    with contextlib.suppress(OSError):
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = base / LOG_FILE
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {' '.join(text.split())}\n")
        if path.stat().st_size > MAX_LOG_BYTES:
            kept = path.read_bytes()[-MAX_LOG_BYTES // 2 :]
            write_atomic(path, kept[kept.find(b"\n") + 1 :], FILE_MODE)


def prune(now: float) -> None:
    """Delete the queues, and the bytes beside them, of sessions untouched for KEEP_DAYS."""
    with contextlib.suppress(OSError):
        for path in local_outputs.directory().iterdir():
            with contextlib.suppress(OSError):
                if path.is_dir() and now - (path / local_outputs.QUEUE).stat().st_mtime > KEEP_DAYS * 86400:
                    shutil.rmtree(path, ignore_errors=True)


# Copying a file sent on purpose


def redacted(data: bytes, token: str) -> bytes:
    """``data`` as text, its secrets replaced as the run's events are; bytes that are not UTF-8 stay as they are."""
    from evo_agents.hub.redact import redact_data

    return redact_data(data.decode("utf-8", "surrogateescape"), [token]).encode("utf-8", "surrogateescape")


def _copy(path: Path, target, digest, media_type: str, token: str) -> None:
    if outputs.is_text(media_type):
        data = redacted(path.read_bytes(), token)
        digest.update(data)
        target.write(data)
        return
    with open(path, "rb") as source:
        while chunk := source.read(CHUNK):
            digest.update(chunk)
            target.write(chunk)


def stage(queue: Queue, item: dict, token: str) -> tuple[str, int]:
    """Copy the file of ``item`` next to the queue under its SHA-256, a text file redacted; (sha256, size). ValueError
    naming why it cannot go: gone, not a file, larger than MAX_FILE_BYTES, unreadable."""
    path = Path(item["file"])
    if not path.is_file():
        raise ValueError("no such file any more")
    if path.stat().st_size > outputs.MAX_FILE_BYTES:
        raise ValueError(f"larger than {outputs.MAX_FILE_BYTES} bytes")
    queue.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, partial = tempfile.mkstemp(dir=queue.dir, prefix=".stage.", suffix=".part")  # created 0600
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as target:
            _copy(path, target, digest, item["media_type"], token)
        size = os.stat(partial).st_size
        if size > outputs.MAX_FILE_BYTES:
            raise ValueError(f"larger than {outputs.MAX_FILE_BYTES} bytes")
        os.replace(partial, queue.staged(digest.hexdigest()))
    except OSError:
        raise ValueError("the file cannot be read") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(partial)
    return digest.hexdigest(), size


# Sending


def _local_run(session_id: str, url: str) -> tuple[str, int] | str:
    """(project, run id) of the session's local run, or why there is none to send to: NO_RUN (a directory of no hub
    project: there never will be), REFUSED (the hub refused the session lately), UNREGISTERED (not yet)."""
    rec = local_sync.Store(session_id, url).load()
    if rec is not None and rec.get("resolved") and rec.get("project") is None:
        return NO_RUN
    refused = rec.get("refused_at") if rec else None
    if isinstance(refused, int | float) and 0 <= time.time() - refused < local_sync.REFUSAL_COOLDOWN:
        return REFUSED
    run_id, project = (rec.get("run_id"), rec.get("project")) if rec else (None, None)
    return (project, run_id) if isinstance(run_id, int) and isinstance(project, str) else UNREGISTERED


def _wire(item: dict, upload_id: str | None) -> dict:
    keys = ("sha256", "size", "source", "name", "media_type", "caption", "tool_call_id")
    return {**{key: item.get(key) for key in keys}, "upload_id": upload_id}


class Sender:
    """One run of the sender against ``hub`` (see the module)."""

    def __init__(self, hub: Hub, report: Report | None = None):
        self.hub = hub
        self.report = report or Report()

    def session(self, session_id: str) -> None:
        """Send what the session's queue holds. Raises Stopped."""
        queue = Queue(session_id)
        if not (queue.read(self.hub.url) or {}).get("items"):
            return
        found = _local_run(session_id, self.hub.url)
        if found == NO_RUN:
            shutil.rmtree(queue.dir, ignore_errors=True)
        if found == UNREGISTERED:
            self.report.unregistered.add(session_id)
        if isinstance(found, str):
            return
        project, run_id = found
        base = f"/v1/projects/{quote(project, safe='')}/local-runs/{run_id}"
        self.stage_all(queue)
        try:
            self.switch(queue, base)
            self.batches(queue, base)
        except HubError as exc:
            if exc.status not in GONE or transient(exc):
                raise self._stopped(exc) from None
            self.report.dropped.append(f"session {session_id}: {exc}")
            shutil.rmtree(queue.dir, ignore_errors=True)
            return
        self.report.sessions.add(session_id)

    def _stopped(self, exc: HubError) -> Stopped:
        if transient(exc):
            hooks.HookState(hub_dir()).mark_down(self.hub.url, time.time())
        self.report.stopped = str(exc)
        return Stopped(str(exc))

    def stage_all(self, queue: Queue) -> None:
        """Copy each file sent on purpose the queue names; one that cannot go is dropped and named."""
        data = queue.read(self.hub.url) or {"items": []}
        done: dict[str, tuple[str, int] | str] = {}
        for item in data["items"]:
            if item.get("file") and not item.get("sha256"):
                try:
                    done[item["key"]] = stage(queue, item, self.hub.token)
                except ValueError as exc:
                    done[item["key"]] = str(exc)
        if not done:
            return
        with queue.edit(self.hub.url, EDIT_WAIT) as data:
            kept = []
            for item in data["items"]:
                outcome = done.get(item["key"])
                if isinstance(outcome, str):
                    self.report.dropped.append(f"{item['name']}: {outcome}")
                    continue
                if outcome is not None and not item.get("sha256"):
                    item["sha256"], item["size"] = outcome
                kept.append(item)
            data["items"] = kept

    def switch(self, queue: Queue, base: str) -> None:
        """Read the project's switch of tool images; off, the images the queue holds are deleted unsent."""
        settings = self.hub.call("GET", f"{base}/output-settings")
        on = bool(settings.get("tool_images", True)) if isinstance(settings, dict) else True
        with queue.edit(self.hub.url, EDIT_WAIT) as data:
            data["tool_images"], data["switch_at"] = on, time.time()
            if on:
                return
            images = [item for item in data["items"] if item["source"] == outputs.TOOL]
            data["items"] = [item for item in data["items"] if item["source"] != outputs.TOOL]
            for item in images:
                queue.unstage(data, item.get("sha256"))

    def batches(self, queue: Queue, base: str) -> None:
        for _ in range(outputs.MAX_RUN_OUTPUTS):
            data = queue.read(self.hub.url) or {"items": []}
            ready = [item for item in data["items"] if item.get("sha256")][: outputs.MAX_BATCH]
            if not ready:
                return
            self.settle(queue, self.batch(queue, base, ready))

    def batch(self, queue: Queue, base: str, items: list[dict]) -> dict[str, dict | str]:
        """{key: the hub's output, or why it refused it} of each item; a refused batch goes again item by item."""
        try:
            return self.send(queue, base, items)
        except HubError as exc:
            if transient(exc) or exc.status in (401, *GONE) or (exc.status or 0) >= 500:
                raise
            if len(items) == 1:
                return {items[0]["key"]: str(exc)}
        outcomes: dict[str, dict | str] = {}
        for item in items:
            outcomes |= self.batch(queue, base, [item])
        return outcomes

    def send(self, queue: Queue, base: str, items: list[dict]) -> dict[str, dict]:
        """Upload the bytes of ``items`` the project lacks and record them; {key: the hub's output} of each."""
        best: dict[str, dict] = {}  # per SHA-256 in the queue's order, the item of the source kept longest
        for item in items:
            held = best.get(item["sha256"])
            if held is None or outputs.KEPT_ORDER[item["source"]] > outputs.KEPT_ORDER[held["source"]]:
                best[item["sha256"]] = item
        distinct = list(best.values())
        asked = self.hub.call(
            "POST",
            f"{base}/output-uploads",
            {"items": [{"sha256": i["sha256"], "size": i["size"], "source": i["source"]} for i in distinct]},
        )
        tickets = {ticket["sha256"]: ticket for ticket in asked.get("uploads") or []}
        for sha256, ticket in tickets.items():
            self.put(ticket["url"], queue.staged(sha256))
        wire = [_wire(item, tickets.get(item["sha256"], {}).get("upload_id")) for item in distinct]
        answer = self.hub.call("POST", f"{base}/outputs", {"outputs": wire}, timeout=RECORD_TIMEOUT)
        by_sha = {output["sha256"]: output for output in answer.get("outputs") or [] if isinstance(output, dict)}
        return {item["key"]: by_sha[item["sha256"]] for item in items if item["sha256"] in by_sha}

    def put(self, url: str, path: Path) -> None:
        for attempt in range(PUT_ATTEMPTS):
            try:
                put_presigned(url, path)
                return
            except HubError as exc:
                if attempt == PUT_ATTEMPTS - 1 or not transient(exc):
                    raise
                time.sleep(1.0 * 2**attempt)

    def settle(self, queue: Queue, outcomes: dict[str, dict | str]) -> None:
        """Take the items of ``outcomes`` out of the queue, remembering what the hub recorded."""
        with queue.edit(self.hub.url, EDIT_WAIT) as data:
            done = [item for item in data["items"] if item["key"] in outcomes]
            data["items"] = [item for item in data["items"] if item["key"] not in outcomes]
            for item in done:
                outcome = outcomes[item["key"]]
                if isinstance(outcome, str):
                    self.report.dropped.append(f"{item['name']}: {outcome}")
                else:
                    self.report.recorded[item["key"]] = outcome
                    keep = {"id": outcome["id"], "turn": item["turn"], "source": item["source"], "name": item["name"]}
                    data["recorded"].pop(item["sha256"], None)
                    data["recorded"][item["sha256"]] = keep  # the newest last, so the oldest go first
                queue.unstage(data, item["sha256"])
            for stale in list(data["recorded"])[: -local_outputs.MAX_RECORDED]:
                del data["recorded"][stale]
        hooks.HookState(hub_dir()).mark_up(self.hub.url)


def queued_sessions() -> list[tuple[float, str]]:
    """(queued_at, session id) of each session whose queue holds outputs, the newest first."""
    found = []
    with contextlib.suppress(OSError):
        for path in local_outputs.directory().iterdir():
            data = Queue(path.name).read() if path.is_dir() else None
            if data and data.get("items"):
                found.append((float(data.get("queued_at") or 0.0), path.name))
    return sorted(found, reverse=True)


def send_all(hub: Hub, report: Report) -> float:
    """Pass over the queues until a pass records nothing new; while a session whose first hook queued outputs has not
    registered yet (it does in that hook, after it queued them), pass again each second for REGISTER_WAIT at most. The
    newest ``queued_at`` the passes saw."""
    seen, started = 0.0, time.monotonic()
    for _ in range(PASSES):
        before, sessions = len(report.recorded), queued_sessions()
        seen = max([seen, *(at for at, _ in sessions)])
        report.unregistered.clear()
        for _, session_id in sessions:
            try:
                Sender(hub, report).session(session_id)
            except Stopped:
                return seen
            except (HubError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                report.dropped.append(f"session {session_id} left for later: {type(exc).__name__}")
        if len(report.recorded) == before:
            down = hooks.HookState(hub_dir()).down_since(hub.url, time.time(), local_outputs.COOLDOWN) is not None
            if down or not report.unregistered or time.monotonic() - started >= REGISTER_WAIT:
                break  # a hub that did not answer registers nobody
            time.sleep(1.0)
    return seen


def send_queued(wait: float) -> int:
    """``hub send --queued``: what the hooks queued, by one sender of this machine at a time; always 0."""
    try:
        credentials = load_credentials()
    except HubError:
        return 0
    hub = Hub(credentials.url, credentials.token, timeout=REQUEST_TIMEOUT)
    prune(time.time())
    while True:
        report = Report()
        with local_outputs.send_lock() as held:
            if not held:
                return 0
            time.sleep(wait)
            seen = send_all(hub, report)
        if report.recorded or report.dropped or report.stopped:
            waiting = sum(len((Queue(sid).read() or {}).get("items") or []) for _, sid in queued_sessions())
            log_line(report.line(waiting))
        if report.stopped or not any(at > seen for at, _ in queued_sessions()):
            return 0
        wait = 0  # a hook queued more while the lock was held: it started no sender, so this one goes on


# hub send FILE...


def _files(paths: list[str], caption: str | None) -> list[local_outputs.Sent]:
    """The files to send, or HubError naming the first that cannot go."""
    found = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise HubError(f"{raw}: no such file")
        if path.stat().st_size > outputs.MAX_FILE_BYTES:
            raise HubError(f"{raw}: larger than {outputs.MAX_FILE_BYTES} bytes, the most an output has")
        name = local_outputs.safe_name(path.name, "file")
        found.append(local_outputs.Sent(str(path.resolve()), name, outputs.guess_media_type(name), caption))
    return found


def _session(hub: Hub, named: str | None) -> tuple[str, dict]:
    env = dict(os.environ)
    if named:
        env[local_sync.SESSION_VARIABLE] = named
    found = local_sync.live_session(hub.url, None, env)
    if found is None:
        raise HubError(
            "no local run of a Claude Code session of a hub project here: run this inside the session after its "
            "first message (Claude Code names it in CLAUDE_CODE_SESSION_ID), or pass --session ID"
        )
    return found


def send_files(hub: Hub, session_id: str, files: list[local_outputs.Sent]) -> tuple[list[dict], list[str], Report]:
    """Queue ``files`` for the session and send them now unless another sender holds the lock CLI_LOCK_WAIT seconds:
    (the outputs the hub recorded, the names still waiting, the report)."""
    queue = Queue(session_id)
    with queue.edit(hub.url, EDIT_WAIT) as data:
        keys = local_outputs.add(data, queue, local_outputs.Found(files=files), hub.token, None)
    report = Report()
    with local_outputs.send_lock(wait=CLI_LOCK_WAIT) as held:
        if held:
            with contextlib.suppress(Stopped):
                Sender(hub, report).session(session_id)
    data = queue.read(hub.url) or {"items": []}
    waiting = [item["name"] for item in data["items"] if item["key"] in keys]
    if waiting:
        local_outputs.kick(hub.url, time.time())
    return [report.recorded[key] for key in keys if key in report.recorded], waiting, report


def send_json(project: str, run_id: int, session_id: str, sent: list[dict], waiting: list[str]) -> dict:
    """What ``hub send FILE... --json`` prints (SEND_KEYS): the outputs the hub recorded and the names that wait."""
    return {"project": project, "run_id": run_id, "session_id": session_id, "outputs": sent, "queued": waiting}


@client_command
def cmd_send(args) -> int:
    if args.queued:
        return send_queued(args.wait)
    if not args.files:
        raise HubError("name the files to send, or pass --queued")
    if not local_sync.enabled():
        raise HubError(
            "local sessions are not synced here: EVO_HUB_LOCAL_SYNC turns them off, and the agent of a worker run "
            "sends its files with `evo-agents worker output`"
        )
    hub, _ = signed_in()
    files = _files(args.files, local_outputs.caption_of(args.caption))
    session_id, rec = _session(hub, args.session)
    sent, waiting, report = send_files(hub, session_id, files)
    if args.json:
        print_json(send_json(rec["project"], rec["run_id"], session_id, sent, waiting))
        return 1 if report.dropped else 0
    for output in sent:
        print(f"sent {output['name']} as output #{output['id']} of local run #{rec['run_id']} ({rec['project']})")
    if waiting:
        why = f": {report.stopped}" if report.stopped else ""
        print(f"queued {', '.join(waiting)} for local run #{rec['run_id']}{why}; the evo-hub hooks send it later")
    for dropped in report.dropped:
        print(f"not sent: {dropped}", file=sys.stderr)
    return 1 if report.dropped else 0


def register_send(hsub) -> None:
    send = hsub.add_parser(
        "send",
        help="send files to the hub as outputs of this Claude Code session's local run, as SendUserFile does in "
        "Claude Desktop; --queued sends what the evo-hub hooks queued, as they do in the background",
    )
    send.add_argument("files", nargs="*", metavar="FILE", help="files to send, at most 200 MiB each")
    send.add_argument("--caption", help="what the files show, for the people who read the run")
    send.add_argument(
        "--session",
        metavar="ID",
        help="the Claude Code session (default: CLAUDE_CODE_SESSION_ID, else the newest here)",
    )
    send.add_argument("--queued", action="store_true", help="send what the evo-hub hooks queued; they run it")
    send.add_argument(
        "--wait",
        type=int,
        default=0,
        choices=range(0, 61),
        metavar="SECONDS",
        help="with --queued, wait first: 0 to 60",
    )
    json_option(send, returns_object(*SEND_KEYS))
    send.set_defaults(func=cmd_send)
