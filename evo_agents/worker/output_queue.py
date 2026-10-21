"""The outputs of a run waiting on this worker for its daemon to send them to the hub (``runner.outputs``).

The commands of a run's agent put them here, and so does the daemon; the daemon takes each out once the hub has it,
or has refused it, while the run runs and when it ends:

```
runs/<run>/outputs/          0700
  queue/<ns>-<id>.json       0600  an item, in the order put: a file to send ({"file", "name", "media_type",
                                   "caption", "source", "tool_call_id", "wait"}), or a scan ({"scan": true})
  files/<id>                 0600  the bytes of a file as they were when it was put here
  answers/<id>.json          0600  what became of an item a command waits on: {"output_id": N} or {"error": TEXT}
```

``evo-agents worker output`` puts a copy of each file it is given, so the agent may change or remove the file
afterwards; ``worker ask --attach`` and ``worker chat post --attach`` put theirs and wait for the answers, which name
the outputs the question or the message carries; ``worker step`` puts a scan of the run's .evo-run/outputs/; the
daemon puts each image it takes out of a tool's result. A file of text is redacted when the daemon sends it, not
here: the values it masks are the leases the daemon holds. An item is written whole or not at all
(``write_atomic``), its file before it, so the daemon never reads half of one. Standard library only, as the commands
need the core package alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from evo_agents.hub import outputs
from evo_agents.hub.client import write_atomic
from evo_agents.worker.home import DIR_MODE, FILE_MODE, WorkerHome

OUTPUTS_DIR = "outputs"  # under the run's directory of the worker's state
ITEM_KEYS = ("name", "media_type", "caption", "source", "tool_call_id")
COPY_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Item:
    """One item of the queue: its id, its file in queue/, and what it says."""

    id: str
    path: Path
    entry: dict

    @property
    def scan(self) -> bool:
        return self.entry.get("scan") is True

    @property
    def waits(self) -> bool:
        return self.entry.get("wait") is True


class OutputQueue:
    """The queue of outputs of run ``run_id`` in the worker's state ``home``."""

    def __init__(self, home: WorkerHome, run_id: int):
        self.root = home.run_dir(int(run_id)) / OUTPUTS_DIR
        self.queue, self.files, self.answers = self.root / "queue", self.root / "files", self.root / "answers"

    def _ready(self) -> None:
        for directory in (self.root, self.queue, self.files, self.answers):
            directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)

    def _put(self, entry: dict) -> str:
        item_id = entry.get("file") or secrets.token_hex(8)
        self._ready()
        data = json.dumps({**entry, "id": item_id}, ensure_ascii=False).encode() + b"\n"
        write_atomic(self.queue / f"{time.time_ns():020d}-{item_id}.json", data, FILE_MODE)
        return item_id

    # Putting

    def put_file(
        self,
        path: Path,
        *,
        name: str,
        media_type: str,
        caption: str | None = None,
        source: str = outputs.SENT,
        wait: bool = False,
    ) -> str:
        """A copy of the file at ``path``, to send as an output of ``source``; its item's id."""
        self._ready()
        item_id = secrets.token_hex(8)
        target = os.open(self.files / item_id, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with open(path, "rb") as original, open(target, "wb") as copy:
            shutil.copyfileobj(original, copy, COPY_CHUNK)
        entry = {"file": item_id, "name": name, "media_type": media_type, "caption": caption, "source": source}
        return self._put({**entry, "wait": wait})

    def put_bytes(self, data: bytes, **entry) -> str:
        """``data`` to send as an output that ``entry`` describes (ITEM_KEYS); its item's id."""
        self._ready()
        item_id = secrets.token_hex(8)
        write_atomic(self.files / item_id, data, FILE_MODE)
        return self._put({"file": item_id, **{key: entry.get(key) for key in ITEM_KEYS}})

    def put_scan(self) -> str:
        """A scan of the run's .evo-run/outputs/, which the daemon makes when it takes the item."""
        return self._put({"scan": True})

    # Taking

    def items(self) -> list[Item]:
        """The items waiting, oldest first; one that does not read is dropped."""
        try:
            names = sorted(path.name for path in self.queue.iterdir() if path.suffix == ".json")
        except OSError:
            return []
        found = []
        for name in names:
            path = self.queue / name
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                found.append(Item(entry["id"], path, entry))
        return found

    def data(self, item: Item) -> bytes:
        """The bytes of the file of ``item``; OSError when they are gone."""
        return (self.files / str(item.entry.get("file"))).read_bytes()

    def finish(self, item: Item, answer: dict | None = None) -> None:
        """Take ``item`` out, with its file; ``answer`` is kept for the command that waits on it."""
        if item.waits and answer is not None:
            with contextlib.suppress(OSError):
                self._ready()
                write_atomic(self.answers / f"{item.id}.json", json.dumps(answer).encode() + b"\n", FILE_MODE)
        item.path.unlink(missing_ok=True)
        if item.entry.get("file"):
            (self.files / str(item.entry["file"])).unlink(missing_ok=True)

    def answer(self, item_id: str) -> dict | None:
        """What became of the item ``item_id`` a command waits on, once the daemon took it; it is read once."""
        path = self.answers / f"{item_id}.json"
        try:
            found = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        path.unlink(missing_ok=True)
        return found if isinstance(found, dict) else {"error": "the worker's answer does not read"}

    def pending(self) -> int:
        """The items waiting."""
        try:
            return sum(1 for path in self.queue.iterdir() if path.suffix == ".json")
        except OSError:
            return 0
