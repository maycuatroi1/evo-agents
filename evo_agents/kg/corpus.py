"""Corpus of one project: the append-only log, content-addressed blobs, and the merged state.

Layout under ``$EVO_KG_HOME/<project>/`` (default ``~/.evo/kg``), mode 0700::

    log/<run_id>.jsonl.gz   every message of one connector run, item text replaced by blob refs
    blobs/sha256/ab/cd...   item bodies and fragments
    state.json              the merged state sigma_p, rebuildable from the log, with the runs, cursors, lineages and
                            held removals it records; written whole under locks/.state.lock
    locks/<source>.lock     one sync per source at a time
    audit.jsonl             one line per sync and per build, counts and ids only (audit.py), mode 0600

The merge keeps, for every item ID, the record with the largest order key
``(epoch, rev_time, rev, run_id, tie)``. Taking a maximum under a total order is commutative, associative and
idempotent, so replaying the log in any order, or twice, gives the same state (property P3).
Deletions enter the log only as tombstones: sent by a connector, or derived from a scoped listing that
ended with ``complete: true``.

A revision time only orders records read from one place: the commit times of two git refs say nothing about
which one the source reads now. Each run's header therefore names its source's lineage, a digest of where the
source reads from (``evo_agents.kg.sync.source_lineage``), and its epoch: BASE_EPOCH for the first lineage the
corpus records for a source, the epoch of the source's previous run when the lineage is the same, and the run's
own ID when it changed. Every record of a run takes the run's epoch, so a run that reads from somewhere new
supersedes whatever earlier lineages left, and within one lineage the revision order decides as before. Logs
written before epochs existed have none (``""``) and lose to any run that has one.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from evo_agents import filelock
from evo_agents.kg.ids import normalize_time, rev_key, utc_now
from evo_agents.kg.protocol import canonical_json, sha256, source_of

STATE_FILE = "state.json"  # the merged state and what the corpus records of its runs
# The lock of the state file, beside that of each source (``<source>.lock``): a dot no source id starts with, as the
# bench's source is named ``corpus``.
STATE_LOCK = ".state.lock"
# The epoch of a source's first lineage: below every run ID, above the empty epoch of logs written before epochs.
BASE_EPOCH = "00000000-0000-0000-0000-000000000000"
EPOCH = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def run_epoch(header: dict) -> str:
    """The epoch a run's header names; ``""`` for a log written before epochs, or when it is not a UUID."""
    epoch = header.get("epoch")
    return epoch if isinstance(epoch, str) and EPOCH.fullmatch(epoch) else ""


def next_epoch(recorded: tuple[str, str] | None, lineage: str, run_id: str) -> str:
    """The epoch of run ``run_id`` of a source, which reads from ``lineage``, when ``recorded`` is the lineage and the
    epoch recorded for the source: BASE_EPOCH when none is recorded yet, the recorded epoch when the lineage is the
    recorded one, else ``run_id`` itself, which starts a new epoch."""
    if recorded is None:
        return BASE_EPOCH
    return recorded[1] if recorded[0] == lineage else run_id


def kg_home() -> Path:
    return Path(os.environ.get("EVO_KG_HOME") or "~/.evo/kg").expanduser()


def order_key(record: dict, run_id: str, epoch: str = "") -> tuple[str, str, str, str, str]:
    return (
        epoch,
        normalize_time(record.get("rev_time")),
        rev_key(record.get("rev")),
        run_id,
        sha256(canonical_json(record)),
    )


def blob_ref(text: str) -> str:
    """The reference of a blob holding ``text``, which ``Corpus.put_blob`` would store it under."""
    return sha256(text.encode("utf-8"))


def to_log_record(message: dict, put=blob_ref) -> dict:
    """``message`` as a log keeps it: the text of an item's body and fragments replaced by the reference ``put(text)``
    gives, which stores it or, by default, only names it."""
    if message.get("type") != "item":
        return dict(message)
    record = {k: v for k, v in message.items() if k not in ("body", "fragments")}
    if "body" in message:
        body = {k: v for k, v in message["body"].items() if k != "text"}
        body["blob"] = put(message["body"]["text"])
        record["body"] = body
    if "fragments" in message:
        frags = []
        for f in message["fragments"]:
            frag = {k: v for k, v in f.items() if k != "text"}
            frag["blob"] = put(f["text"])
            frags.append(frag)
        record["fragments"] = frags
    return record


class MergedState:
    """The merged state sigma_p in memory, without SQLite: for every item ID the record with the largest order key,
    merged as ``Corpus.merge`` merges, so ``digest`` equals the ``Corpus.state_digest`` of a corpus that merged the
    same runs."""

    def __init__(self):
        self.rows: dict[str, tuple[tuple[str, str, str, str, str], int, dict]] = {}

    def merge(self, records: Iterable[dict], run_id: str) -> int:
        """Merge item and tombstone records of one run, each with the epoch the run's header names; how many changed."""
        changed = 0
        epoch = ""
        for record in records:
            kind = record.get("type")
            if kind == "run":
                epoch = run_epoch(record)
            if kind not in ("item", "tombstone"):
                continue
            key = order_key(record, run_id, epoch)
            known = self.rows.get(record["id"])
            if known is not None and known[0] >= key:
                continue
            # the record as Corpus.merge stores it and reads it back: through JSON
            stored = json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True))
            self.rows[record["id"]] = (key, 1 if kind == "tombstone" else 0, stored)
            changed += 1
        return changed

    def digest(self) -> str:
        """``Corpus.state_digest`` of this state."""
        return sha256(canonical_json([[item_id, row[1], row[2]] for item_id, row in sorted(self.rows.items())]))


def replayed_digest(runs: Iterable[tuple[str, Iterable[dict]]]) -> str:
    """The state digest of a state that starts empty and merges ``runs`` (run id, records) in their order."""
    state = MergedState()
    for run_id, records in runs:
        state.merge(records, run_id)
    return state.digest()


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
        self.state_path = self.root / STATE_FILE
        self.state = _empty_state()
        self.loaded: tuple[int, int, int] | None = None  # (inode, mtime_ns, size) of the state file read last
        self.depth = 0  # transactions open (``transaction``)
        self.read_digest: str | None = None  # the state digest of the items ``read_state`` read last
        self._load()

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            st = self.state_path.stat()
        except FileNotFoundError:
            return None
        return st.st_ino, st.st_mtime_ns, st.st_size  # each write is a new file: its inode tells it from the last

    def _load(self, *, always: bool = False) -> None:
        """Read the state file when another process wrote it since it was read here, or ``always``: a transaction
        reads it under the lock whatever the stat says, as two writes within a clock tick may leave it the same."""
        seen = self._stat()
        if seen == self.loaded and not always:
            return
        state = _empty_state()
        if seen is not None:
            with open(self.state_path, encoding="utf-8") as fh:
                state.update(json.load(fh))
        self.state, self.loaded = state, seen

    def _fresh(self) -> dict:
        if not self.depth:
            self._load()
        return self.state

    @contextlib.contextmanager
    def transaction(self):
        """Hold the corpus's write lock, read what other processes wrote, and write the state file at the end of the
        outermost one; what was changed in one that raises is read back from the file instead."""
        if self.depth:
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
            return
        fh = open(self.lock_dir / STATE_LOCK, "a", encoding="utf-8")
        filelock.lock(fh.fileno())
        try:
            self._load(always=True)
            self.depth = 1
            try:
                yield
            except BaseException:
                self.state, self.loaded = _empty_state(), None  # what this transaction changed is dropped
                raise
            finally:
                self.depth = 0
            self._write()
        finally:
            self._load()
            filelock.unlock(fh.fileno())
            fh.close()

    def _write(self) -> None:
        tmp = self.state_path.with_name(f".{STATE_FILE}.tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, separators=(",", ":"))
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)
        self.loaded = self._stat()

    def close(self) -> None:
        """Nothing to do: a corpus holds no file open between its transactions."""

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
        return to_log_record(message, self.put_blob)

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

    def merge(self, records: Iterable[dict], run_id: str) -> int:
        """Merge item and tombstone records of one run into the state, each with the epoch the run's header names.
        Returns how many changed."""
        with self.transaction():
            items = self.state["items"]
            changed = 0
            epoch = ""
            for record in records:
                kind = record.get("type")
                if kind == "run":
                    epoch = run_epoch(record)
                if kind not in ("item", "tombstone"):
                    continue
                key = order_key(record, run_id, epoch)
                known = items.get(record["id"])
                if known is not None and tuple(known[0]) >= key:
                    continue
                stored = json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True))
                items[record["id"]] = [list(key), 1 if kind == "tombstone" else 0, stored]
                changed += 1
            return changed

    def get(self, item_id: str) -> ItemRecord | None:
        row = self._fresh()["items"].get(item_id)
        return _item(item_id, row) if row else None

    def items(self, source: str | None = None, *, include_deleted: bool = False) -> list[ItemRecord]:
        return [
            _item(item_id, row)
            for item_id, row in sorted(self._fresh()["items"].items())
            if (include_deleted or not row[1]) and (source is None or source_of(item_id) == source)
        ]

    def read_state(self) -> list[ItemRecord]:
        """The live items, as ``items`` gives them, read at once with the state digest of what it read: a build
        records ``read_digest``, the state it was made of, not the state a sync left after it."""
        rows = sorted(self._fresh()["items"].items())
        self.read_digest = _digest(rows)
        return [_item(item_id, row) for item_id, row in rows if not row[1]]

    def sources(self) -> list[str]:
        """The sources that hold at least one live item, by id."""
        return sorted({source_of(item_id) for item_id, row in self._fresh()["items"].items() if not row[1]})

    def alive_in_scope(self, scope: str) -> list[ItemRecord]:
        source = source_of(scope)
        return [r for r in self.items(source) if r.item_id.startswith(scope)]

    def state_digest(self, state: MergedState | None = None) -> str:
        """Hash of sigma_p: every item ID with its winning record and deletion flag; of ``state`` when given."""
        if state is not None:
            return state.digest()
        return _digest(sorted(self._fresh()["items"].items()))

    def alive_digest(self, source: str) -> str:
        """Hash of what one source holds alive: each live item ID of ``source`` with its winning record. Two corpora
        whose merges left the same items alive with the same records give the same hash, whichever runs got there."""
        rows = sorted(self._fresh()["items"].items())
        return sha256(canonical_json([[i, row[2]] for i, row in rows if not row[1] and source_of(i) == source]))

    def rebuild_state(self, files: list[Path] | None = None) -> MergedState:
        """Recompute sigma_p from the log alone, in memory."""
        state = MergedState()
        for path in files if files is not None else self.log_files():
            run_id, records = self.run_records(path)
            if run_id:
                state.merge(records, run_id)
        return state

    # -- lineages ------------------------------------------------------------------------------

    def _lineage(self, source: str) -> tuple[str, str] | None:
        row = self._fresh()["lineages"].get(source)
        return (row[0], row[1]) if row else None

    def epoch_for(self, source: str, lineage: str, run_id: str) -> str:
        """The epoch of run ``run_id`` of ``source``, which reads from ``lineage`` (``next_epoch``)."""
        return next_epoch(self._lineage(source), lineage, run_id)

    def current_epoch(self, source: str) -> str:
        """The epoch of the last run of ``source`` that recorded its lineage, ``""`` when none did."""
        recorded = self._lineage(source)
        return recorded[1] if recorded else ""

    def record_lineage(self, source: str, lineage: str, epoch: str, run_id: str) -> None:
        """Remember what run ``run_id`` of ``source`` read from, in the caller's transaction."""
        with self.transaction():
            self.state["lineages"][source] = [lineage, epoch, run_id]

    def adopt_lineage(self, source: str, lineage: str, epoch: str, run_id: str) -> None:
        """Take what run ``run_id`` of ``source``, a run another machine wrote and this corpus merged, read from and its
        epoch, when that epoch, then that run id, is above the recorded one's; in the caller's transaction. The next run
        here that reads from the same place then takes that epoch, so its records compete with the other machine's
        within one epoch instead of losing to them, and one that reads from elsewhere starts a newer epoch."""
        with self.transaction():
            known = self.state["lineages"].get(source)
            if known is None or (epoch, run_id) > (known[1], known[2]):
                self.state["lineages"][source] = [lineage, epoch, run_id]

    def lineage_of(self, source: str) -> str | None:
        """The lineage the corpus records for ``source``, None when it records none."""
        recorded = self._lineage(source)
        return recorded[0] if recorded else None

    # -- cursors, runs, held removals ----------------------------------------------------------

    def cursor(self, source: str):
        row = self._fresh()["cursors"].get(source)
        return json.loads(json.dumps(row[0])) if row else None

    def record_cursor(self, source: str, cursor, run_id: str) -> None:
        """Keep the cursor run ``run_id`` of ``source`` ended with, unless the source has one of a later run: runs
        merged in any order leave the newest one's. In the caller's transaction."""
        with self.transaction():
            known = self.state["cursors"].get(source)
            if known is None or run_id > known[1]:
                self.state["cursors"][source] = [cursor, run_id]

    def forget_cursor(self, source: str) -> None:
        """Drop the cursor of ``source``: its next run reads everything again."""
        with self.transaction():
            self.state["cursors"].pop(source, None)

    def set_cursor(self, source: str, cursor, run_id: str) -> None:
        """Make ``cursor`` the cursor of ``source``, whatever run kept the one before: for a source read in the
        builder's own process (``evo_agents.kg.replace.keep_cursor``). In the caller's transaction."""
        with self.transaction():
            self.state["cursors"][source] = [cursor, run_id]

    def runs(self, source: str | None = None, limit: int = 20, status: str | None = None) -> list[dict]:
        """Recorded runs, newest first, optionally of one source and with one status (ok or failed)."""
        found = [
            {"run_id": run_id, **row}
            for run_id, row in sorted(self._fresh()["runs"].items(), reverse=True)
            if (not source or row["source"] == source) and (not status or row["status"] == status)
        ]
        return json.loads(json.dumps(found[:limit]))

    def hub_run_ids(self) -> set[str]:
        """The ids of the runs merged from the hub: those whose detail names a hub."""
        return {run_id for run_id, row in self._fresh()["runs"].items() if row["detail"].get("hub")}

    def run_ids(self) -> set[str]:
        """The ids of the runs recorded."""
        return set(self._fresh()["runs"])

    def record_run(
        self,
        run_id: str,
        *,
        source: str,
        connector: str | None,
        status: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        version: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Record run ``run_id``, replacing what was recorded of it, in the caller's transaction."""
        with self.transaction():
            self.state["runs"][run_id] = {
                "source": source,
                "connector": connector,
                "version": version,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "detail": json.loads(json.dumps(detail or {}, ensure_ascii=False)),
            }

    def record_on_hub(self, run_ids: Iterable[str], hub: str) -> None:
        """Remember that the hub at ``hub`` holds the runs ``run_ids`` (``hub kg push`` saw it say so)."""
        now = utc_now()
        with self.transaction():
            for run_id in run_ids:
                self.state["hub_runs"].setdefault(run_id, [hub, now])

    def runs_on_hub(self) -> set[str]:
        """The runs ``record_on_hub`` recorded."""
        return set(self._fresh()["hub_runs"])

    def hold(self, run_id: str, source: str, scope: str, reason: str, ids: list[str]) -> None:
        """Keep the removals the deletion guard held back in run ``run_id`` for ``scope``, in the caller's
        transaction."""
        with self.transaction():
            held = [h for h in self.state["held"] if (h["run_id"], h["scope"]) != (run_id, scope)]
            held.append({"run_id": run_id, "source": source, "scope": scope, "reason": reason, "ids": list(ids)})
            self.state["held"] = held

    def drop_held(self, source: str) -> None:
        """Forget the removals held back for ``source``, in the caller's transaction."""
        with self.transaction():
            self.state["held"] = [h for h in self.state["held"] if h["source"] != source]

    def held(self, source: str | None = None) -> list[dict]:
        found = [h for h in self._fresh()["held"] if not source or h["source"] == source]
        return json.loads(json.dumps(sorted(found, key=lambda h: (h["run_id"], h["scope"]))))

    @contextlib.contextmanager
    def lock(self, source: str):
        path = self.lock_dir / f"{source}.lock"
        with open(path, "a", encoding="utf-8") as fh:
            if not filelock.try_lock(fh.fileno()):
                raise RuntimeError(f"another sync of source {source!r} is running")
            try:
                yield
            finally:
                filelock.unlock(fh.fileno())


def _empty_state() -> dict:
    return {"version": 1, "items": {}, "runs": {}, "cursors": {}, "held": [], "lineages": {}, "hub_runs": {}}


def _item(item_id: str, row: list) -> ItemRecord:
    return ItemRecord(item_id, source_of(item_id), bool(row[1]), row[0][3], json.loads(json.dumps(row[2])))


def _digest(rows: list[tuple[str, list]]) -> str:
    return sha256(canonical_json([[item_id, row[1], row[2]] for item_id, row in rows]))


class LogHome(Protocol):
    """Where a run's log goes: a corpus, or the spool of a sync against the hub (``evo_agents.hub.kg_hub_sync``)."""

    log_dir: Path


class LogWriter:
    """Writes one run's log. Lines go to ``<run_id>.jsonl`` as they arrive and are flushed, so a crash
    keeps every message written before it; ``close`` compresses the file to ``.jsonl.gz``."""

    def __init__(self, corpus: LogHome, run_id: str, header: dict):
        self.corpus = corpus
        self.run_id = run_id
        self.path = corpus.log_dir / f"{run_id}.jsonl"
        self.fh = open(self.path, "a", encoding="utf-8")
        self.records: list[dict] = []
        self.header = {"type": "run", "run_id": run_id, "started_at": utc_now(), **header}
        self.write(self.header)

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
