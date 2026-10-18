"""Memoized tasks (definition D9).

A memo key encodes the stage, the code version and every (input name, input version) pair, each part
length-prefixed. Concatenating values without names or lengths lets two compensating changes collide:
Dagster hit exactly that (issue #34184). A task without a code version is refused, never keyed by run.

A traced task (``run_traced``) also reads entries of a table shared by every task of its stage, such as the
identifier index of the link stage. Keying it by a digest of the whole table would recompute every task whenever
one entry changes. It is keyed instead by the entries it read last time, as build systems with recorded
dependencies do: the memo keeps, under the task's inputs, the keys it read, and its output under its inputs plus
a digest of what those keys held. The next run looks the keys up again; the output counts only while every one of
them holds what it held. A task is a function of its inputs and of what it reads, so the same values make it read
the same keys and return the same output.

Every row a memo returns or writes counts as used: ``commit`` records, in ``memo_used``, the time the memo was opened
as the row's last use, and in ``memo_meta`` that time as ``last_build``. Retention (``evo_agents.kg.retention``)
deletes the rows no key uses any more: those whose last use is older than its period and than ``last_build``
(``unused``). Rows written before uses were recorded have none, and go once a build has recorded its own. Both tables
sit beside ``memo``, which keeps its four columns, so a memo stays readable and writable by a version that does not
know them.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections import Counter
from pathlib import Path

from evo_agents.kg.ids import utc_now
from evo_agents.kg.protocol import canonical_json, sha256

SCHEMA = """
CREATE TABLE IF NOT EXISTS memo (
    key TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    output BLOB NOT NULL,
    output_hash TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS memo_used (key TEXT PRIMARY KEY, used TEXT NOT NULL) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS memo_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
"""
# The rows no key uses: no use recorded since ``cut``, the earlier of the retention cutoff and ``last_build``.
UNUSED = "SELECT memo.key FROM memo LEFT JOIN memo_used USING (key) WHERE coalesce(memo_used.used, '') < :cut"


def _enc(part) -> bytes:
    data = part if isinstance(part, bytes) else str(part).encode("utf-8")
    return len(data).to_bytes(8, "big") + data


def memo_key(stage: str, code_version: str, inputs: dict[str, str]) -> str:
    if not code_version:
        raise ValueError(f"task {stage!r} has no code_version; refusing to memoize it")
    parts = [_enc("stage"), _enc(stage), _enc("code_version"), _enc(code_version), _enc(len(inputs))]
    for name, version in sorted(inputs.items()):
        parts.append(_enc(name))
        parts.append(_enc(version))
    return sha256(b"".join(parts))


class Memo:
    def __init__(self, path: Path | None):
        self.db = sqlite3.connect(path if path else ":memory:")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.hits: Counter[str] = Counter()
        self.misses: Counter[str] = Counter()
        self.opened = utc_now()
        self.used: set[str] = set()  # keys returned or written since the last commit

    def run(self, stage: str, code_version: str, inputs: dict[str, str], fn):
        """Return ``(output, output_hash)``. Downstream keys take the output hash, so a recomputed task
        whose output did not change does not invalidate anything after it (early cutoff)."""
        key = memo_key(stage, code_version, inputs)
        row = self.db.execute("SELECT output, output_hash FROM memo WHERE key = ?", (key,)).fetchone()
        if row is not None:
            self.hits[stage] += 1
            self.used.add(key)
            return json.loads(gzip.decompress(row[0])), row[1]
        self.misses[stage] += 1
        output = fn()
        return output, self._put(key, stage, output)

    def run_traced(self, stage: str, code_version: str, inputs: dict[str, str], fn, held):
        """Return ``(output, output_hash)`` of a task that reads a shared table (see the module). ``fn()`` returns
        the output and the sorted list of the keys it read; ``held(keys)`` is the digest of what those keys hold
        in the table now."""
        reads_key = memo_key(f"{stage}:reads", code_version, inputs)
        row = self.db.execute("SELECT output FROM memo WHERE key = ?", (reads_key,)).fetchone()
        if row is not None:
            keys = json.loads(gzip.decompress(row[0]))
            key = memo_key(stage, code_version, {**inputs, "reads": held(keys)})
            found = self.db.execute("SELECT output, output_hash FROM memo WHERE key = ?", (key,)).fetchone()
            if found is not None:
                self.hits[stage] += 1
                self.used.update((reads_key, key))
                return json.loads(gzip.decompress(found[0])), found[1]
        self.misses[stage] += 1
        output, keys = fn()
        self._put(reads_key, f"{stage}:reads", keys)
        key = memo_key(stage, code_version, {**inputs, "reads": held(keys)})
        return output, self._put(key, stage, output)

    def _put(self, key: str, stage: str, output) -> str:
        data = canonical_json(output)
        digest = sha256(data)
        self.db.execute(
            "INSERT OR REPLACE INTO memo VALUES (?, ?, ?, ?)",
            (key, stage, gzip.compress(data, compresslevel=5), digest),
        )
        self.used.add(key)
        return digest

    def commit(self) -> None:
        """Commit the rows written, with the use of every row returned or written since the last commit."""
        if self.used:
            self.db.executemany(
                "INSERT INTO memo_used VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET used = excluded.used"
                " WHERE excluded.used > memo_used.used",
                [(key, self.opened) for key in sorted(self.used)],
            )
            self.db.execute(
                "INSERT INTO memo_meta VALUES ('last_build', ?) ON CONFLICT (name) DO UPDATE SET value = excluded.value"
                " WHERE excluded.value > memo_meta.value",
                (self.opened,),
            )
            self.used.clear()
        self.db.commit()

    def close(self) -> None:
        self.commit()
        self.db.close()

    def stats(self) -> dict:
        stages = sorted(set(self.hits) | set(self.misses))
        return {s: {"hits": self.hits[s], "misses": self.misses[s]} for s in stages}


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def unused(path: Path, cutoff: str) -> tuple[int, int, str | None]:
    """In the memo at ``path``: how many rows no key uses (no use since ``cutoff`` nor since ``last_build``), how many
    rows it holds, and the time below which a last use counts as none, None when no build recorded a use. Reads only."""
    if not path.exists():
        return 0, 0, None
    db = _read_only(path)
    try:
        total = db.execute("SELECT count(*) FROM memo").fetchone()[0]
        try:
            row = db.execute("SELECT value FROM memo_meta WHERE name = 'last_build'").fetchone()
        except sqlite3.OperationalError:  # a memo written before uses were recorded
            row = None
        if row is None:
            return 0, total, None
        cut = min(cutoff, row[0])
        return db.execute(f"SELECT count(*) FROM ({UNUSED})", {"cut": cut}).fetchone()[0], total, cut
    except sqlite3.OperationalError:  # not a memo
        return 0, 0, None
    finally:
        db.close()


def drop_unused(path: Path, cut: str) -> int:
    """Delete the rows of the memo at ``path`` with no use since ``cut`` (see ``unused``), and the uses recorded of
    rows that are gone; how many rows went."""
    db = sqlite3.connect(path, timeout=30)
    try:
        with db:
            deleted = db.execute(f"DELETE FROM memo WHERE key IN ({UNUSED})", {"cut": cut}).rowcount
            db.execute("DELETE FROM memo_used WHERE key NOT IN (SELECT key FROM memo)")
    finally:
        db.close()
    return deleted
