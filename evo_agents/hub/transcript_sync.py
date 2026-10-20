"""``evo-agents hub sessions sync`` and ``hub sessions backfill``: the transcripts of this machine's sessions to the hub.

Both find transcripts (``evo_agents.hub.transcripts``), decide the project of each from its directory as memories do
(``load_places``, one request), leave out those of no project, and send each file's bytes the hub does not
hold yet as parts (``evo_agents.hub.server.transcripts``): per project, batches of at most BATCH_PARTS parts and
BATCH_BYTES bytes, each asked for (POST /v1/blobs/uploads, kind transcript), PUT to its presigned URL and recorded with
the commit of its uploads (POST /v1/projects/{p}/transcripts/parts). The hub answers each part's status and what it
holds of the file then; a part sent twice is stored once, and a client that lost its place starts again where the hub
says (at most MAX_CONFLICTS times a file in one run). A part whose blob the project already holds is not uploaded
again.

- ``sync`` is what the hooks start in the background (``transcripts.spawn_sync``). It takes this machine's sync lock
  (or leaves at once when another sync holds it), waits ``--wait`` seconds so the turns of a moment go together, then
  sends the sessions the hooks queued, their subagents' transcripts, and any transcript changed since an hour before
  the last sync (a day back the first time). It trusts ``state.json``: a file the hub held whole last time and that has
  not changed since is not asked about. It takes the queue again before it lets the lock go, and after, starts another
  sync when a hook queued a session meanwhile, so no Stop is left waiting for the next one.
- ``backfill`` sends every transcript of this machine (``--since`` a day: those changed on it or later), asking the hub
  what it holds of each first (POST .../transcripts/held), and ``--dry-run`` only counts: the sessions and files of each
  project, those the hub holds whole, those it misses, and among these the sessions unchanged for more than
  ENDED_AFTER (an hour), which should be on the hub already. A second run uploads nothing.

A hub that does not answer stops the run, keeping what was sent; a project the member may not write to, or whose blob
store does not answer, is an error of its own and the other projects go on. Nothing here prints a transcript's text;
``sync`` writes one line per run to ``sync.log``. Standard library only.
"""

from __future__ import annotations

import contextlib
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub import transcripts as local
from evo_agents.hub.client import Hub, HubError, Unreachable, put_presigned
from evo_agents.hub.memory import Places, host_tag
from evo_agents.hub.registry import default_registry, read_registry
from evo_agents.kg.project import read_bindings

MiB = 1024 * 1024
BATCH_PARTS = 50
BATCH_BYTES = 64 * MiB
LOOKUP = 500  # session ids in one held request
ENDED_AFTER = 3600.0  # seconds without a change after which a session counts as ended
LOOK_BACK = 3600.0  # seconds before the last sync the next one looks for changed transcripts from
FIRST_LOOK = 86400.0  # how far back the first sync of a machine looks; backfill takes the rest
MAX_CONFLICTS = 3  # restarts of one file in one run
PUT_ATTEMPTS = 3
REFUSAL_COOLDOWN = 86400.0  # seconds a transcript another member holds is left alone
COMMIT_SECONDS = 20.0  # what the parts request may take, plus a second for each part
BACKFILL_LOCK_WAIT = 60.0  # seconds a backfill waits for a sync of the hooks


@dataclass
class Report:
    """What a sync or a backfill found and did; ``as_json`` is its --json."""

    hub: str
    dry_run: bool = False
    project: str | None = None
    since: str | None = None
    sessions: set = field(default_factory=set)  # (project, session id)
    transcripts: int = 0
    complete: int = 0
    missing: int = 0
    missing_sessions: set = field(default_factory=set)
    missing_ended: set = field(default_factory=set)
    uploaded_parts: int = 0
    uploaded_bytes: int = 0
    uploaded_transcripts: set = field(default_factory=set)
    projects: dict = field(default_factory=dict)
    skipped: Counter = field(default_factory=Counter)
    errors: list = field(default_factory=list)

    def _project(self, name: str) -> Counter:
        return self.projects.setdefault(name, Counter())

    def as_json(self) -> dict:
        return {
            "hub": self.hub,
            "dry_run": self.dry_run,
            "project": self.project,
            "since": self.since,
            "sessions": len(self.sessions),
            "transcripts": self.transcripts,
            "complete": self.complete,
            "missing": self.missing,
            "missing_sessions": len(self.missing_sessions),
            "missing_ended": len(self.missing_ended),
            "uploaded": {
                "parts": self.uploaded_parts,
                "bytes": self.uploaded_bytes,
                "transcripts": len(self.uploaded_transcripts),
            },
            "projects": {name: dict(sorted(counts.items())) for name, counts in sorted(self.projects.items())},
            "skipped": dict(sorted(self.skipped.items())),
            "errors": self.errors,
        }

    def summary(self) -> str:
        verb = "would upload" if self.dry_run else "uploaded"
        done = (
            f"{len(self.sessions)} session(s), {self.transcripts} transcript file(s) of hub projects: {self.complete} "
            f"whole on the hub, {self.missing} missing"
        )
        if self.dry_run:
            done += f" ({len(self.missing_ended)} of the sessions missing one ended more than an hour ago)"
        else:
            done += f"; {verb} {self.uploaded_parts} part(s), {self.uploaded_bytes} bytes"
        skipped = ", ".join(f"{n} {why}" for why, n in sorted(self.skipped.items()))
        return done + (f"; skipped {skipped}" if skipped else "") + "."


@dataclass
class Work:
    """A file to send from ``offset`` (line ``line``)."""

    found: local.Found
    project: str
    offset: int
    line: int
    total: int
    restarts: int = 0
    closed: bool = False  # another member's, or deleted on the hub: counted as skipped, not as missing


class Sync:
    """One run over a list of transcripts, with one hub, as ``login``."""

    def __init__(self, hub: Hub, login: str, base: Path, *, dry_run: bool = False, project: str | None = None):
        self.hub = hub
        self.login = login
        self.base = base
        self.state = local.State(base, hub.url)
        self.machine = host_tag()
        self.values = [hub.token]
        self.now = time.time()
        self.report = Report(hub.url, dry_run, project)
        self.progress = None  # a callable taking one line, for a backfill run by a person
        self.unreachable = False
        self.oldest_missing: float | None = None  # the mtime of the oldest file the run left with bytes to send

    # Deciding

    def _sort(self, found: list[local.Found]) -> dict[str, list[local.Found]]:
        places = load_places(self.hub)
        by_project: dict[str, list[local.Found]] = {}
        for item in found:
            if item.slug is None:
                self.report.skipped["without a directory"] += 1
                continue
            try:
                project = places.project_of(item.slug)
            except HubError:
                self.report.skipped["in a directory two projects claim"] += 1
                continue
            if project is None:
                self.report.skipped["of no hub project"] += 1
                continue
            if self.report.project is not None and project != self.report.project:
                continue
            by_project.setdefault(project, []).append(item)
            self.report.sessions.add((project, item.session_id))
            self.report.transcripts += 1
            self.report._project(project)["transcripts"] += 1
        for project, items in by_project.items():
            self.report._project(project)["sessions"] = len({item.session_id for item in items})
        return by_project

    def _held(self, project: str, sessions: set[str]) -> dict[tuple[str, str], dict]:
        found: dict[tuple[str, str], dict] = {}
        ordered = sorted(sessions)
        for start in range(0, len(ordered), LOOKUP):
            answer = self.hub.call(
                "POST",
                f"/v1/projects/{quote(project, safe='')}/transcripts/held",
                {"session_ids": ordered[start : start + LOOKUP]},
            )
            for entry in answer["transcripts"]:
                found[(entry["session_id"], entry["file"])] = entry
        return found

    def _total(self, item: local.Found) -> int:
        entry = self.state.get(item)
        if item.compressed and (entry.get("size"), entry.get("mtime")) == (item.size, item.mtime):
            if isinstance(entry.get("source_size"), int):
                return entry["source_size"]
        return local.source_size(item)

    def _plan(self, project: str, items: list[local.Found], trust_state: bool) -> list[Work]:
        """The files of ``project`` with bytes the hub does not hold, after counting those it holds whole."""
        if trust_state:
            fresh = []
            for item in items:
                refused = self.state.get(item).get("refused_at")
                if self.state.unchanged(item):
                    self._complete(item, project)
                elif isinstance(refused, (int, float)) and self.now - refused < REFUSAL_COOLDOWN:
                    self.report.skipped["another member's"] += 1
                else:
                    fresh.append(item)
            items = fresh
        held = self._held(project, {item.session_id for item in items}) if items else {}
        work = []
        for item in items:
            entry = held.get((item.session_id, item.file)) or {}
            if entry.get("deleted"):
                self.report.skipped["deleted on the hub"] += 1
                self.state.put(item, complete=True, deleted=True)
                continue
            try:
                total = self._total(item)
            except local.Unreadable as exc:
                self.report.skipped["unreadable"] += 1
                self.report.errors.append(str(exc))
                continue
            except OSError as exc:
                self.report.skipped["unreadable"] += 1
                self.report.errors.append(f"{item.path.name}: {type(exc).__name__}")
                continue
            offset, line = entry.get("source_bytes", 0), entry.get("lines", 0) + 1
            if offset >= total:
                self.state.put(item, offset=offset, line=line, complete=True, source_size=total)
                self._complete(item, project)
            else:
                work.append(Work(item, project, offset, line, total))
        return work

    def _complete(self, item: local.Found, project: str) -> None:
        self.report.complete += 1
        self.report._project(project)["complete"] += 1

    def _missing(self, work: Work) -> None:
        mtime = work.found.mtime
        self.oldest_missing = mtime if self.oldest_missing is None else min(self.oldest_missing, mtime)
        self.report.missing += 1
        self.report._project(work.project)["missing"] += 1
        self.report.missing_sessions.add((work.project, work.found.session_id))
        if self.now - work.found.mtime > ENDED_AFTER:
            self.report.missing_ended.add((work.project, work.found.session_id))

    # Sending

    def _put(self, url: str, path: Path) -> None:
        for attempt in range(PUT_ATTEMPTS):
            try:
                put_presigned(url, path)
                return
            except HubError as exc:
                last = attempt == PUT_ATTEMPTS - 1
                if last or not (isinstance(exc, Unreachable) or (exc.status or 0) >= 500):
                    raise
                time.sleep(1.0 * 2**attempt)

    def _flush(self, project: str, batch: list[local.Part]) -> list[dict]:
        """Upload the blobs of ``batch`` the project lacks and record its parts; the hub's outcome of each part."""
        sizes = {part.sha256: (part.size, part.path) for part in batch}
        items = [{"sha256": sha, "size": size, "kind": local.KIND} for sha, (size, _) in sorted(sizes.items())]
        asked = self.hub.call("POST", "/v1/blobs/uploads", {"project": project, "items": items})
        for ticket in asked["uploads"]:
            self._put(ticket["url"], sizes[ticket["sha256"]][1])
        body = {
            "upload_ids": [ticket["upload_id"] for ticket in asked["uploads"]],
            "parts": [part.wire(self.machine) for part in batch],
        }
        timeout = COMMIT_SECONDS + len(batch)
        answer = self.hub.call(
            "POST", f"/v1/projects/{quote(project, safe='')}/transcripts/parts", body, timeout=timeout
        )
        return answer["parts"]

    def _settle(self, work: Work, part: local.Part, outcome: dict) -> bool:
        """Take the hub's outcome of one part of ``work``; whether the file goes on from where the part ended."""
        status = outcome["status"]
        if status in ("stored", "held"):
            work.offset, work.line = part.end, part.first_line + part.lines
            complete = work.offset >= work.total
            self.state.put(work.found, offset=work.offset, line=work.line, complete=complete, source_size=work.total)
            if status == "stored":
                self.report.uploaded_parts += 1
                self.report.uploaded_bytes += part.size
                self.report.uploaded_transcripts.add(work.found.key)
                self.report._project(work.project)["uploaded_parts"] += 1
            return True
        if status == "refused":
            self.state.put(work.found, refused_at=self.now)
            self.report.skipped["another member's"] += 1
            work.closed = True
        elif status == "deleted":
            self.state.put(work.found, complete=True, deleted=True)
            self.report.skipped["deleted on the hub"] += 1
            work.closed = True
        else:  # conflict or missing: from where the hub's copy ends
            work.offset, work.line = outcome["source_bytes"], outcome["lines"] + 1
            work.restarts += 1
        return False

    def _batch(self, queue: list[Work]) -> tuple[list[tuple[Work, local.Part]], set[str]]:
        """The next parts of ``queue`` in order, up to a batch, and the files with no whole line left to send now."""
        into = self.base / local.PARTS_DIR
        batch: list[tuple[Work, local.Part]] = []
        size, exhausted = 0, set()
        for work in queue:
            offset, line = work.offset, work.line
            while offset < work.total and part_room(batch, size):
                part = local.read_part(work.found, offset, line, self.values, into)
                if part is None:
                    exhausted.add(work.found.key)
                    break
                batch.append((work, part))
                size += part.size
                offset, line = part.end, part.first_line + part.lines
            if not part_room(batch, size):
                break
        return batch, exhausted

    def _send(self, project: str, works: list[Work]) -> None:
        """Send the parts of ``works`` in batches, going on from each outcome, until each file is whole on the hub,
        has no whole line left, cannot go on now, or restarted MAX_CONFLICTS times."""
        queue = list(works)
        while queue:
            batch, exhausted = self._batch(queue)
            if not batch:
                return
            try:
                outcomes = self._flush(project, [part for _, part in batch])
            finally:
                for _, part in batch:
                    with contextlib.suppress(OSError):
                        part.path.unlink()
            by_part = {(o["session_id"], o["file"], o["start"]): o for o in outcomes}
            stopped, again = set(), set()
            for work, part in batch:
                key = work.found.key
                if key in stopped:
                    continue
                outcome = by_part.get((part.found.session_id, part.found.file, part.start))
                if outcome is None or not self._settle(work, part, outcome):
                    stopped.add(key)
                    if outcome is not None and outcome["status"] in ("conflict", "missing"):
                        again.add(key)
            self.state.save()
            if self.progress is not None:
                self.progress(f"{project}: {self.report.uploaded_parts} part(s), {self.report.uploaded_bytes} bytes")

            queue = [work for work in queue if _going(work, again, stopped, exhausted)]

    def _finish(self, works: list[Work]) -> None:
        for work in works:
            if work.closed:
                continue
            if work.offset >= work.total:
                self._complete(work.found, work.project)
            else:
                self._missing(work)

    def run(self, found: list[local.Found], *, trust_state: bool = False) -> Report:
        try:
            for project, items in self._sort(found).items():
                works: list[Work] = []
                try:
                    works = self._plan(project, items, trust_state)
                    if not self.report.dry_run:
                        self._send(project, works)
                except Unreachable:
                    self._finish(works)
                    raise
                except (HubError, OSError, local.Unreadable) as exc:
                    self.report.errors.append(f"project {project}: {exc}")
                self._finish(works)
        except Unreachable as exc:
            self.unreachable = True
            self.report.errors.append(f"the hub did not answer ({exc}); what was sent stays, the rest goes next time")
        finally:
            if not self.report.dry_run:
                self.state.save()
        return self.report


def load_places(hub: Hub) -> Places:
    """The places of this machine's directories as the projects the person sees claim them now, as memory push and
    pull decide them (``MemorySync``): one request, then ``Places.project_of`` decides any number of directories."""
    projects = hub.call("GET", "/v1/projects")
    if not isinstance(projects, list):
        raise HubError(f"the hub at {hub.url} did not answer with a list of projects")
    registry, _ = read_registry(default_registry().expanduser())
    return Places.from_hub(projects, registry, hub.url, Path.home(), read_bindings())


def _going(work: Work, again: set[str], stopped: set[str], exhausted: set[str]) -> bool:
    """Whether a file goes on in the next batch: bytes left, restarts left, and a restart asked for, or neither a stop
    nor the end of its whole lines."""
    key = work.found.key
    if work.offset >= work.total or work.restarts > MAX_CONFLICTS:
        return False
    return key in again or (key not in stopped and key not in exhausted)


def part_room(batch: list, size: int) -> bool:
    """Whether a batch has room for another part."""
    return len(batch) < BATCH_PARTS and size < BATCH_BYTES


# Entry points


def since_of(text: str | None) -> float | None:
    """``--since`` YYYY-MM-DD (00:00 UTC) or an ISO time as a POSIX time; ValueError for anything else."""
    if text is None:
        return None
    moment = datetime.fromisoformat(text)
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def backfill(
    hub: Hub, login: str, *, project: str | None = None, since: str | None = None, dry_run: bool = False, progress=None
) -> Report:
    """Send every transcript of this machine the hub misses (see the module)."""
    base = local.directory()
    start = since_of(since)
    lock = contextlib.nullcontext(True) if dry_run else local.sync_lock(base, wait=BACKFILL_LOCK_WAIT)
    with lock as held:
        if not held:
            raise HubError(
                f"another sync of transcripts on this machine held {base / local.LOCK_FILE} for "
                f"{BACKFILL_LOCK_WAIT:g}s; try again when it is done"
            )
        sync = Sync(hub, login, base, dry_run=dry_run, project=project)
        sync.report.since = since
        sync.progress = progress
        return sync.run(local.find_all(start, sync.state.roots))


def _queued(state: local.State, entries: dict[str, dict]) -> list[local.Found]:
    found = []
    for entry in entries.values():
        path = Path(str(entry.get("transcript") or ""))
        items = local.given(path) if path.is_absolute() else []
        for item in items:
            if item.runtime == local.CLAUDE and item.file == local.MAIN:
                state.add_root(path.parent.parent)
        found += items
    return found


def sync(hub: Hub, login: str, *, wait: float = 0.0) -> Report | None:
    """What the hooks start (see the module); None when another sync of this machine runs."""
    base = local.directory()
    with local.sync_lock(base) as held:
        if not held:
            return None
        if wait > 0:
            time.sleep(wait)
        report = None
        while True:
            taken = local.Queue(base).entries()
            run = Sync(hub, login, base)
            since = (run.state.synced_at - LOOK_BACK) if run.state.synced_at else run.now - FIRST_LOOK
            found = {item.key: item for item in _queued(run.state, taken)}
            for item in local.find_all(since, run.state.roots):
                found.setdefault(item.key, item)
            report = run.run(sorted(found.values(), key=lambda item: item.key), trust_state=True)
            if run.unreachable:
                break
            # A file left with bytes to send stays in the next sync's look back, though nothing queues it again.
            run.state.mark_synced(min(run.now, run.oldest_missing or run.now))
            run.state.save()
            with contextlib.suppress(OSError, TimeoutError):
                local.Queue(base).drop(taken)
            if not _newer(local.Queue(base).entries(), taken):
                break
        local.log_line(base, report.summary() + (f" Errors: {'; '.join(report.errors)}" if report.errors else ""))
    if _newer(local.Queue(base).entries(), taken):
        with contextlib.suppress(OSError):
            local.spawn_sync(base, wait=0)
    return report


def _newer(entries: dict[str, dict], taken: dict[str, dict]) -> bool:
    return any(taken.get(key, {}).get("at") != entry.get("at") for key, entry in entries.items())
