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

A memo is a directory, one gzipped file an entry: ``<stage>/<first two hex digits of the key>/<the rest>``, the
output and its hash. An entry is written to a temporary file and moved into place, so another build sharing the memo
reads a whole entry or none, and a build that crashes keeps every entry it wrote. Two builds that write one key write
the same output, a task being a function of its key. ``Memo(None)`` keeps its entries in memory.

A memo opened where a ``memo.sqlite`` of an earlier version sits beside it (``<path>.sqlite``) first takes its rows
into entry files, once (``IMPORTED`` marks it done): those rows hold answers a model was paid for, by a key the hub
holds, that no blob gives again (``evo_agents.sqlite_legacy``).
"""

from __future__ import annotations

import gzip
import json
import os
import threading
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from evo_agents.kg.protocol import canonical_json, sha256

IMPORTED = ".imported-sqlite"  # the marker of a memo that took the rows of the memo.sqlite beside it


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


def _stage_dir(root: Path, stage: str) -> Path:
    return root / stage.replace(":", "+")  # ``link:reads``: no colon in a file name on Windows


def _entry_path(root: Path, stage: str, key: str) -> Path:
    digest = key.split(":", 1)[1]
    return _stage_dir(root, stage) / digest[:2] / digest[2:]


def _read(path: Path) -> tuple[object, str] | None:
    try:
        entry = json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, EOFError, ValueError):  # none yet, or half of one a crash left: computed again
        return None
    return entry["output"], entry["output_hash"]


class Memo:
    def __init__(self, path: Path | None):
        self.root = path
        if path is not None:
            path.mkdir(parents=True, exist_ok=True)
            _import_sqlite(path)
        self.held: dict[tuple[str, str], tuple[object, str]] = {}  # the entries of a memo kept in memory
        self.hits: Counter[str] = Counter()
        self.misses: Counter[str] = Counter()

    def _get(self, stage: str, key: str) -> tuple[object, str] | None:
        if self.root is None:
            found = self.held.get((stage, key))
            return (json.loads(canonical_json(found[0])), found[1]) if found else None
        return _read(_entry_path(self.root, stage, key))

    def run(self, stage: str, code_version: str, inputs: dict[str, str], fn, *, commit: bool = False):
        """Return ``(output, output_hash)``. Downstream keys take the output hash, so a recomputed task
        whose output did not change does not invalidate anything after it (early cutoff). ``commit`` is kept for the
        callers that ask for an entry on disk at once (an answer a model was paid for): every entry is."""
        key = memo_key(stage, code_version, inputs)
        found = self._get(stage, key)
        if found is not None:
            self.hits[stage] += 1
            return found
        self.misses[stage] += 1
        output = fn()
        return output, self._put(key, stage, output)

    def run_traced(self, stage: str, code_version: str, inputs: dict[str, str], fn, held):
        """Return ``(output, output_hash)`` of a task that reads a shared table (see the module). ``fn()`` returns
        the output and the sorted list of the keys it read; ``held(keys)`` is the digest of what those keys hold
        in the table now."""
        reads_stage = f"{stage}:reads"
        reads_key = memo_key(reads_stage, code_version, inputs)
        read = self._get(reads_stage, reads_key)
        if read is not None:
            key = memo_key(stage, code_version, {**inputs, "reads": held(read[0])})
            found = self._get(stage, key)
            if found is not None:
                self.hits[stage] += 1
                return found
        self.misses[stage] += 1
        output, keys = fn()
        self._put(reads_key, reads_stage, keys)
        key = memo_key(stage, code_version, {**inputs, "reads": held(keys)})
        return output, self._put(key, stage, output)

    def _put(self, key: str, stage: str, output) -> str:
        data = canonical_json(output)
        digest = sha256(data)
        if self.root is None:
            self.held[(stage, key)] = (json.loads(data), digest)
            return digest
        path = _entry_path(self.root, stage, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({"output": json.loads(data), "output_hash": digest}, ensure_ascii=False, sort_keys=True)
        _write_entry(path, entry.encode("utf-8"))
        return digest

    def keys(self, stage: str) -> set[str]:
        """The keys of the entries of ``stage``."""
        if self.root is None:
            return {key for (held_stage, key) in self.held if held_stage == stage}
        top = _stage_dir(self.root, stage)
        if not top.is_dir():
            return set()
        return {
            f"sha256:{part.name}{entry.name}"
            for part in top.iterdir()
            if part.is_dir()
            for entry in part.iterdir()
            if ".tmp" not in entry.name
        }

    def commit(self) -> None:
        """Nothing to do: every entry is on disk once ``run`` returns it."""

    def close(self) -> None:
        """Nothing to do: a memo holds no file open."""

    def stats(self) -> dict:
        stages = sorted(set(self.hits) | set(self.misses))
        return {s: {"hits": self.hits[s], "misses": self.misses[s]} for s in stages}


def _write_entry(path: Path, entry: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}-{threading.get_ident()}")
    tmp.write_bytes(gzip.compress(entry, compresslevel=5))
    os.replace(tmp, path)


def _import_sqlite(root: Path) -> None:
    """Take the rows of the memo.sqlite beside ``root`` into its entry files, unless done already (see the module)."""
    legacy = root.with_name(f"{root.name}.sqlite")
    marker = root / IMPORTED
    if marker.exists() or not legacy.is_file():
        return
    from evo_agents.sqlite_legacy import errors, memo_rows

    try:
        for key, stage, output, output_hash in memo_rows(legacy):
            path = _entry_path(root, stage, key)
            if path.exists():
                continue
            try:
                entry = {"output": json.loads(gzip.decompress(output)), "output_hash": output_hash}
            except (OSError, EOFError, ValueError):  # a row a crash cut: computed again when a build needs it
                continue
            _write_entry(path, json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    except errors():  # not read whole: no marker, so the next open tries again; the rows taken stay
        return
    marker.write_text(f"{legacy.name}\n", encoding="utf-8")


def usages(path: Path, stage: str) -> Iterator[dict]:
    """The ``usage`` (tokens, cost) of each answer of ``stage`` in the memo at ``path``, ``{}`` for an answer without
    one. Reads only."""
    top = _stage_dir(path, stage)
    for entry in sorted(top.glob("*/*")) if top.is_dir() else ():
        found = None if ".tmp" in entry.name else _read(entry)
        if found is not None:
            yield (found[0] or {}).get("usage") or {}  # type: ignore[union-attr]
