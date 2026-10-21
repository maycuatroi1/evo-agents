"""A run's outputs on their way to the hub (``evo_agents.hub.outputs``, docs/workers.md, section Outputs): the files
its agent made that the run's readers see on the hub.

- What the agent sends, in the run's queue (``evo_agents.worker.output_queue``): ``evo-agents worker output FILE...``
  (source sent), and the files ``worker ask --attach`` and ``worker chat post --attach`` wait on, whose answers name
  the outputs the question or the message carries.
- The images the agent's tools gave (``take``): an adapter takes each image out of a tool's result and links to it
  in the event (``AgentEvent.files``, ``outputs.output_link``); the run puts it in the queue as an output of source
  tool, of its tool call, once per SHA-256. One it does not send (the project's switch off, a limit, the hub refusing
  the run's outputs) gets a line saying why in place of its link, and a limit is said once in a ``system`` event.
- The files the agent left in the run's .evo-run/outputs/ (``outputs.OUTPUTS_DIR``, under the agent's directory: in
  the run's checkout, or above the worktrees of a plan run), as outputs of source outputs named by their path below
  it, when the agent reports a step (``worker step`` puts a scan in the queue) and when the run ends (``settle``,
  ``close``). Each file goes once: SENT_FILE, next to the directory, keeps the size, the time and the SHA-256 of each
  file sent, so a file is sent again only when its bytes changed, and a run that resumes a parked one in the same
  directory sends only what is new. Links, hidden files and folders, and files over the limit are left out.

A loop of its own sends the queue in batches (at most ``outputs.MAX_BATCH`` items, BATCH_BYTES of bytes, one item per
SHA-256): the presigned PUTs of what the project lacks, the bytes, then the outputs, each with its upload. A file of
text (``outputs.is_text``) goes through the redaction of the run's events first (``credentials.scrub``, the values of
the leases the daemon holds). A hub that does not answer holds nothing up: the items wait on disk and go with the
backoff of the worker. A refusal is never a failure of the run: a batch the hub refuses is sent again one item at a
time, and an item it refuses is a ``system`` event of the run (an image of a tool only once per limit); the hub
refusing the run's outputs altogether (404, 403) stops them for the run. The end of the run waits END_GRACE seconds
for the queue to empty before its last report, and CLOSE_GRACE more once the run is over here; what is left then
stays on this worker, which the cleanup of the run's state removes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import outputs
from evo_agents.hub.client import write_atomic
from evo_agents.worker import credentials
from evo_agents.worker.home import FILE_MODE
from evo_agents.worker.hubapi import Backoff, HubProblem, Outdated, Refused, Unreachable
from evo_agents.worker.output_queue import Item, OutputQueue
from evo_agents.worker.runner.common import log

if TYPE_CHECKING:
    from evo_agents.worker.adapter import AgentEvent, ToolImage
    from evo_agents.worker.runner.context import Run

POLL = 0.5  # seconds between looks at the queue, unless something pokes it
END_GRACE = 60.0  # seconds the end of a run waits for its outputs, before its last report
CLOSE_GRACE = 30.0  # seconds more once the run is over here
BATCH_BYTES = 64 * outputs.MiB
SENT_FILE = "outputs-sent.json"  # in .evo-run/, next to outputs/
HASH_CHUNK = 1024 * 1024
GONE = (401, 403, 404)  # the hub takes no outputs of the run: the run is not this worker's, or the hub is older


def redacted(data: bytes) -> bytes:
    """The bytes of a file of text with every lease value the daemon holds masked, as in the run's events; bytes
    that are not UTF-8 stay as they are."""
    text = data.decode("utf-8", "surrogateescape")
    clean = credentials.scrub(text)
    return data if clean == text else clean.encode("utf-8", "surrogateescape")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def outputs_files(base: Path) -> list[tuple[str, Path]]:
    """(name, path) of each regular file under ``base``, its path below it as the name, sorted; links, and files and
    folders whose name starts with a dot, left out."""
    found = []
    for root, folders, files in os.walk(base, followlinks=False):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            path = Path(root) / name
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            found.append((path.relative_to(base).as_posix(), path))
    return sorted(found)


def _left_out(item, why: dict[str, str]):
    """An item of an event's content, the link to an image the run does not send replaced by a line saying why."""
    block = item.get("content") if isinstance(item, dict) else None
    uri = block.get("uri") if isinstance(block, dict) else None
    if uri not in why:
        return item
    text = f"[{block.get('mimeType')} image of {block.get('size')} bytes, not kept: {why[uri]}]"
    return {"type": "content", "content": {"type": "text", "text": text}}


class Outputs:
    """The outputs of one run on their way to the hub."""

    def __init__(self, run: Run):
        self.run = run
        self.queue = OutputQueue(run.daemon.home, run.id)
        self.limits = {
            "max_file_bytes": outputs.MAX_FILE_BYTES,
            "max_outputs": outputs.MAX_RUN_OUTPUTS,
            "max_run_bytes": outputs.MAX_RUN_BYTES,
        }
        self.base = [0, 0]  # outputs and bytes the hub said the run held when this daemon asked
        self.counted: dict[str, int] = {}  # SHA-256 -> size of each output sent or queued from here
        self.settings_read = False
        self.tool_images = True  # the project takes images out of tools' results, as far as the run knows
        self.refused: str | None = None  # why the hub takes no outputs of this run: nothing more is sent
        self.noted: set[str] = set()  # the limits of tool images said once
        self.poked = asyncio.Event()
        self.closing = False
        self.task: asyncio.Task | None = None
        self.backoff = Backoff()

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._loop())

    def poke(self) -> None:
        self.poked.set()

    # The images of tools' results

    def take(self, event: AgentEvent) -> dict:
        """The body of ``event`` to log, its images (``event.files``) put in the queue as outputs of source tool."""
        if not event.files:
            return event.body
        call = event.body.get("toolCallId")
        why: dict[str, str] = {}
        for image in event.files:
            refusal = self._image_refusal(image)
            if refusal is not None:
                why[outputs.link_uri(image.sha256)] = refusal
                continue
            if image.sha256 in self.counted:  # a run holds one output per SHA-256, the first tool call's
                continue
            try:
                self.queue.put_bytes(
                    image.data, name=image.name, media_type=image.media_type, source=outputs.TOOL, tool_call_id=call
                )
            except OSError as exc:
                why[outputs.link_uri(image.sha256)] = f"the worker could not keep it ({type(exc).__name__})"
                continue
            self.counted[image.sha256] = len(image.data)
            self.poke()
        if not why:
            return event.body
        return {**event.body, "content": [_left_out(item, why) for item in event.body.get("content") or []]}

    def _image_refusal(self, image: ToolImage) -> str | None:
        if self.refused is not None:
            return self.refused
        if not self.tool_images:
            return "the project takes no images out of tools' results"
        if image.sha256 in self.counted:
            return None
        size = len(image.data)
        if size > self.limits["max_file_bytes"]:
            return self._limit("file_bytes", f"an output is at most {self.limits['max_file_bytes']} bytes")
        count, held = self.base[0] + len(self.counted), self.base[1] + sum(self.counted.values())
        if count >= self.limits["max_outputs"]:
            return self._limit("outputs_per_run", f"a run holds at most {self.limits['max_outputs']} outputs")
        if held + size > self.limits["max_run_bytes"]:
            return self._limit("bytes_per_run", f"a run holds at most {self.limits['max_run_bytes']} bytes of outputs")
        return None

    def _limit(self, limit: str, text: str) -> str:
        """``text``, said once in the run's log for the images of its tools."""
        if limit not in self.noted:
            self.noted.add(limit)
            self.run.note(f"An image of a tool's result was not kept, nor will the next ones be: {text}.", limit=limit)
        return text

    # .evo-run/outputs/

    def _scan(self) -> tuple[int, list[str]]:
        """Put each file of the run's .evo-run/outputs/ not sent before, or changed since, in the queue: how many, and
        the notes for the run's log. Runs in a thread: it reads and hashes the files."""
        worktree = self.run.worktree
        base = Path(worktree) / outputs.OUTPUTS_DIR if worktree is not None else None
        if base is None or not base.is_dir():
            return 0, []
        ledger = base.parent / SENT_FILE
        try:
            sent = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sent = {}
        sent = sent if isinstance(sent, dict) else {}
        put, notes = 0, []
        for name, path in outputs_files(base):
            try:
                stat = path.stat()
                mark = [stat.st_size, stat.st_mtime_ns]
                seen = sent.get(name)
                if isinstance(seen, list) and seen[:2] == mark:
                    continue
                sha = file_sha256(path)
                if not (isinstance(seen, list) and seen[2:] == [sha]):
                    notes += self._put_found(name, path, stat.st_size)
                    put += 1
                sent[name] = [*mark, sha]
            except OSError as exc:
                notes.append(f"{outputs.OUTPUTS_DIR}/{name} was not sent: {type(exc).__name__}: {exc}")
        with contextlib.suppress(OSError):
            write_atomic(ledger, json.dumps(sent, ensure_ascii=False).encode() + b"\n", FILE_MODE)
        return put, notes

    def _put_found(self, name: str, path: Path, size: int) -> list[str]:
        """A file of .evo-run/outputs/ in the queue; the note when it is left out instead."""
        try:
            name = outputs.check_name(name)
        except ValueError as exc:
            return [f"{outputs.OUTPUTS_DIR}/{name} was not sent: {exc}"]
        if size > self.limits["max_file_bytes"]:
            return [
                f"{outputs.OUTPUTS_DIR}/{name} was not sent: a file is at most {self.limits['max_file_bytes']} bytes"
            ]
        media_type = outputs.guess_media_type(name)
        self.queue.put_file(path, name=name, media_type=media_type, source=outputs.OUTPUTS)
        return []

    async def scan(self) -> None:
        put, notes = await asyncio.to_thread(self._scan)
        for text in notes:
            self.run.note(text)
        if put:
            self.poke()

    # The end of the run

    async def settle(self, grace: float = END_GRACE) -> None:
        """At the end of the run, before its last report: scan .evo-run/outputs/, and wait at most ``grace`` seconds
        for the queue to empty."""
        if self.task is None or self.task.done():
            return
        with contextlib.suppress(OSError):
            self.queue.put_scan()
        self.poke()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        stop = self.run.daemon.hard_stop
        while self.queue.pending() and loop.time() < deadline and not self.task.done() and not stop.is_set():
            await asyncio.sleep(0.05)

    async def close(self, grace: float = CLOSE_GRACE) -> None:
        """The run is over here: a last scan, at most ``grace`` seconds more for the queue, then the loop ends; what is
        left is said in the run's log."""
        if self.task is None:
            return
        with contextlib.suppress(OSError):
            self.queue.put_scan()
        self.closing = True
        self.poke()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self.task), grace)
        if not self.task.done():
            self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        left = self.queue.pending()
        if left:
            self.run.note(f"{left} output(s) of the run were not sent: the hub did not take them in time.", left=left)

    # Sending

    async def _loop(self) -> None:
        while True:
            self.poked.clear()
            try:
                delay = await self._round()
            except Exception:  # an output never fails the run
                log.exception("the run's outputs were not sent", extra={"run_id": self.run.id})
                delay = POLL
            if self.closing and not self.queue.pending():
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.poked.wait(), delay)

    async def _round(self) -> float:
        """Send what the queue holds; the seconds to wait before the next round."""
        if not self.settings_read:
            await self._read_settings()
        files = []
        for item in self.queue.items():
            if item.scan:
                await self.scan()
                self.queue.finish(item)
            elif self.refused is not None:
                self._drop(item, self.refused, said=True)
            else:
                files.append(item)
        if files and not await self._send_all(files):
            return self.backoff.next()
        self.backoff.reset()
        return POLL

    async def _read_settings(self) -> None:
        try:
            answer = await self.run.daemon.hub.output_settings(self.run.id)
        except Unreachable:
            return
        except Refused as exc:  # an older hub without outputs, or a run no longer this worker's
            self.settings_read = True
            if exc.status in GONE or isinstance(exc, Outdated):
                self.refused = f"the hub takes no outputs of this run ({exc})"
            return
        self.settings_read = True
        self.tool_images = answer.get("tool_images") is not False
        for key in self.limits:
            if isinstance(answer.get(key), int) and answer[key] > 0:
                self.limits[key] = answer[key]
        self.base = [int(answer.get("outputs") or 0), int(answer.get("bytes") or 0)]

    def _prepare(self, item: Item) -> tuple[dict, bytes] | str:
        """The output ``item`` is, as the hub records it, and its bytes, redacted when it is text; why not, else."""
        entry = item.entry
        if entry.get("source") == outputs.TOOL and not self.tool_images:
            return "the project takes no images out of tools' results"
        try:
            data = self.queue.data(item)
        except OSError:
            return "its file is gone from the worker"
        media_type = str(entry.get("media_type") or "application/octet-stream")
        if outputs.is_text(media_type):
            data = redacted(data)
        if len(data) > self.limits["max_file_bytes"]:
            return f"a file is at most {self.limits['max_file_bytes']} bytes, and it has {len(data)}"
        found = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "source": entry.get("source")}
        found.update(name=entry.get("name"), media_type=media_type)
        found.update({key: entry[key] for key in ("caption", "tool_call_id") if entry.get(key)})
        return found, data

    async def _send_all(self, files: list[Item]) -> bool:
        """Send ``files`` in batches; False when the hub did not answer, the rest left for the next round."""
        batch: list[tuple[Item, dict, bytes]] = []
        size = 0
        for item in files:
            prepared = self._prepare(item)
            if isinstance(prepared, str):  # an image of a tool is said once per limit, never one by one
                self._drop(item, prepared, said=item.entry.get("source") == outputs.TOOL)
                continue
            entry, data = prepared
            full = len(batch) >= outputs.MAX_BATCH or size + len(data) > BATCH_BYTES
            if batch and (full or entry["sha256"] in {sent["sha256"] for _, sent, _ in batch}):
                if not await self._send(batch):
                    return False
                batch, size = [], 0
            batch.append((item, entry, data))
            size += len(data)
        return await self._send(batch) if batch else True

    async def _send(self, batch: list[tuple[Item, dict, bytes]]) -> bool:
        """One batch to the hub; False when the hub did not answer."""
        try:
            recorded = await self._upload(batch)
        except Unreachable as exc:
            log.warning("outputs not sent; trying again", extra={"run_id": self.run.id, "error": str(exc)})
            return False
        except Refused as exc:
            return await self._refused(batch, exc)
        for (item, entry, _), output in zip(batch, recorded, strict=True):
            self.counted[entry["sha256"]] = entry["size"]
            self.queue.finish(item, {"output_id": output.get("id"), "name": entry["name"]})
        log.info("outputs sent", extra={"run_id": self.run.id, "outputs": len(batch)})
        return True

    async def _upload(self, batch: list[tuple[Item, dict, bytes]]) -> list[dict]:
        """The uploads of what the project lacks, then the outputs recorded, in order; once more when the project lost
        bytes in between (409)."""
        hub = self.run.daemon.hub
        data_of = {entry["sha256"]: data for _, entry, data in batch}
        for attempt in (1, 2):
            asked = [{key: entry[key] for key in ("sha256", "size", "source")} for _, entry, _ in batch]
            answer = await hub.output_uploads(self.run.id, asked)
            uploads = {}
            try:
                for ticket in answer.get("uploads") or []:
                    await hub.put(ticket["url"], data_of[ticket["sha256"]])
                    uploads[ticket["sha256"]] = ticket["upload_id"]
            except (KeyError, TypeError, AttributeError):
                raise Refused("the hub's answer of the uploads does not read") from None
            entries = [{**entry, "upload_id": uploads.get(entry["sha256"])} for _, entry, _ in batch]
            try:
                answer = await hub.record_outputs(self.run.id, entries)
            except Refused as exc:
                if exc.status == 409 and attempt == 1:
                    continue
                raise
            found = answer.get("outputs") if isinstance(answer, dict) else None
            if not isinstance(found, list) or len(found) != len(batch) or not all(isinstance(o, dict) for o in found):
                raise Refused("the hub did not answer with the outputs it recorded")
            return found
        raise Refused("the hub did not record the outputs")

    async def _refused(self, batch: list[tuple[Item, dict, bytes]], exc: HubProblem) -> bool:
        """The hub refused ``batch``: stop the run's outputs, leave out its tool images, send it one item at a time,
        or drop its one item, as the refusal says."""
        if exc.status in GONE or isinstance(exc, Outdated):
            self.refused = f"the hub takes no outputs of this run ({exc})"
            self.run.note(f"The run's outputs are not sent: {self.refused}.")
            for item, _, _ in batch:
                self._drop(item, self.refused, said=True)
            return True
        tools = [part for part in batch if part[1]["source"] == outputs.TOOL]
        if exc.status == 409 and tools:
            self.tool_images = False
            self.run.note("The project's admin turned off the images of tools' results: the run sends none of them.")
            for item, _, _ in tools:
                self._drop(item, "the project takes no images out of tools' results", said=True)
            rest = [part for part in batch if part[1]["source"] != outputs.TOOL]
            return await self._send(rest) if rest else True
        if len(batch) > 1:
            for part in batch:
                if not await self._send([part]):
                    return False
            return True
        item, entry, _ = batch[0]
        if entry["source"] == outputs.TOOL:
            detail = exc.payload.get("detail") if isinstance(exc.payload, dict) else None
            limit = (
                detail[0].get("limit") if isinstance(detail, list) and detail and isinstance(detail[0], dict) else None
            )
            self._limit(str(limit or exc.status), str(exc))
            self._drop(item, str(exc), said=True)
        else:
            self._drop(item, str(exc))
        return True

    def _drop(self, item: Item, why: str, *, said: bool = False) -> None:
        """Take ``item`` out unsent, with ``why`` for the command that waits on it, and in the run's log unless it was
        said already."""
        if not said:
            name = item.entry.get("name") or "a file"
            self.run.note(f"{name} was not sent to the run's outputs on the hub: {why}", output=name)
        self.queue.finish(item, {"error": why})
