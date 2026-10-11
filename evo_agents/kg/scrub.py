"""Old copies of secrets in a kg home: find them and count them, and on an explicit request delete them.

The git connector redacts each secret before a file enters the corpus (``evo_agents.kg.redact``). What a corpus took
in before it did stays on disk until this module deletes it. ``scan_project`` reads the directory of one project,
``<kg home>/<project>/``, with the connector's own patterns (``redact_file``), and ``purge`` deletes what it found
that nothing uses any more. Neither ever returns, prints or logs a value: a finding names a blob by its hash, the
paths and item ids that refer to it, and the number of secrets of each kind it holds.

What is read, and what a purge does with it:

- ``blobs/sha256/``: the text of items, plain. A blob is read as the file the items of the run logs say it is (its
  path decides whether a bare value after ``=`` is an expression or a literal, as in the connector); a blob no log
  names is read as a document. A blob holding a secret is **live** when an item of the merged state
  (``corpus.sqlite``) refers to it: the graph is built from it, so it stays until a sync of its source replaces it
  with a redacted copy. It is **kept** when only an older record of a run log still in use refers to it, and **old**
  otherwise. A purge deletes the old ones.
- ``log/<run>.jsonl.gz``: refers to blobs by hash, and holds no text. A log is in use while one of its items or
  tombstones is the merged state's record, or while its run is still being written. A purge deletes each log not in
  use that refers to an old blob, so no log left names a blob that is gone: replaying the logs still gives the merged
  state (none of the records deleted won it), and a push never asks for a missing blob.
- ``memo.sqlite``: the memoized outputs of the build, a cache. A purge deletes every row whose output holds a secret;
  the next build computes it again.
- ``graph.sqlite``: the graph, rewritten by every build from the merged state. Its rows holding a secret are counted;
  they go once the corpus is clean and ``kg build`` has run. A purge changes no row of it.
- ``_shared/memo.sqlite``, the memo some projects share (``evo_agents.kg.build.memo_path``), counts as the project
  ``_shared``.

A purge then compacts memo.sqlite and graph.sqlite (the full-text index merged, VACUUM, the WAL truncated), so text
that a delete or an earlier build left in free pages is gone from the files too, and appends a ``scrub`` line to the
project's audit log with the counts. It holds the lock of every source of the project while it runs, so no sync
writes a blob or a log meanwhile.

Standard library and the kg package only: it runs on a core install, and on the hub's worker cache, which is laid out
as a kg home.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import re
import sqlite3
import time
import zlib
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evo_agents import __version__, filelock
from evo_agents.kg import audit
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.ids import utc_now
from evo_agents.kg.redact import redact_file
from evo_agents.secret_patterns import PROVIDER_KEYS

OLD, LIVE, KEPT = "old", "live", "kept"
STATES = (OLD, LIVE, KEPT)
NOT_PROJECTS = frozenset({"sessions"})  # directories of a kg home that are not a project's
BLOB_REF = re.compile(r"sha256:([0-9a-f]{64})")
SHA256 = re.compile(r"[0-9a-f]{64}")
MIN_STRING = 8  # a string shorter than this holds no secret the patterns take
# What any text the patterns of evo_agents.kg.redact take must hold (``might_hold_secret``): in lower case, the end of
# a secret's name before its assignment or a PEM header; or a URI with a password; or the prefix of a provider's key.
# Text without any of it is not read further, which makes a scan several times faster. A pattern added there must add
# its condition here: tests/kg/test_scrub.py checks every case of tests/kg/test_redact.py.
NAME_END = re.compile(r"(?:word|wd|phrase|secret|[iset_-]key|token)[\"']?[ \t]*[=:]")  # on the lower-cased text
URI_WITH_PASSWORD = re.compile(r"://[^:/@\s]*:[^@\s/]+@")
# A part of each provider's prefix (evo_agents.secret_patterns.PROVIDER_KEYS): a fine-grained GitHub token holds _pat_.
KEY_PREFIXES = ("sk-", "AKIA", "ASIA", "ABIA", "ACCA", "A3T", "xox", "AIza", "_pat_")
KEY_PREFIXES += tuple(f"gh{kind}_" for kind in "opsur") + tuple(f"ev{kind}_" for kind in "hsw")
GRAPH_QUERIES = (  # every row of the graph that holds text, current or not
    "SELECT name, body FROM node_fts",
    "SELECT name, props FROM nodes",
    "SELECT props, NULL FROM edges",
)


class ScrubBusy(RuntimeError):
    """A sync of the project is running: its lock is held."""


# Reading text


def might_hold_secret(text: str) -> bool:
    """Whether ``text`` holds what every secret the patterns take holds (see NAME_END); False means it holds none."""
    low = text.lower()
    if "private key" in low or NAME_END.search(low):
        return True
    if "://" in text and URI_WITH_PASSWORD.search(text):
        return True
    if not any(prefix in text for prefix in KEY_PREFIXES):
        return False
    return any(pattern.search(text) for _, pattern in PROVIDER_KEYS)


def secrets_in(text: str, path: str = "") -> dict[str, int]:
    """The secrets the git connector would redact from ``text``, read as the file at ``path``: kind -> count."""
    if not might_hold_secret(text):
        return {}
    return redact_file(text, path)[1]


def _strings(value) -> Iterator[str]:
    if isinstance(value, str):
        if len(value) >= MIN_STRING:
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _most(*found: dict[str, int]) -> dict[str, int]:
    """Per kind, the highest count of ``found``: two readings of one text count a secret once."""
    most: dict[str, int] = {}
    for counts in found:
        for kind, count in counts.items():
            most[kind] = max(most.get(kind, 0), count)
    return dict(sorted(most.items()))


def secrets_in_json(text: str) -> dict[str, int]:
    """The secrets of a JSON document: in its text, where a key names its value, and in each string it holds, which
    may be a file's text with its own lines."""
    try:
        value = json.loads(text)
    except ValueError:
        return secrets_in(text)
    return _most(secrets_in(text), secrets_in("\n".join(_strings(value))))


def blob_refs(record: dict) -> list[str]:
    """The hex SHA-256 of every blob an item record of a run log refers to: its body and its fragments."""
    refs = []
    body = record.get("body")
    for part in [body] + list(record.get("fragments") or []):
        if isinstance(part, dict) and isinstance(part.get("blob"), str):
            found = BLOB_REF.fullmatch(part["blob"])
            if found:
                refs.append(found[1])
    return refs


@dataclass
class RunSummary:
    """What one run log says about the blobs of its source."""

    run_id: str | None
    source: str | None
    refs: set[str]  # the blobs its items refer to
    full: bool  # a complete listing and an ok end: every item of the source as the run saw it, and no other


def run_summary(path: Path) -> RunSummary:
    """Read the run log at ``path`` (jsonl, or jsonl.gz) for its blobs and whether the run was full."""
    summary = RunSummary(None, None, set(), False)
    listed = ended = False
    for record in Corpus.read_log(path):
        kind = record.get("type")
        if kind == "run":
            summary.run_id, summary.source = record.get("run_id"), record.get("source")
        elif kind == "item":
            summary.refs.update(blob_refs(record))
        elif kind == "listing":
            listed = listed or record.get("complete") is True
        elif kind == "run_end":
            ended = record.get("status") == "ok"
    summary.full = listed and ended
    return summary


def graph_secrets(path: Path, *, immutable: bool = False) -> tuple[int, dict[str, int]]:
    """The rows of the graph file at ``path`` holding a secret, and the secrets by kind: the full-text index of the
    nodes (name and text), and the name and properties of every node and edge, current or not."""
    mode = "mode=ro&immutable=1" if immutable else "mode=ro"
    db = sqlite3.connect(f"{path.resolve().as_uri()}?{mode}", uri=True)
    rows, kinds = 0, Counter()
    try:
        for query in GRAPH_QUERIES:
            try:
                found = db.execute(query)
            except sqlite3.OperationalError:  # a store from another schema: what it lacks holds nothing
                continue
            for first, second in found:
                counts = _most(secrets_in(first or ""), secrets_in_json(second) if second else {})
                if counts:
                    rows += 1
                    kinds.update(counts)
    finally:
        db.close()
    return rows, dict(sorted(kinds.items()))


# What a scan finds


@dataclass
class BlobFinding:
    sha256: str
    size: int
    state: str  # old, live or kept (see the module)
    secrets: dict[str, int]  # kind -> count, never a value
    paths: list[str] = field(default_factory=list)  # the paths the items referring to it gave
    items: list[str] = field(default_factory=list)  # the ids of those items


@dataclass
class ProjectScan:
    project: str
    root: Path
    scanned: int = 0  # blobs read
    blobs: list[BlobFinding] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)  # file names of the run logs a purge deletes
    memo: list[str] = field(default_factory=list)  # keys of the memo rows holding a secret
    memo_secrets: dict[str, int] = field(default_factory=dict)
    graph_rows: int = 0
    graph_secrets: dict[str, int] = field(default_factory=dict)
    deleted: dict | None = None  # what a purge deleted; None when nothing was purged
    warnings: list[str] = field(default_factory=list)

    def found(self, state: str) -> list[BlobFinding]:
        return [finding for finding in self.blobs if finding.state == state]

    def counts(self) -> dict[str, int]:
        counts = {state: len(self.found(state)) for state in STATES}
        return {**counts, "logs": len(self.logs), "memo": len(self.memo), "graph": self.graph_rows}

    def secrets(self) -> dict[str, int]:
        """The secrets of every blob found, by kind."""
        total: Counter[str] = Counter()
        for finding in self.blobs:
            total.update(finding.secrets)
        return dict(sorted(total.items()))

    @property
    def clean(self) -> bool:
        return not (self.blobs or self.memo or self.graph_rows)

    def to_json(self) -> dict:
        return {
            "project": self.project,
            "root": str(self.root),
            "scanned": self.scanned,
            "counts": self.counts(),
            "secrets": self.secrets(),
            "blobs": [asdict(finding) for finding in self.blobs],
            "logs": self.logs,
            "memo": {"rows": len(self.memo), "secrets": self.memo_secrets},
            "graph": {"rows": self.graph_rows, "secrets": self.graph_secrets},
            "deleted": self.deleted,
            "warnings": self.warnings,
        }


# Where to look


def project_dirs(home: Path) -> list[tuple[str, Path]]:
    """(name, directory) of every project under the kg home ``home``: a directory holding a corpus, a memo or a
    graph. The shared memo comes as the project ``_shared``."""
    found = []
    if not home.is_dir():
        return found
    for path in sorted(home.iterdir()):
        if not path.is_dir() or path.name in NOT_PROJECTS:
            continue
        markers = ("log", "blobs", "corpus.sqlite", "memo.sqlite", "graph.sqlite")
        if any((path / marker).exists() for marker in markers):
            found.append((path.name, path))
    return found


@dataclass
class _Logs:
    """What the run logs of a project say about its blobs, read once."""

    files: list[Path] = field(default_factory=list)
    in_use: set[str] = field(default_factory=set)  # file names of the logs in use
    used_refs: set[str] = field(default_factory=set)  # blobs a log in use refers to
    paths: dict[str, set[str]] = field(default_factory=dict)  # blob -> paths
    items: dict[str, set[str]] = field(default_factory=dict)  # blob -> item ids


def _winners(root: Path) -> tuple[dict[str, str], set[str]]:
    """The merged state of the corpus: the run whose record won, by item id, and the blobs the live items refer to."""
    path = root / "corpus.sqlite"
    winners: dict[str, str] = {}
    live: set[str] = set()
    if not path.exists():
        return winners, live
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        for item_id, deleted, run_id, record in db.execute("SELECT item_id, deleted, k_run, record FROM items"):
            winners[item_id] = run_id
            if not deleted:
                live.update(blob_refs(json.loads(record)))
    finally:
        db.close()
    return winners, live


def _log_files(root: Path) -> list[Path]:
    directory = root / "log"
    if not directory.is_dir():
        return []
    return sorted([*directory.glob("*.jsonl.gz"), *directory.glob("*.jsonl")])


def _read_logs(root: Path, winners: dict[str, str]) -> _Logs:
    logs = _Logs(files=_log_files(root))
    for path in logs.files:
        run_id, refs = None, set()
        in_use = path.suffix == ".jsonl"  # still being written by a sync
        for record in Corpus.read_log(path):
            kind = record.get("type")
            if kind == "run":
                run_id = record.get("run_id")
            if kind not in ("item", "tombstone"):
                continue
            if run_id is not None and winners.get(record.get("id")) == run_id:
                in_use = True
            for ref in blob_refs(record) if kind == "item" else ():
                refs.add(ref)
                logs.paths.setdefault(ref, set()).add(str((record.get("props") or {}).get("path") or ""))
                logs.items.setdefault(ref, set()).add(str(record.get("id")))
        if in_use:
            logs.in_use.add(path.name)
            logs.used_refs |= refs
    return logs


def _blob_files(root: Path) -> Iterator[tuple[str, Path]]:
    directory = root / "blobs" / "sha256"
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*/*")):
        sha = path.parent.name + path.name
        if SHA256.fullmatch(sha) and path.is_file():
            yield sha, path


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _first_found(text: str, paths: list[str]) -> dict[str, int]:
    """The secrets of ``text`` read as the first of ``paths`` (the files that hold it) where it holds any."""
    for path in paths:
        found = secrets_in(text, path)
        if found:
            return found
    return {}


def _scan_blobs(scan: ProjectScan, logs: _Logs, live: set[str]) -> None:
    for sha, path in _blob_files(scan.root):
        text = _read_text(path)
        if text is None:
            continue
        scan.scanned += 1
        paths = sorted(name for name in logs.paths.get(sha, ()) if name) or [""]  # no path: read as a document
        found = _first_found(text, paths)
        if not found:
            continue
        state = LIVE if sha in live else KEPT if sha in logs.used_refs else OLD
        items = sorted(logs.items.get(sha) or ())
        scan.blobs.append(BlobFinding(sha, path.stat().st_size, state, found, [p for p in paths if p], items))


def _old_logs(logs: _Logs, old: set[str]) -> list[str]:
    """The logs not in use that refer to one of the ``old`` blobs."""
    doomed = []
    for path in logs.files:
        if path.name in logs.in_use or not old:
            continue
        records = Corpus.read_log(path)
        if any(old.intersection(blob_refs(r)) for r in records if r.get("type") == "item"):
            doomed.append(path.name)
    return doomed


def _scan_memo(path: Path) -> tuple[list[str], dict[str, int]]:
    if not path.exists():
        return [], {}
    keys, kinds = [], Counter()
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        for key, output in db.execute("SELECT key, output FROM memo ORDER BY key"):
            try:
                text = gzip.decompress(output).decode("utf-8")
            except (OSError, EOFError, zlib.error, UnicodeDecodeError):
                continue
            counts = secrets_in_json(text)
            if counts:
                keys.append(key)
                kinds.update(counts)
    except sqlite3.OperationalError:
        return [], {}
    finally:
        db.close()
    return keys, dict(sorted(kinds.items()))


def scan_project(name: str, root: Path) -> ProjectScan:
    """Read the kg directory ``root`` of project ``name`` and say what holds a secret (see the module). Changes
    nothing."""
    scan = ProjectScan(name, root)
    winners, live = _winners(root)
    logs = _read_logs(root, winners)
    _scan_blobs(scan, logs, live)
    scan.logs = _old_logs(logs, {finding.sha256 for finding in scan.found(OLD)})
    scan.memo, scan.memo_secrets = _scan_memo(root / "memo.sqlite")
    graph = root / "graph.sqlite"
    if graph.exists():
        try:
            scan.graph_rows, scan.graph_secrets = graph_secrets(graph)
        except sqlite3.DatabaseError as exc:
            scan.warnings.append(f"graph.sqlite was not read: {type(exc).__name__}")
    return scan


def sources_of(root: Path) -> set[str]:
    """The sources of the project at ``root`` whose lock a purge holds: those its runs and lock files name."""
    found = {path.stem for path in (root / "locks").glob("*.lock")} if (root / "locks").is_dir() else set()
    path = root / "corpus.sqlite"
    if path.exists():
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            found |= {row[0] for row in db.execute("SELECT DISTINCT source FROM runs")}
        except sqlite3.OperationalError:
            pass
        finally:
            db.close()
    for log in _log_files(root):
        with contextlib.closing(Corpus.read_log(log)) as records:
            header = next(records, {})
        if header.get("type") == "run" and header.get("source"):
            found.add(str(header["source"]))
    return {source for source in found if re.fullmatch(r"[A-Za-z0-9_.-]+", source)}


@contextlib.contextmanager
def holding_sources(root: Path):
    """Hold the lock of every source of the project at ``root`` (``sources_of``), as a sync does for its own; raises
    ScrubBusy when a sync holds one."""
    with contextlib.ExitStack() as stack:
        directory = root / "locks"
        for source in sorted(sources_of(root)):
            directory.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context(open(directory / f"{source}.lock", "a", encoding="utf-8"))
            if not filelock.try_lock(handle.fileno()):
                raise ScrubBusy(f"a sync of source {source!r} of {root.name} is running: try again once it ended")
            stack.callback(filelock.unlock, handle.fileno())
        yield


# Purging


def _purge_memo(path: Path, keys: list[str]) -> int:
    if not keys or not path.exists():
        return 0
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA secure_delete = ON")
        with db:
            deleted = sum(db.execute("DELETE FROM memo WHERE key = ?", (key,)).rowcount for key in keys)
    finally:
        db.close()
    return deleted


def compact(path: Path, *, fts: str | None = None) -> str | None:
    """Rewrite the SQLite file at ``path`` without free pages, the full-text index ``fts`` merged first, and empty its
    WAL; a warning when a reader kept the WAL from being emptied, None otherwise."""
    if not path.exists():
        return None
    db = sqlite3.connect(path, timeout=30)
    try:
        if fts and db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (fts,)).fetchone():
            with db:
                db.execute(f"INSERT INTO {fts}({fts}) VALUES ('optimize')")
        db.execute("VACUUM")
        busy = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError as exc:
        return f"{path.name} was not compacted: {exc}"
    finally:
        db.close()
    if busy and busy[0]:
        return f"{path.name} still has a WAL a reader holds: close kg serve and run the purge again to empty it"
    return None


def purge(scan: ProjectScan) -> dict:
    """Delete what ``scan`` found that nothing uses (see the module), compact the memo and the graph, and append a
    ``scrub`` line to the project's audit log; what went. Call it holding the project's sources
    (``holding_sources``), with a scan made while holding them."""
    started = utc_now()
    clock = time.monotonic()
    old = scan.found(OLD)
    for finding in old:
        (scan.root / "blobs" / "sha256" / finding.sha256[:2] / finding.sha256[2:]).unlink(missing_ok=True)
    for name in scan.logs:
        (scan.root / "log" / name).unlink(missing_ok=True)
    memo = _purge_memo(scan.root / "memo.sqlite", scan.memo)
    for warning in (compact(scan.root / "memo.sqlite"), compact(scan.root / "graph.sqlite", fts="node_fts")):
        if warning:
            scan.warnings.append(warning)
    scan.deleted = {
        "blobs": len(old),
        "bytes": sum(finding.size for finding in old),
        "logs": len(scan.logs),
        "memo": memo,
        "secrets": dict(sorted(sum((Counter(f.secrets) for f in old), Counter()).items())),
        "seconds": round(time.monotonic() - clock, 3),
    }
    _record(scan, started)
    return scan.deleted


def _record(scan: ProjectScan, started: str) -> None:
    deleted = scan.deleted or {}
    audit.append(
        scan.root,
        {
            "v": audit.AUDIT_VERSION,
            "event": "scrub",
            "project": scan.project,
            "started_at": started,
            "finished_at": utc_now(),
            "trigger": audit.trigger(),
            "evo_agents": __version__,
            "blobs": deleted.get("blobs", 0),
            "bytes": deleted.get("bytes", 0),
            "logs": deleted.get("logs", 0),
            "memo": deleted.get("memo", 0),
            "live": len(scan.found(LIVE)),
            "kept": len(scan.found(KEPT)),
            "graph": scan.graph_rows,
            "secrets": deleted.get("secrets", {}),
        },
    )


def totals(scans: Iterable[ProjectScan]) -> dict[str, int]:
    """The counts of ``scans`` added up, and the bytes of the old blobs."""
    total: Counter[str] = Counter()
    for scan in scans:
        total.update(scan.counts())
        total["bytes"] += sum(finding.size for finding in scan.found(OLD))
    return {key: total[key] for key in (*STATES, "logs", "memo", "graph", "bytes")}
