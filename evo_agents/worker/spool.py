"""A run's events on disk until the hub acknowledges them.

Each event gets the run's next ``seq`` (1 for the first) and is appended to ``spool/<run>.jsonl`` as one JSON line
``{seq, at, kind, body}`` before anything is sent, so a hub that does not answer loses nothing and a daemon that
restarts sends what was left. The hub answers a batch with ``ack_seq``, its highest seq stored with none missing below;
``spool/<run>.ack`` keeps it, the events up to it are dropped from memory, and the file is rewritten without them
once they take more than half of it, or emptied once everything is acknowledged. A batch is sent again from
``ack_seq + 1`` until the hub has it all.

All spools of the daemon share ``SpoolBudget``, 256 MiB of events not yet acknowledged. An event that would pass it
is dropped and counted, never given a seq, so the seq stays without a gap; once there is room again the run gets a
``system`` event saying how many were dropped. A body over 64 KiB of JSON is cut here as the hub would cut it
(``runs.fit_event_body``), so a batch always fits the hub's 1 MiB. Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from evo_agents.hub import runs
from evo_agents.hub.client import write_atomic

MAX_SPOOL_BYTES = 256 * 1024 * 1024
BATCH_BYTES = runs.MAX_BATCH_BYTES - 64 * 1024  # room for the envelope of the batch
FILE_MODE = 0o600
COMPACT_BYTES = 1024 * 1024  # an acknowledged head smaller than this is not worth rewriting the file for


class SpoolBudget:
    """Bytes of events not yet acknowledged, over every spool of the daemon."""

    def __init__(self, limit: int = MAX_SPOOL_BYTES):
        self.limit = limit
        self.used = 0

    def take(self, size: int) -> bool:
        if self.used + size > self.limit:
            return False
        self.used += size
        return True

    def give(self, size: int) -> None:
        self.used = max(0, self.used - size)


@dataclass
class _Entry:
    seq: int
    offset: int
    size: int


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def encode_event(seq: int, kind: str, body: dict, at: datetime) -> bytes:
    """One spool line: the event as the hub takes it, its body cut to 64 KiB of JSON when longer."""
    fitted, _ = runs.fit_event_body(body)
    event = {"seq": seq, "at": _iso(at), "kind": kind, "body": fitted}
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


class Spool:
    """The events of one run not yet acknowledged by the hub."""

    def __init__(self, directory: Path, run_id: int, budget: SpoolBudget):
        self.run_id = int(run_id)
        self.path = Path(directory) / f"{self.run_id}.jsonl"
        self.ack_path = Path(directory) / f"{self.run_id}.ack"
        self.budget = budget
        self.entries: list[_Entry] = []
        self.ack_seq = 0
        self.last_seq = 0
        self.dropped = 0  # events dropped since the last note of it, for want of room
        self.dropped_total = 0
        self._head = 0  # bytes of the file before the first entry not acknowledged
        self._load()

    # Reading what a previous daemon left

    def _load(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(self.ack_path.read_text(encoding="utf-8"))
            self.ack_seq = int(data.get("ack_seq", 0))
            self.last_seq = max(self.ack_seq, int(data.get("last_seq", 0)))
        try:
            handle = open(self.path, "rb")
        except FileNotFoundError:
            return
        with handle:
            offset = 0
            for line in handle:
                size = len(line)
                try:
                    seq = int(json.loads(line)["seq"])
                except (ValueError, KeyError, TypeError):
                    break  # a line cut by a crash ends what can be sent
                if not line.endswith(b"\n"):
                    break
                if seq > self.ack_seq:
                    if self.entries and seq != self.entries[-1].seq + 1:
                        break
                    self.entries.append(_Entry(seq, offset, size))
                    self.budget.take(size)
                else:
                    self._head = offset + size
                self.last_seq = max(self.last_seq, seq)
                offset += size
        if self.entries:
            self.last_seq = self.entries[-1].seq
        self._truncate_after_entries()

    def _truncate_after_entries(self) -> None:
        """Drop whatever follows the last good line, so the next append continues the file cleanly."""
        end = self.entries[-1].offset + self.entries[-1].size if self.entries else self._head
        with contextlib.suppress(OSError):
            if self.path.stat().st_size > end:
                os.truncate(self.path, end)

    # Writing

    @property
    def pending(self) -> int:
        return len(self.entries)

    def append(self, kind: str, body: dict, at: datetime | None = None) -> int | None:
        """Write the event and give it the next seq; None when the budget has no room for it."""
        at = at or datetime.now(timezone.utc)
        if self.dropped:  # say how many were lost as soon as there is room again
            note = {"text": f"{self.dropped} event(s) of this run were dropped: the worker's spool was full"}
            line = encode_event(self.last_seq + 1, "system", {**note, "dropped": self.dropped}, at)
            if not self._write(line):
                self.dropped += 1
                self.dropped_total += 1
                return None
            self.dropped = 0
        line = encode_event(self.last_seq + 1, kind, body, at)
        if not self._write(line):
            self.dropped += 1
            self.dropped_total += 1
            return None
        return self.last_seq

    def _write(self, line: bytes) -> bool:
        if not self.budget.take(len(line)):
            return False
        seq = self.last_seq + 1
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        try:
            offset = os.fstat(fd).st_size
            os.write(fd, line)
        except OSError:
            self.budget.give(len(line))
            raise
        finally:
            os.close(fd)
        self.entries.append(_Entry(seq, offset, len(line)))
        self.last_seq = seq
        return True

    # Sending

    def batch(self, max_events: int = runs.MAX_BATCH_EVENTS, max_bytes: int = BATCH_BYTES) -> list[dict]:
        """The next events to send, oldest first, within the hub's limits of a batch."""
        chosen: list[_Entry] = []
        total = 0
        for entry in self.entries:
            if len(chosen) >= max_events or (chosen and total + entry.size > max_bytes):
                break
            chosen.append(entry)
            total += entry.size
        if not chosen:
            return []
        events = []
        with open(self.path, "rb") as handle:
            for entry in chosen:
                handle.seek(entry.offset)
                events.append(json.loads(handle.read(entry.size)))
        return events

    def acknowledge(self, ack_seq: int) -> int:
        """Drop the events up to ``ack_seq``; how many were dropped."""
        if ack_seq <= self.ack_seq:
            return 0
        done = [entry for entry in self.entries if entry.seq <= ack_seq]
        self.entries = self.entries[len(done) :]
        for entry in done:
            self.budget.give(entry.size)
        if done:
            self._head = done[-1].offset + done[-1].size
        self.ack_seq = min(ack_seq, self.last_seq)
        self._save_ack()
        self._compact()
        return len(done)

    def _save_ack(self) -> None:
        data = json.dumps({"ack_seq": self.ack_seq, "last_seq": self.last_seq}).encode() + b"\n"
        write_atomic(self.ack_path, data, FILE_MODE)

    def _compact(self) -> None:
        if not self.entries:
            with contextlib.suppress(FileNotFoundError):
                os.truncate(self.path, 0)
            self._head = 0
            return
        if self._head < COMPACT_BYTES or self._head * 2 < self.entries[-1].offset + self.entries[-1].size:
            return
        with open(self.path, "rb") as handle:
            handle.seek(self._head)
            rest = handle.read()
        write_atomic(self.path, rest, FILE_MODE)
        shift = self._head
        for entry in self.entries:
            entry.offset -= shift
        self._head = 0

    def drop_all(self) -> None:
        """Give up on the events not acknowledged: the hub will not take them."""
        for entry in self.entries:
            self.budget.give(entry.size)
        self.entries = []
        self.ack_seq = self.last_seq
        self._save_ack()
        self._compact()

    def remove(self) -> None:
        """Delete the spool's files; the run is over and the hub has all it will take."""
        self.drop_all()
        for path in (self.path, self.ack_path):
            path.unlink(missing_ok=True)


def leftover_runs(directory: Path) -> list[int]:
    """Runs whose spool a previous daemon left in ``directory``."""
    found = set()
    with contextlib.suppress(OSError):
        for path in Path(directory).iterdir():
            if path.suffix in (".jsonl", ".ack") and path.stem.isdigit():
                found.add(int(path.stem))
    return sorted(found)
