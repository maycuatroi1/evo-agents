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
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections import Counter
from pathlib import Path

from evo_agents.kg.protocol import canonical_json, sha256

SCHEMA = """
CREATE TABLE IF NOT EXISTS memo (
    key TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    output BLOB NOT NULL,
    output_hash TEXT NOT NULL
) STRICT;
"""


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

    def run(self, stage: str, code_version: str, inputs: dict[str, str], fn):
        """Return ``(output, output_hash)``. Downstream keys take the output hash, so a recomputed task
        whose output did not change does not invalidate anything after it (early cutoff)."""
        key = memo_key(stage, code_version, inputs)
        row = self.db.execute("SELECT output, output_hash FROM memo WHERE key = ?", (key,)).fetchone()
        if row is not None:
            self.hits[stage] += 1
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
        return digest

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def stats(self) -> dict:
        stages = sorted(set(self.hits) | set(self.misses))
        return {s: {"hits": self.hits[s], "misses": self.misses[s]} for s in stages}
