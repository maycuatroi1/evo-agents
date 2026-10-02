"""Corpus of one project: the append-only log, content-addressed blobs, and the merged state.

Layout under ``$EVO_KG_HOME/<project>/`` (default ``~/.evo/kg``), mode 0700::

    log/<run_id>.jsonl.gz   every message of one connector run, item text replaced by blob refs
    blobs/sha256/ab/cd...   item bodies and fragments
    corpus.sqlite           the merged state sigma_p, rebuildable from the log
    locks/<source>.lock     one sync per source at a time

The merge keeps, for every item ID, the record with the largest order key
``(rev_time, rev, run_id, tie)``. Taking a maximum under a total order is commutative, associative and
idempotent, so replaying the log in any order, or twice, gives the same state (property P3).
Deletions enter the log only as tombstones: sent by a connector, or derived from a scoped listing that
ended with ``complete: true``.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from evo_agents.kg.ids import normalize_time, rev_key, utc_now
from evo_agents.kg.protocol import canonical_json, sha256, source_of

try:  # POSIX advisory locks; Windows runs without them for now.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    deleted INTEGER NOT NULL,
    k_time TEXT NOT NULL,
    k_rev TEXT NOT NULL,
    k_run TEXT NOT NULL,
    k_tie TEXT NOT NULL,
    record TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS items_source ON items(source, deleted);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    connector TEXT,
    version TEXT,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    detail TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS cursors (
    source TEXT PRIMARY KEY,
    cursor TEXT,
    run_id TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS held (
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    reason TEXT NOT NULL,
    ids TEXT NOT NULL,
    PRIMARY KEY (run_id, scope)
) STRICT;
"""


def kg_home() -> Path:
    return Path(os.environ.get("EVO_KG_HOME") or "~/.evo/kg").expanduser()


def order_key(record: dict, run_id: str) -> tuple[str, str, str, str]:
    return (
        normalize_time(record.get("rev_time")),
        rev_key(record.get("rev")),
        run_id,
        sha256(canonical_json(record)),
    )


@dataclass
class ItemRecord:
    """One item as the merged state holds it."""

    item_id: str
    source: str
    deleted: bool
    run_id: str
    record: dict

    @property
    def rev(self) -> str:
        return self.record.get("rev", "")

    @property
    def hash(self) -> str:
        return self.record.get("hash", "")


class Corpus:
    def __init__(self, project: str, home: Path | None = None):
        self.project = project
        self.root = (home or kg_home()) / project
        self.log_dir = self.root / "log"
        self.blob_dir = self.root / "blobs" / "sha256"
        self.lock_dir = self.root / "locks"
        for path in (self.root, self.log_dir, self.blob_dir, self.lock_dir):
            path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        self.db_path = self.root / "corpus.sqlite"
        self.db = self._open(self.db_path)

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        return db

    def close(self) -> None:
        self.db.close()

    # -- blobs ---------------------------------------------------------------------------------

    def blob_path(self, ref: str) -> Path:
        digest = ref.split(":", 1)[1]
        return self.blob_dir / digest[:2] / digest[2:]

    def put_blob(self, text: str) -> str:
        data = text.encode("utf-8")
        ref = sha256(data)
        path = self.blob_path(ref)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return ref

    def get_blob(self, ref: str) -> str:
        return self.blob_path(ref).read_text(encoding="utf-8")

    def to_log_record(self, message: dict) -> dict:
        """Move item text into blobs; the log keeps references only."""
        if message.get("type") != "item":
            return dict(message)
        record = {k: v for k, v in message.items() if k not in ("body", "fragments")}
        if "body" in message:
            body = {k: v for k, v in message["body"].items() if k != "text"}
            body["blob"] = self.put_blob(message["body"]["text"])
            record["body"] = body
        if "fragments" in message:
            frags = []
            for f in message["fragments"]:
                frag = {k: v for k, v in f.items() if k != "text"}
                frag["blob"] = self.put_blob(f["text"])
                frags.append(frag)
            record["fragments"] = frags
        return record

    def body_text(self, record: dict) -> str | None:
        body = record.get("body")
        return self.get_blob(body["blob"]) if body and "blob" in body else None

    def fragments(self, record: dict) -> list[dict]:
        return [{**f, "text": self.get_blob(f["blob"])} for f in record.get("fragments", [])]

    # -- log -----------------------------------------------------------------------------------

    def log_files(self) -> list[Path]:
        return sorted([*self.log_dir.glob("*.jsonl.gz"), *self.log_dir.glob("*.jsonl")])

    @staticmethod
    def read_log(path: Path) -> Iterator[dict]:
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        return  # a crash can leave half a line at the end; everything before it counts
        except (EOFError, gzip.BadGzipFile):
            return

    def run_records(self, path: Path) -> tuple[str | None, list[dict]]:
        records = list(self.read_log(path))
        header = records[0] if records and records[0].get("type") == "run" else None
        return (header["run_id"] if header else None), records

    # -- merge ---------------------------------------------------------------------------------

    def merge(self, records: Iterable[dict], run_id: str, db: sqlite3.Connection | None = None) -> int:
        """Merge item and tombstone records of one run into the state. Returns how many changed."""
        db = db or self.db
        changed = 0
        for record in records:
            kind = record.get("type")
            if kind not in ("item", "tombstone"):
                continue
            key = order_key(record, run_id)
            row = db.execute(
                "SELECT k_time, k_rev, k_run, k_tie FROM items WHERE item_id = ?", (record["id"],)
            ).fetchone()
            if row is not None and tuple(row) >= key:
                continue
            db.execute(
                "INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    source_of(record["id"]),
                    1 if kind == "tombstone" else 0,
                    *key,
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                ),
            )
            changed += 1
        return changed

    def get(self, item_id: str) -> ItemRecord | None:
        row = self.db.execute(
            "SELECT item_id, source, deleted, k_run, record FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row) -> ItemRecord:
        return ItemRecord(row[0], row[1], bool(row[2]), row[3], json.loads(row[4]))

    def items(self, source: str | None = None, *, include_deleted: bool = False) -> list[ItemRecord]:
        query = "SELECT item_id, source, deleted, k_run, record FROM items"
        clauses, params = [], []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if not include_deleted:
            clauses.append("deleted = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return [self._row(r) for r in self.db.execute(query + " ORDER BY item_id", params)]

    def alive_in_scope(self, scope: str) -> list[ItemRecord]:
        source = source_of(scope)
        return [r for r in self.items(source) if r.item_id.startswith(scope)]

    def state_digest(self, db: sqlite3.Connection | None = None) -> str:
        """Hash of sigma_p: every item ID with its winning record and deletion flag."""
        db = db or self.db
        rows = db.execute("SELECT item_id, deleted, record FROM items ORDER BY item_id").fetchall()
        return sha256(canonical_json([[r[0], r[1], json.loads(r[2])] for r in rows]))

    def rebuild_state(self, files: list[Path] | None = None) -> sqlite3.Connection:
        """Recompute sigma_p from the log alone, into an in-memory database."""
        db = sqlite3.connect(":memory:")
        db.executescript(SCHEMA)
        for path in files if files is not None else self.log_files():
            run_id, records = self.run_records(path)
            if run_id:
                self.merge(records, run_id, db)
        db.commit()
        return db

    # -- cursors, runs, held removals ----------------------------------------------------------

    def cursor(self, source: str):
        row = self.db.execute("SELECT cursor FROM cursors WHERE source = ?", (source,)).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    def runs(self, source: str | None = None, limit: int = 20) -> list[dict]:
        query = "SELECT run_id, source, connector, version, status, started_at, finished_at, detail FROM runs"
        params: list = []
        if source:
            query += " WHERE source = ?"
            params.append(source)
        query += " ORDER BY run_id DESC LIMIT ?"
        params.append(limit)
        keys = ("run_id", "source", "connector", "version", "status", "started_at", "finished_at")
        out = []
        for row in self.db.execute(query, params):
            entry = dict(zip(keys, row[:7], strict=True))
            entry["detail"] = json.loads(row[7])
            out.append(entry)
        return out

    def held(self, source: str | None = None) -> list[dict]:
        query = "SELECT run_id, source, scope, reason, ids FROM held"
        params: list = []
        if source:
            query += " WHERE source = ?"
            params.append(source)
        return [
            {"run_id": r[0], "source": r[1], "scope": r[2], "reason": r[3], "ids": json.loads(r[4])}
            for r in self.db.execute(query + " ORDER BY run_id", params)
        ]

    @contextlib.contextmanager
    def lock(self, source: str):
        path = self.lock_dir / f"{source}.lock"
        with open(path, "w") as fh:
            if fcntl is not None:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(f"another sync of source {source!r} is running") from exc
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)


class LogWriter:
    """Writes one run's log. Lines go to ``<run_id>.jsonl`` as they arrive and are flushed, so a crash
    keeps every message written before it; ``close`` compresses the file to ``.jsonl.gz``."""

    def __init__(self, corpus: Corpus, run_id: str, header: dict):
        self.corpus = corpus
        self.run_id = run_id
        self.path = corpus.log_dir / f"{run_id}.jsonl"
        self.fh = open(self.path, "a", encoding="utf-8")
        self.records: list[dict] = []
        self.write({"type": "run", "run_id": run_id, "started_at": utc_now(), **header})

    def write(self, record: dict) -> None:
        self.fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self.fh.flush()
        self.records.append(record)

    def sync(self) -> None:
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self) -> Path:
        self.sync()
        self.fh.close()
        target = self.path.with_suffix(".jsonl.gz")
        tmp = target.with_suffix(".gz.tmp")
        with open(self.path, "rb") as src, gzip.open(tmp, "wb") as dst:
            dst.write(src.read())
        os.replace(tmp, target)
        self.path.unlink()
        return target
