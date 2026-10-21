"""The outputs of a local session (``evo_agents.hub.outputs``): what the evo-hub plugin takes out of a Claude Code
session of a hub project for its run of kind local (``evo_agents.hub.local_sync``), and keeps on this machine until
``evo_agents.hub.local_send`` sends it.

What becomes an output of the session's run:

- a file SendUserFile sent (the Code tab of Claude Desktop): each path of its input ``files``, else of the
  ``attachments`` of its result, source ``sent``, with the input's ``caption``;
- a page the Artifact tool published (no ``action``, or ``publish``, a ``file_path`` and no ``asset``, and a claude.ai
  link in its result): the page's file, source ``sent``, its caption the page's title and link and the input's
  ``description``;
- a file ``evo-agents hub send FILE`` names, which is how a CLI session, which has no SendUserFile, sends one;
- every image block of a tool's result (a browser's screenshot through MCP or Playwright, a Read of an image), source
  ``tool``, in any of the shapes Claude Code hands a hook (IMAGE_SHAPES), MAX_IMAGES of one result at most. The tool's
  event in the run's log keeps a reference to it in place of its base64 (``reference``: ``{"type": "output", "source":
  "tool", "sha256", "media_type", "size", "name"}`` as the content of a ``tool_call_update``), which the run's outputs
  match by SHA-256. A project whose admin turned tool images off gets none, once the sender read the switch.

A shape the parser does not know is passed over, never an error: a SendUserFile without files, an Artifact call that
is a quickstart or a listing, a block that is not an image.

The PostToolUse hook only writes on this machine, in its own deadline: the bytes of an image go to
``~/.evo/hub/local-outputs/<session id>/<sha256>``, and each output waits in that directory's ``queue.json`` (QUEUE)
with its source, name, media type, caption (redacted here, as the run's events are) and tool call; a file sent on purpose
waits as its path, and the sender copies it, redacting a text file (``outputs.is_text``) with ``redact_data`` as the
events are, when it runs. The hook then starts the sender in the background (``kick``), detached from the session,
unless one runs already or the hub did not answer any hook of this machine in the last COOLDOWN seconds; the Stop,
UserPromptSubmit and SessionEnd hooks start it too while outputs wait. So no upload runs in a hook's deadline, and a
hub or a blob store that does not answer leaves the outputs on this machine for a later one.

A PostToolUse payload is read whole up to TOOL_PAYLOAD characters, well past the 1 MiB of the other hooks
(``hooks.MAX_PAYLOAD``), since a screenshot's base64 is often more. A longer one still gives the tool's event: its
session, directory, transcript, tool and tool call come from its first and last characters (``read_payload``).

A question of the session's AskUserQuestion dialog carries the files the session sent (source ``sent``) since the
person's last message (``for_question``): the turn counts up at each UserPromptSubmit (``new_turn``), and the hook
waits up to ASK_WAIT seconds for the sender to record the files of the turn still waiting. Standard library only.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from evo_agents import filelock
from evo_agents.hub import hooks, outputs
from evo_agents.hub.client import FILE_MODE, hub_dir, write_atomic

DIRECTORY = "local-outputs"  # under ~/.evo/hub
QUEUE = "queue.json"
QUEUE_LOCK = "queue.lock"
SEND_LOCK = "send.lock"  # one sender at a time per machine
VERSION = 1
SEND_TOOL = "SendUserFile"
ARTIFACT_TOOL = "Artifact"
READ_TOOL = "Read"
IMAGE_SHAPES = ("source: {type: base64, media_type, data}", "file: {base64, type} (Read)", "data, mimeType (MCP)")
MAX_IMAGES = 20  # image blocks of one tool result taken
MAX_DEPTH = 8  # levels of a result searched; what is deeper is left out of the event
MAX_QUEUED = outputs.MAX_RUN_OUTPUTS  # outputs that wait for the hub per session
MAX_RECORDED = 1000  # SHA-256s whose output the queue remembers, for the questions
TOOL_PAYLOAD = 64 << 20  # characters of a PostToolUse payload parsed whole
TAIL_CHARS = 64 << 10  # of a longer one, the end kept, where its tool_use_id is
READ_CHUNK = 1 << 20
QUEUE_WAIT = 1.0  # seconds a hook waits for the queue's lock
COOLDOWN = 15.0  # seconds no hook starts the sender after the hub did not answer (local_sync.COOLDOWN)
SEND_WAIT = 1  # seconds the sender a hook starts waits first, so the images of a burst go together
SWITCH_TTL = 600.0  # seconds the hooks trust the project's switch of tool images as the sender last read it
ASK_WAIT = 3.0  # seconds a question waits for the files of its turn to be recorded
ASK_RESERVE = 4.0  # seconds of the question hook's deadline kept for putting its questions
POLL = 0.1
NAME_CHARS = 120
ARTIFACT_LINK = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+")
_FIELDS = ("session_id", "transcript_path", "cwd", "tool_name", "tool_use_id")
_STRING_FIELD = r'"{}"\s*:\s*"((?:[^"\\]|\\.)*)"'
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def directory() -> Path:
    return hub_dir() / DIRECTORY


# What a payload holds


@dataclass(frozen=True)
class Image:
    """One image block of a tool's result, decoded."""

    data: bytes
    media_type: str
    name: str
    sha256: str

    def reference(self) -> dict:
        """What the tool's event holds in its place."""
        return {
            "type": "output",
            "source": outputs.TOOL,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "size": len(self.data),
            "name": self.name,
        }


@dataclass(frozen=True)
class Sent:
    """A file the session sent on purpose, by its path."""

    path: str
    name: str
    media_type: str
    caption: str | None = None


@dataclass
class Found:
    """What one PostToolUse payload holds for the hub: its images, the files it sent, and its input and result
    without the images, for the event."""

    images: list[Image] = field(default_factory=list)
    files: list[Sent] = field(default_factory=list)
    tool_input: object = None
    response: object = None

    def references(self) -> list[dict]:
        return [image.reference() for image in self.images]


def _image_block(value: dict) -> tuple[str, str] | None:
    """(base64, media type) of an image block in one of IMAGE_SHAPES; None for anything else."""
    if value.get("type") != "image":
        return None
    source, file = value.get("source"), value.get("file")
    if isinstance(source, dict) and source.get("type") == "base64":
        data, media = source.get("data"), source.get("media_type")
    elif isinstance(file, dict):
        data, media = file.get("base64"), file.get("type")
    else:
        data, media = value.get("data"), value.get("mimeType")
    return (data, media) if isinstance(data, str) and isinstance(media, str) else None


def split_images(value, found: list | None = None, depth: int = 0):
    """``value`` with each image block in it replaced by an empty text block, and the (base64, media type) of the
    first MAX_IMAGES of them appended to ``found``. What lies deeper than MAX_DEPTH becomes None."""
    found = [] if found is None else found
    if depth > MAX_DEPTH:
        return None
    if isinstance(value, list):
        return [split_images(item, found, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    block = _image_block(value)
    if block is None:
        return {key: split_images(item, found, depth + 1) for key, item in value.items()}
    if len(found) < MAX_IMAGES:
        found.append(block)
    return {"type": "text", "text": ""}


def safe_name(name: str, fallback: str) -> str:
    """``name`` as an output's name: its last part, its unsafe characters as ``_``, at most NAME_CHARS."""
    base = PurePosixPath(name.replace("\\", "/")).name.strip()
    try:
        return outputs.check_name(base)
    except ValueError:
        cleaned = _UNSAFE_NAME.sub("_", base).strip("._")[:NAME_CHARS]
        return cleaned or fallback


def _image_name(payload: dict, media_type: str, index: int, many: bool) -> str:
    extension = EXTENSIONS.get(media_type, ".img")
    data = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool = str(payload.get("tool_name") or "tool")
    if tool == READ_TOOL and isinstance(data.get("file_path"), str):
        stem = PurePosixPath(data["file_path"].replace("\\", "/")).stem or "image"
    else:
        call = _UNSAFE_NAME.sub("", str(payload.get("tool_use_id") or ""))[-8:]
        stem = "-".join(part for part in (tool.rsplit("__", 1)[-1], call) if part)
    suffix = f"-{index + 1}" if many else ""
    return safe_name(f"{stem[:NAME_CHARS]}{suffix}{extension}", f"image{extension}")


def decode(blocks: list[tuple[str, str]], payload: dict) -> list[Image]:
    """The images of ``blocks`` that decode to an image of a type the web shows (no SVG, which is text), at most
    MAX_FILE_BYTES each, once per SHA-256."""
    images, seen = [], set()
    for index, (data, given) in enumerate(blocks):
        try:
            media = outputs.check_media_type(given)
            raw = base64.b64decode(data, validate=False)
        except (binascii.Error, ValueError):
            continue
        if media not in EXTENSIONS and (outputs.viewer(media) != "image" or media == outputs.SVG):
            continue
        sha256 = hashlib.sha256(raw).hexdigest()
        if not raw or len(raw) > outputs.MAX_FILE_BYTES or sha256 in seen:
            continue
        seen.add(sha256)
        images.append(Image(raw, media, _image_name(payload, media, index, len(blocks) > 1), sha256))
    return images


def caption_of(*parts) -> str | None:
    """A caption of the parts that are text, one per line, without control characters, cut to MAX_CAPTION_CHARS."""
    text = "\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    text = _CONTROL.sub(" ", text).strip()[: outputs.MAX_CAPTION_CHARS].strip()
    return text or None


def _file(path: str, payload: dict, caption: str | None, media_type: str | None = None) -> Sent | None:
    cwd = payload.get("cwd")
    if not os.path.isabs(path):
        if not (isinstance(cwd, str) and os.path.isabs(cwd)):
            return None
        path = os.path.join(cwd, path)
    name = safe_name(path, "file")
    guessed = outputs.guess_media_type(name)
    if guessed == "application/octet-stream" and isinstance(media_type, str):
        with contextlib.suppress(ValueError):
            guessed = outputs.check_media_type(media_type)
    return Sent(os.path.normpath(path), name, guessed, caption)


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _paths(files) -> list[str]:
    if isinstance(files, str):
        return [files]
    return [f for f in files if isinstance(f, str)] if isinstance(files, list) else []


def sent_files(payload: dict) -> list[Sent]:
    """The files of a SendUserFile call: its input's ``files`` (a list, or one path), else the paths of its result's
    ``attachments``, with the input's ``caption``."""
    data, response = _mapping(payload.get("tool_input")), _mapping(payload.get("tool_response"))
    attachments = response.get("attachments") if isinstance(response.get("attachments"), list) else []
    attached = {a["path"]: a for a in attachments if isinstance(a, dict) and isinstance(a.get("path"), str)}
    caption = caption_of(data.get("caption") if isinstance(data.get("caption"), str) else response.get("caption"))
    found = [
        _file(path, payload, caption, attached.get(path, {}).get("media_type"))
        for path in _paths(data.get("files")) or list(attached)
    ]
    return [sent for sent in found if sent is not None][: outputs.MAX_BATCH]


def artifact_files(payload: dict) -> list[Sent]:
    """The page an Artifact call published, with its title, its claude.ai link and the input's description as the
    caption; none for any other action, an asset upload or a result with no claude.ai link."""
    data, response = _mapping(payload.get("tool_input")), payload.get("tool_response")
    if data.get("action", "publish") != "publish" or data.get("asset") or not isinstance(data.get("file_path"), str):
        return []
    url = _mapping(response).get("url")
    if isinstance(url, str):
        link = ARTIFACT_LINK.search(url)
    else:
        link = ARTIFACT_LINK.search(response if isinstance(response, str) else json.dumps(response, default=str))
    if link is None:
        return []
    title = _mapping(response).get("title")
    head = f"{title}: {link.group(0)}" if isinstance(title, str) and title.strip() else link.group(0)
    sent = _file(data["file_path"], payload, caption_of(head, data.get("description")))
    return [sent] if sent is not None else []


def found_in(payload: dict) -> Found:
    """What the PostToolUse ``payload`` holds for the hub (see the module). Never raises: a payload it cannot read
    gives no output, and an event without the tool's input and result."""
    try:
        blocks: list = []
        response = split_images(payload.get("tool_response"), blocks)
        tool_input = split_images(payload.get("tool_input"), blocks)
        tool = payload.get("tool_name")
        files = sent_files(payload) if tool == SEND_TOOL else artifact_files(payload) if tool == ARTIFACT_TOOL else []
        return Found(decode(blocks, payload), files, tool_input, response)
    except Exception:
        return Found()


def _string_field(name: str, text: str, last: bool) -> str | None:
    found = list(re.finditer(_STRING_FIELD.format(name), text))
    if not found:
        return None
    with contextlib.suppress(ValueError):
        return json.loads(f'"{found[-1 if last else 0].group(1)}"')
    return None


def _skeleton(head: str, tail: str, total: int) -> dict:
    """What a payload too long to parse still says: the fields of _FIELDS, found in its start (the tool call's id in
    its end), and a result that names its size."""
    payload: dict = {}
    for name in _FIELDS:
        last = name == "tool_use_id"
        value = _string_field(name, tail if last else head[:TAIL_CHARS], last)
        if value is not None:
            payload[name] = value
    if "tool_name" in payload:
        payload["tool_response"] = f"[evo-hub: a result of {total} characters, too long for this hook to read]"
    return payload


def read_payload(stream=None, limit: int = TOOL_PAYLOAD) -> dict:
    """The PostToolUse payload on stdin, parsed whole up to ``limit`` characters; of a longer one, its ``_skeleton``.
    {} when there is none or it cannot be read."""
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or stream.isatty():
            return {}
        head = stream.read(limit)
        more = stream.read(1) if len(head) >= limit else ""
        if not more:
            data = json.loads(head or "{}")
            return data if isinstance(data, dict) else {}
        tail, total = head[-TAIL_CHARS:] + more, len(head) + 1
        while chunk := stream.read(READ_CHUNK):
            tail, total = (tail + chunk)[-TAIL_CHARS:], total + len(chunk)
    except (OSError, ValueError):
        return {}
    return _skeleton(head, tail, total)


# The queue of a session


def _fresh(url: str) -> dict:
    return {
        "version": VERSION,
        "hub": url,
        "turn": 0,
        "next": 0,
        "tool_images": None,
        "queued_at": 0.0,
        "items": [],
        "recorded": {},
    }


class Queue:
    """``<session id>/queue.json``: the outputs of one session that wait for the hub, the bytes the hook took, and
    the SHA-256s the hub recorded with their output's id. Edited under ``queue.lock``."""

    def __init__(self, session_id: str, base: Path | None = None):
        self.dir = (base or directory()) / session_id
        self.path = self.dir / QUEUE
        self.lock = self.dir / QUEUE_LOCK

    def read(self, url: str | None = None) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return None
        return data if url is None or data.get("hub") == url else None

    def staged(self, sha256: str) -> Path:
        return self.dir / sha256

    @contextlib.contextmanager
    def edit(self, url: str, wait: float = QUEUE_WAIT) -> Iterator[dict]:
        """The queue's data, written back when the block ends without an exception; a queue of another hub starts
        afresh. TimeoutError when another process holds it ``wait`` seconds."""
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            deadline = time.monotonic() + wait
            while not filelock.try_lock(fd):
                if time.monotonic() >= deadline:
                    raise TimeoutError("the queue of outputs is locked")
                time.sleep(0.02)
            data = self.read(url) or _fresh(url)
            yield data
            write_atomic(self.path, json.dumps(data, ensure_ascii=False).encode(), FILE_MODE)
        finally:
            os.close(fd)  # releases the lock

    def unstage(self, data: dict, sha256: str | None) -> None:
        """Delete the bytes of ``sha256`` unless an item of ``data`` still needs them."""
        if sha256 and all(item.get("sha256") != sha256 for item in data["items"]):
            self.staged(sha256).unlink(missing_ok=True)


def _tool_call(value: str | None) -> str | None:
    """The tool call an output came from, when it is one line an output may name."""
    try:
        return outputs.check_line(value[: outputs.MAX_TOOL_CALL_CHARS], outputs.MAX_TOOL_CALL_CHARS, "a tool call")
    except (TypeError, ValueError):
        return None


def _item(data: dict, source: str, name: str, media_type: str, caption: str | None, tool_call: str | None) -> dict:
    data["next"] += 1
    return {
        "key": str(data["next"]),
        "source": source,
        "name": name,
        "media_type": media_type,
        "caption": caption,
        "tool_call_id": _tool_call(tool_call),
        "turn": data["turn"],
        "file": None,
        "sha256": None,
        "size": None,
        "at": time.time(),
    }


def _redacted(text: str | None, token: str) -> str | None:
    from evo_agents.hub.redact import redact

    return caption_of(redact(text, [token])) if text else None


def add(data: dict, queue: Queue, found: Found, token: str, tool_call: str | None) -> list[str]:
    """Put what ``found`` holds in ``data``, the bytes of its images beside it; the keys of the items added. Images
    are left out when the run holds them already, or the sender found the project's switch off less than SWITCH_TTL
    seconds ago (after that the next image is queued, and the sender reads the switch again)."""
    keys, held = [], {item["sha256"] for item in data["items"]} | set(data["recorded"])
    off = data.get("tool_images") is False and 0 <= time.time() - float(data.get("switch_at") or 0) < SWITCH_TTL
    for image in found.images if not off else ():
        if image.sha256 in held or len(data["items"]) >= MAX_QUEUED:
            continue
        path = queue.staged(image.sha256)
        if not path.exists():
            write_atomic(path, image.data, FILE_MODE)
        item = _item(data, outputs.TOOL, image.name, image.media_type, None, tool_call)
        item.update(sha256=image.sha256, size=len(image.data))
        data["items"].append(item)
        held.add(image.sha256)
        keys.append(item["key"])
    for sent in found.files:
        if len(data["items"]) < MAX_QUEUED:
            item = _item(data, outputs.SENT, sent.name, sent.media_type, _redacted(sent.caption, token), tool_call)
            item["file"] = sent.path
            data["items"].append(item)
            keys.append(item["key"])
    if keys:
        data["queued_at"] = time.time()
    return keys


def take(session_id: str, url: str, found: Found, token: str, tool_call: str | None) -> list[str]:
    """What the hook of a tool call does with ``found``: queue it for the session (see the module); the keys of what
    it queued. Never raises: what cannot be written is lost, and the session goes on."""
    if not found.images and not found.files:
        return []
    queue = Queue(session_id)
    try:
        with queue.edit(url) as data:
            return add(data, queue, found, token, tool_call)
    except (OSError, TimeoutError, ValueError):
        return []


def new_turn(session_id: str, url: str) -> None:
    """The person wrote: the files the session sends from now on are this turn's, for its questions."""
    queue = Queue(session_id)
    if queue.read(url) is None:
        return
    with contextlib.suppress(OSError, TimeoutError), queue.edit(url) as data:
        data["turn"] += 1


def waiting(session_id: str, url: str) -> bool:
    """Whether outputs of the session wait for the hub; reads the file only."""
    data = Queue(session_id).read(url)
    return bool(data and data.get("items"))


# The sender in the background


@contextlib.contextmanager
def send_lock(base: Path | None = None, wait: float = 0.0) -> Iterator[bool]:
    """Hold this machine's sender lock; False when another sender held it ``wait`` seconds."""
    base = base or directory()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(base / SEND_LOCK, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        deadline = time.monotonic() + wait
        while not (held := filelock.try_lock(fd)) and time.monotonic() < deadline:
            time.sleep(POLL)
        yield held
    finally:
        os.close(fd)


def sender_running() -> bool:
    with send_lock() as held:
        return not held


def spawn_sender(wait: int = SEND_WAIT) -> None:
    """Start ``evo-agents hub send --queued`` in the background, detached from the hook and from its output."""
    command = [sys.executable, "-m", "evo_agents", "hub", "send", "--queued", "--wait", str(wait)]
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(directory()),  # not the session's directory, whose evo_agents/ (a checkout) -m would import instead
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)  # noqa: S603 - this interpreter, this package


def kick(url: str, now: float, *, urgent: bool = False, wait: int = SEND_WAIT) -> bool:
    """Start the sender unless one runs, or the hub did not answer a hook of this machine in the last COOLDOWN
    seconds (``urgent``: a question waits for it, whatever the cooldown); whether one was started. Never raises."""
    try:
        if not urgent and hooks.HookState(hub_dir()).down_since(url, now, COOLDOWN) is not None:
            return False
        if sender_running():
            return False
        spawn_sender(wait)
    except (OSError, ValueError):
        return False
    return True


def for_question(session_id: str, url: str, deadline: float) -> list[int]:
    """The outputs of source sent of the session since the person's last message, for a question of its dialog: the
    ids the hub recorded, after waiting up to ASK_WAIT seconds, never past ``deadline`` (``time.monotonic``) less
    ASK_RESERVE, for the sender to record those that still wait. At most MAX_ATTACHED."""
    queue = Queue(session_id)
    data = queue.read(url)
    if data is None:
        return []
    until = min(time.monotonic() + ASK_WAIT, deadline - ASK_RESERVE)
    kicked = False
    while time.monotonic() < until and any(_of_turn(item, data) for item in data["items"]):
        if not kicked:
            kicked = True
            kick(url, time.time(), urgent=True, wait=0)
        time.sleep(POLL)
        data = queue.read(url) or data
    found = [record["id"] for record in data["recorded"].values() if _of_turn(record, data)]
    return sorted(found)[: outputs.MAX_ATTACHED]


def _of_turn(entry: dict, data: dict) -> bool:
    """Whether an item or a record of the queue ``data`` is a file sent on purpose in the current turn."""
    return entry.get("source") == outputs.SENT and entry.get("turn") == data["turn"]
