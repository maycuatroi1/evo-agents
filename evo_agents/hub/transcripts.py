"""The transcripts of Claude Code and Codex sessions on this machine, as the hub keeps them: found, read and cut into
parts here; ``evo_agents.hub.transcript_sync`` sends the parts and ``evo_agents.hub.server.transcripts`` keeps them.

A session's transcript is the JSONL file its runtime writes, one JSON object a line, and the hub takes it whole: the
session's own file (``file`` MAIN) and each of its subagents' (``subagents/...``), every line, with no threshold of
messages. Where they are:

- Claude Code: ``<projects>/<slug>/<session id>.jsonl``, and its subagents' under ``<projects>/<slug>/<session
  id>/subagents/`` (``agent-<id>.jsonl``, or deeper for a workflow's agents), read from Claude Code 2.1.2xx. The
  projects directories are ``$CLAUDE_CONFIG_DIR/projects`` (else ``~/.claude/projects``), every ``~/.claude*/projects``
  (another config directory, as a second account uses) and those where a hook found a transcript. ``<slug>`` is the
  directory the session started in, as ``evo_agents.hub.memory.slug`` names it, and decides its project as memories
  decide theirs (``Places``).
- Codex: ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`` (else ``~/.codex``), and ``archived_sessions``. Codex
  compresses an older rollout to ``.jsonl.zst``, which is read through ``compression.zstd`` (Python 3.14), the
  ``zstandard`` module or the ``zstd`` command, whichever is there; without any, the file is skipped and said so. The
  first line, ``session_meta``, names the session (``session_id``), the thread (``id``) and the directory (``cwd``): a
  thread other than the session is a subagent, ``subagents/<thread id>.jsonl``. Read from codex-cli 0.163.

A part is the whole lines from one byte of the file to at most PART_BYTES further (the first line whole however long),
each line redacted on this machine with ``evo_agents.hub.redact.redact_data`` (a line that is not JSON with
``redact``), the machine's hub token among the values replaced, as the digest is. A line redaction leaves unchanged
keeps its bytes; another is written again as compact JSON. A line over MAX_LINE_BYTES becomes a note of its size
(OMITTED). Line N of what the hub holds is line N of the file. The part is gzip of those lines (mtime 0, so the same
lines make the same bytes) and goes as one blob of kind KIND, named by its SHA-256; the hub chains the parts' hashes
into the transcript's (``chain``). A line not finished yet (no line break at the end) waits for the next part, so the
hub never holds half a line.

``~/.evo/hub/transcripts/`` (DIRECTORY) holds what this machine keeps of it: ``state.json`` (per hub, how far into
each file the hub holds it, the projects directories seen and when the last sync ran), ``queue.json`` (the sessions
the hooks named since), ``sync.lock`` (one sync at a time per machine), ``sync.log`` (one line per sync) and
``parts/`` (the parts of a batch until it is sent). Standard library only: the hooks run on a core install.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evo_agents import filelock
from evo_agents.hub.client import FILE_MODE, HubError, hub_dir, load_credentials, write_atomic
from evo_agents.hub.digest import SESSION_ID, is_codex
from evo_agents.hub.memory import SLUG, claude_projects, slug
from evo_agents.hub.redact import redact, redact_data

MiB = 1024 * 1024
KIND = "transcript"  # the blob kind of a part (evo_agents.hub.blobs.KIND_LIMITS)
MAIN = "main"  # the session's own transcript; a subagent's is subagents/...
FILE_KEY = re.compile(r"main|subagents(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}){1,5}")
MAX_FILE_KEY = 512
RUNTIMES = ("claude-code", "codex")
CLAUDE, CODEX = RUNTIMES
PART_BYTES = 8 * MiB  # of the file one part covers at most, unless its first line alone is longer
MAX_LINE_BYTES = 32 * MiB  # a longer line is kept as a note of its size
MAX_PART_BYTES = 48 * MiB  # gzip of one part at most, the blob kind's limit
OMITTED = "evo-hub-omitted"  # the type of the note in place of a line over MAX_LINE_BYTES
META_BYTES = 1 * MiB  # of a rollout's first line read for its session_meta
HEAD_LINES = 50  # lines of a Claude Code transcript read for its directory
MAX_DEPTH = 5  # directories under subagents/
DIRECTORY = "transcripts"  # under ~/.evo/hub
STATE_FILE = "state.json"
QUEUE_FILE = "queue.json"
LOCK_FILE = "sync.lock"
QUEUE_LOCK = "queue.lock"
LOG_FILE = "sync.log"
PARTS_DIR = "parts"
STATE_VERSION = 1
MAX_LOG_BYTES = 256 * 1024  # sync.log is cut to its last half past this
QUEUE_WAIT = 2.0  # seconds a hook waits for the queue's lock
MAX_QUEUE = 500  # sessions the queue keeps, the newest
DISABLE_VARIABLE = "EVO_HUB_TRANSCRIPTS"
RUN_VARIABLE = "EVO_RUN_ID"  # the agent of a worker run: its log is on the hub already
OFF = frozenset({"0", "off", "false", "no"})
SETTLE = 15  # seconds the sync a hook starts waits first, so the turns of a moment go in one part
SWEEP_EVERY = 3600.0  # seconds after which SessionStart starts a sync though nothing is queued
CHUNK = 1 * MiB


class Unreadable(Exception):
    """A transcript this machine cannot read; ``str`` says why, never what it holds."""


@dataclass(frozen=True)
class Found:
    """One transcript file on this machine."""

    path: Path
    session_id: str
    file: str  # MAIN or subagents/...
    runtime: str
    slug: str | None  # the directory its project is decided by; None when it cannot be told
    cwd: str | None
    mtime: float
    size: int  # on disk, compressed for a .zst

    @property
    def key(self) -> str:
        return f"{self.session_id}/{self.file}"

    @property
    def compressed(self) -> bool:
        return self.path.name.endswith(".zst")


@dataclass
class Part:
    """Lines ``first_line`` to ``first_line + lines - 1`` of a transcript, bytes ``start`` to ``end`` of its file,
    redacted and gzipped into ``path``."""

    found: Found
    start: int
    end: int
    first_line: int
    lines: int
    sha256: str
    size: int
    raw_size: int
    path: Path | None
    started_at: str | None
    ended_at: str | None
    cwd: str | None

    def wire(self, machine: str) -> dict:
        """The part as POST .../transcripts/parts takes it."""
        return {
            "session_id": self.found.session_id,
            "file": self.found.file,
            "runtime": self.found.runtime,
            "machine": machine,
            "cwd": self.cwd or self.found.cwd,
            "start": self.start,
            "end": self.end,
            "first_line": self.first_line,
            "lines": self.lines,
            "sha256": self.sha256,
            "size": self.size,
            "raw_size": self.raw_size,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


def chain(previous: str | None, sha256: str) -> str:
    """The hash of a transcript after a part of hash ``sha256``: SHA-256 of the one before it (empty for none) and the
    part's, both as hex."""
    return hashlib.sha256(((previous or "") + sha256).encode()).hexdigest()


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """False when EVO_HUB_TRANSCRIPTS turns the upload off, and for the agent of a worker run."""
    env = os.environ if env is None else env
    if env.get(RUN_VARIABLE, "").strip():
        return False
    return env.get(DISABLE_VARIABLE, "").strip().lower() not in OFF


def directory() -> Path:
    return hub_dir() / DIRECTORY


# Finding transcripts


def claude_roots(extra=(), env: Mapping[str, str] | None = None, home: Path | None = None) -> list[Path]:
    """The Claude Code projects directories of this machine (see the module), those that exist, each once."""
    home = Path.home() if home is None else home
    found = [claude_projects(env)]
    with contextlib.suppress(OSError):
        found += sorted(path / "projects" for path in home.glob(".claude*") if path.is_dir())
    found += [Path(path) for path in extra]
    seen, roots = set(), []
    for root in found:
        real = os.path.realpath(root)
        if real not in seen and root.is_dir():
            seen.add(real)
            roots.append(root)
    return roots


def codex_roots(env: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    base = Path(env["CODEX_HOME"]).expanduser() if env.get("CODEX_HOME") else Path.home() / ".codex"
    return [base / name for name in ("sessions", "archived_sessions") if (base / name).is_dir()]


def _stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def _jsonl(directory: Path, depth: int = MAX_DEPTH) -> Iterator[Path]:
    """The .jsonl files under ``directory``, ``depth`` levels down at most, symlinks left alone."""
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file() and entry.name.endswith(".jsonl"):
            yield Path(entry.path)
        elif entry.is_dir() and depth > 1:
            yield from _jsonl(Path(entry.path), depth - 1)


def claude_session(path: Path, session_id: str, file: str, slug_name: str, since: float | None) -> Found | None:
    stat = _stat(path)
    if stat is None or (since is not None and stat.st_mtime < since):
        return None
    if not FILE_KEY.fullmatch(file) or len(file) > MAX_FILE_KEY:
        return None
    return Found(path, session_id, file, CLAUDE, slug_name, None, stat.st_mtime, stat.st_size)


def claude_subagents(session_dir: Path, session_id: str, slug_name: str, since: float | None) -> Iterator[Found]:
    """The subagents' transcripts of a Claude Code session, from its directory ``<slug>/<session id>``."""
    base = session_dir / "subagents"
    for path in _jsonl(base):
        file = "subagents/" + path.relative_to(base).as_posix()
        found = claude_session(path, session_id, file, slug_name, since)
        if found is not None:
            yield found


def find_claude(root: Path, since: float | None = None) -> Iterator[Found]:
    """The transcripts under one Claude Code projects directory changed at or after ``since`` (any when None)."""
    try:
        slugs = [entry for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return
    for slug_dir in sorted(slugs, key=lambda entry: entry.name):
        if not SLUG.fullmatch(slug_dir.name):
            continue
        try:
            entries = sorted(os.scandir(slug_dir.path), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if entry.is_file(follow_symlinks=False) and name.endswith(".jsonl") and SESSION_ID.fullmatch(name[:-6]):
                found = claude_session(Path(entry.path), name[:-6], MAIN, slug_dir.name, since)
                if found is not None:
                    yield found
            elif entry.is_dir(follow_symlinks=False) and SESSION_ID.fullmatch(name):
                yield from claude_subagents(Path(entry.path), name, slug_dir.name, since)


def codex_meta(path: Path) -> tuple[str, str, str | None] | None:
    """(session id, thread id, directory) of a Codex rollout, from its first line; None when it is not one."""
    with open_source(path) as stream:
        first = stream.readline(META_BYTES)
    try:
        line = json.loads(first.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    payload = line.get("payload") if isinstance(line, dict) and line.get("type") == "session_meta" else None
    if not isinstance(payload, dict):
        return None
    thread = payload.get("id") or payload.get("session_id")
    session = payload.get("session_id") or thread
    if not (isinstance(thread, str) and SESSION_ID.fullmatch(thread) and SESSION_ID.fullmatch(str(session))):
        return None
    cwd = payload.get("cwd")
    return str(session), thread, cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None


def codex_session(path: Path, since: float | None = None) -> Found | None:
    stat = _stat(path)
    if stat is None or (since is not None and stat.st_mtime < since):
        return None
    try:
        meta = codex_meta(path)
    except (OSError, Unreadable):
        return None
    if meta is None:
        return None
    session, thread, cwd = meta
    file = MAIN if thread == session else f"subagents/{thread}.jsonl"
    return Found(path, session, file, CODEX, slug(cwd) if cwd else None, cwd, stat.st_mtime, stat.st_size)


def find_codex(root: Path, since: float | None = None) -> Iterator[Found]:
    """The rollouts under one Codex sessions directory changed at or after ``since``, a .jsonl before its .zst."""
    paths = sorted(path for path in root.rglob("rollout-*.jsonl*") if path.name.endswith((".jsonl", ".jsonl.zst")))
    for path in paths:
        found = codex_session(path, since)
        if found is not None:
            yield found


def given(path: Path) -> list[Found]:
    """The transcripts of the session whose transcript a hook named: a Codex rollout, or a Claude Code transcript with
    its subagents'."""
    stat = _stat(path)
    if stat is None or not path.name.endswith((".jsonl", ".jsonl.zst")):
        return []
    try:
        with open_source(path) as stream:
            first = stream.readline(META_BYTES)
    except (OSError, Unreadable):
        return []
    try:
        head = json.loads(first.decode("utf-8", errors="replace"))
    except ValueError:
        head = None
    if is_codex(head):
        found = codex_session(path)
        return [found] if found is not None else []
    session_id = path.name[: -len(".jsonl")] if path.name.endswith(".jsonl") else ""
    if not SESSION_ID.fullmatch(session_id) or not SLUG.fullmatch(path.parent.name):
        return []
    main = Found(path, session_id, MAIN, CLAUDE, path.parent.name, None, stat.st_mtime, stat.st_size)
    return [main, *claude_subagents(path.parent / session_id, session_id, path.parent.name, None)]


def find_all(since: float | None = None, extra_roots=()) -> list[Found]:
    """Every transcript of this machine changed at or after ``since``, each (session, file) once: a Claude Code root
    before another, a .jsonl before the .zst of the same thread."""
    found: dict[str, Found] = {}
    for root in claude_roots(extra_roots):
        for item in find_claude(root, since):
            found.setdefault(item.key, item)
    for root in codex_roots():
        for item in find_codex(root, since):
            held = found.get(item.key)
            if held is None or (held.compressed and not item.compressed):
                found[item.key] = item
    return sorted(found.values(), key=lambda item: (item.mtime, item.key))


# Reading


def _zstd_module():
    for name in ("compression.zstd", "zstandard"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


@contextlib.contextmanager
def open_source(path: Path):
    """The bytes of a transcript as a binary stream from its start: the file, or a .zst decompressed. Unreadable when
    no zstd decoder is there."""
    if not path.name.endswith(".zst"):
        with open(path, "rb") as handle:
            yield handle
        return
    module = _zstd_module()
    if module is not None and module.__name__ == "compression.zstd":
        with module.open(path, "rb") as handle:
            yield handle
        return
    if module is not None:
        with open(path, "rb") as raw, module.ZstdDecompressor().stream_reader(raw) as handle:
            yield io.BufferedReader(handle)
        return
    program = shutil.which("zstd")
    if program is None:
        raise Unreadable(f"{path.name} is zstd-compressed and no zstd decoder is here (`brew install zstd`)")
    proc = subprocess.Popen([program, "-dcq", "--", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield proc.stdout
    finally:
        proc.stdout.close()
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait()


def _skip(stream, offset: int) -> int:
    """Move ``stream`` ``offset`` bytes on; the bytes it moved."""
    with contextlib.suppress(OSError, ValueError, io.UnsupportedOperation):
        if stream.seekable():
            stream.seek(offset)
            return stream.tell()
    moved = 0
    while moved < offset:
        chunk = stream.read(min(CHUNK, offset - moved))
        if not chunk:
            break
        moved += len(chunk)
    return moved


def source_size(found: Found) -> int:
    """The bytes of a transcript the hub can hold: those of its whole lines, up to its last line break, of the file or
    of a .zst decompressed. A line still being written (or left so by a crash) does not count until it ends."""
    if not found.compressed:
        with open(found.path, "rb") as handle:
            end = handle.seek(0, os.SEEK_END)
            while end > 0:
                start = max(0, end - CHUNK)
                handle.seek(start)
                at = handle.read(end - start).rfind(b"\n")
                if at >= 0:
                    return start + at + 1
                end = start
        return 0
    total, whole = 0, 0
    with open_source(found.path) as stream:
        while chunk := stream.read(CHUNK):
            at = chunk.rfind(b"\n")
            if at >= 0:
                whole = total + at + 1
            total += len(chunk)
    return whole


def _line(stream) -> tuple[bytes | None, int, bool]:
    """The next line of ``stream``: (its bytes, None for one over MAX_LINE_BYTES; its length; whether it ended with a
    line break)."""
    raw = stream.readline(MAX_LINE_BYTES + 1)
    if raw.endswith(b"\n") or not raw:
        return raw, len(raw), bool(raw)
    if len(raw) <= MAX_LINE_BYTES:
        return raw, len(raw), False  # the end of the file, in the middle of a line still being written
    length = len(raw)
    while True:
        rest = stream.readline(CHUNK)
        length += len(rest)
        if rest.endswith(b"\n"):
            return None, length, True
        if not rest:
            return None, length, False


def _moment(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def _cwd_of(data) -> str | None:
    if not isinstance(data, dict):
        return None
    for holder in (data, data.get("payload")):
        if isinstance(holder, dict) and isinstance(holder.get("cwd"), str) and os.path.isabs(holder["cwd"]):
            return holder["cwd"]
    return None


def clean_line(raw: bytes, values: list[str]) -> tuple[bytes, object]:
    """One line of a transcript redacted (see the module), with its JSON value (None when it is not JSON)."""
    try:
        text, lossless = raw.decode("utf-8"), True
    except UnicodeDecodeError:
        text, lossless = raw.decode("utf-8", errors="replace"), False
    body = text[:-1] if text.endswith("\n") else text
    if not body.strip():
        return raw, None
    try:
        data = json.loads(body)
    except ValueError:
        return (redact(body, values) + "\n").encode(), None
    cleaned = redact_data(data, values)
    if lossless and cleaned == data:
        return raw, data
    return (json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")) + "\n").encode(), cleaned


def read_part(
    found: Found, start: int, first_line: int, values: list[str], into: Path | None = None, limit: int = PART_BYTES
) -> Part | None:
    """The part of ``found`` from byte ``start`` (line ``first_line``): whole lines up to ``limit`` bytes, the first
    whole however long, redacted and gzipped into a file under ``into`` (kept in memory with none). None when no whole
    line follows ``start``. Raises OSError and Unreadable."""
    out = io.BytesIO()
    consumed, lines, first, last, cwd = 0, 0, None, None, None
    with open_source(found.path) as stream:
        if _skip(stream, start) < start:
            return None  # the file is shorter than what the hub holds of it
        while consumed < limit:
            raw, length, ended = _line(stream)
            if not ended:
                break
            consumed += length
            lines += 1
            if raw is None:
                out.write((json.dumps({"type": OMITTED, "bytes": length}) + "\n").encode())
                continue
            text, data = clean_line(raw, values)
            out.write(text)
            moment = _moment(data.get("timestamp")) if isinstance(data, dict) else None
            if moment is not None:
                first = moment if first is None or moment < first else first
                last = moment if last is None or moment > last else last
            cwd = cwd or _cwd_of(data)
    if not lines:
        return None
    raw_text = out.getvalue()
    packed = gzip.compress(raw_text, compresslevel=6, mtime=0)
    sha256 = hashlib.sha256(packed).hexdigest()
    path = None
    if into is not None:
        into.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = into / f"{sha256}.gz"
        write_atomic(path, packed, FILE_MODE)
    return Part(
        found,
        start,
        start + consumed,
        first_line,
        lines,
        sha256,
        len(packed),
        len(raw_text),
        path,
        first.isoformat() if first else None,
        last.isoformat() if last else None,
        cwd,
    )


# What this machine keeps


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("version") == STATE_VERSION else {}


def _write_json(path: Path, data: dict) -> None:
    with contextlib.suppress(OSError):  # an optimization: the hub says again what it holds
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(path, (json.dumps(data, indent=1, sort_keys=True) + "\n").encode(), FILE_MODE)


class State:
    """``state.json``: per hub URL, per transcript (``<session>/<file>``) the file and how far into it the hub holds
    it (``offset``, ``line``, the next line's number), with the file's size and mtime then; the Claude Code projects
    directories hooks named; and when a sync last ran. Advisory: a file that cannot be read counts as empty."""

    def __init__(self, base: Path, hub_url: str):
        self.path = base / STATE_FILE
        self.hub_url = hub_url
        self.data = _read_json(self.path) or {"version": STATE_VERSION, "hubs": {}, "roots": []}
        hubs = self.data.setdefault("hubs", {})
        held = hubs.get(hub_url) if isinstance(hubs.get(hub_url), dict) else {}
        hubs[hub_url] = held
        self.files: dict[str, dict] = held.setdefault("files", {})
        if not isinstance(self.files, dict):
            self.files = held["files"] = {}

    @property
    def roots(self) -> list[str]:
        roots = self.data.get("roots")
        return [root for root in roots if isinstance(root, str)] if isinstance(roots, list) else []

    def add_root(self, root: Path) -> None:
        if str(root) not in self.roots:
            self.data["roots"] = [*self.roots, str(root)]

    @property
    def synced_at(self) -> float | None:
        value = self.data["hubs"][self.hub_url].get("synced_at")
        return value if isinstance(value, (int, float)) else None

    def mark_synced(self, at: float) -> None:
        self.data["hubs"][self.hub_url]["synced_at"] = at

    def get(self, found: Found) -> dict:
        entry = self.files.get(found.key)
        return entry if isinstance(entry, dict) else {}

    def unchanged(self, found: Found) -> bool:
        """Whether the hub held the whole file the last time this machine looked, and the file is as it was then."""
        entry = self.get(found)
        return bool(entry.get("complete")) and (entry.get("size"), entry.get("mtime")) == (found.size, found.mtime)

    def put(self, found: Found, **fields) -> None:
        entry = {**self.get(found), "path": str(found.path), "size": found.size, "mtime": found.mtime, **fields}
        self.files[found.key] = entry

    def save(self) -> None:
        _write_json(self.path, self.data)


class Queue:
    """``queue.json``: the sessions the Stop and SessionEnd hooks named, with their transcript, until a sync took them.
    A hook adds under ``queue.lock``, waiting QUEUE_WAIT seconds at most; a sync drops what it handled."""

    def __init__(self, base: Path):
        self.base = base
        self.path = base / QUEUE_FILE

    @contextlib.contextmanager
    def _locked(self, wait: float = QUEUE_WAIT):
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.base / QUEUE_LOCK, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            deadline = time.monotonic() + wait
            while not filelock.try_lock(fd):
                if time.monotonic() >= deadline:
                    raise TimeoutError("the queue of transcripts is locked")
                time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def entries(self) -> dict[str, dict]:
        sessions = _read_json(self.path).get("sessions")
        return {key: value for key, value in (sessions or {}).items() if isinstance(value, dict)}

    def add(self, session_id: str, transcript: str, ended: bool, now: float) -> None:
        with self._locked():
            sessions = self.entries()
            held = sessions.get(session_id, {})
            sessions[session_id] = {"transcript": transcript, "ended": ended or bool(held.get("ended")), "at": now}
            newest = sorted(sessions.items(), key=lambda item: item[1].get("at") or 0, reverse=True)[:MAX_QUEUE]
            _write_json(self.path, {"version": STATE_VERSION, "sessions": dict(newest)})

    def drop(self, taken: Mapping[str, dict]) -> None:
        """Drop the entries of ``taken`` that no hook named again since they were taken."""
        with self._locked(wait=10.0):
            sessions = self.entries()
            for key, entry in taken.items():
                if sessions.get(key, {}).get("at") == entry.get("at"):
                    sessions.pop(key, None)
            _write_json(self.path, {"version": STATE_VERSION, "sessions": sessions})


def log_line(base: Path, text: str) -> None:
    """Append one line to ``sync.log``, which keeps its newer half once past MAX_LOG_BYTES."""
    with contextlib.suppress(OSError):
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = base / LOG_FILE
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {' '.join(text.split())}\n")
        if path.stat().st_size > MAX_LOG_BYTES:
            kept = path.read_bytes()[-MAX_LOG_BYTES // 2 :]
            write_atomic(path, kept[kept.find(b"\n") + 1 :], FILE_MODE)


# The hooks' side


@contextlib.contextmanager
def sync_lock(base: Path, wait: float = 0.0):
    """Hold this machine's sync lock; yields False when another sync held it for ``wait`` seconds."""
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(base / LOCK_FILE, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        deadline = time.monotonic() + wait
        while not (held := filelock.try_lock(fd)) and time.monotonic() < deadline:
            time.sleep(0.2)
        yield held
    finally:
        os.close(fd)  # releases the lock


def sync_running(base: Path) -> bool:
    with sync_lock(base) as held:
        return not held


def spawn_sync(base: Path, wait: int = SETTLE) -> bool:
    """Start ``evo-agents hub sessions sync`` in the background, detached from the hook and from its output, unless a
    sync of this machine runs already (it takes the queue again before it ends); whether one was started."""
    if sync_running(base):
        return False
    command = [sys.executable, "-m", "evo_agents", "hub", "sessions", "sync", "--wait", str(wait)]
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(base),  # not the session's directory, whose evo_agents/ (a checkout) -m would import instead
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)  # noqa: S603 - this interpreter, this package
    return True


def _signed_in() -> bool:
    try:
        load_credentials()
    except HubError:
        return False
    return True


def on_turn(payload: dict, *, ended: bool = False, now: float | None = None) -> None:
    """What Stop (and, with ``ended``, SessionEnd) does for the session's transcript: queue it and start a sync in the
    background. Sends nothing itself and takes a few milliseconds; nothing when not signed in, turned off, in a worker
    run, or without a session id and an absolute transcript path."""
    session_id, transcript = payload.get("session_id"), payload.get("transcript_path")
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return
    if not isinstance(transcript, str) or not os.path.isabs(transcript) or not enabled() or not _signed_in():
        return
    base = directory()
    with contextlib.suppress(OSError, TimeoutError):
        Queue(base).add(session_id, transcript, ended, time.time() if now is None else now)
    with contextlib.suppress(OSError):
        spawn_sync(base)


def on_session_start(now: float | None = None) -> None:
    """SessionStart's share: a sync in the background when sessions wait in the queue, or the last sync of this
    machine was SWEEP_EVERY ago or more. A machine that never synced waits for its first Stop, and writes nothing."""
    if not enabled():
        return
    try:
        hub_url = load_credentials().url
    except HubError:
        return
    base = directory()
    now = time.time() if now is None else now
    synced = State(base, hub_url).synced_at
    if Queue(base).entries() or (synced is not None and now - synced >= SWEEP_EVERY):
        with contextlib.suppress(OSError):
            spawn_sync(base)
