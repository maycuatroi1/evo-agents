"""``evo-agents hub kg push`` and ``kg sync --push``: send the runs of a project's corpus that the hub lacks.

For a project: send its knowledge config (``evo_agents.hub.kg_ingest.knowledge_config``), ask which runs the hub has,
then push every finished run here that it has not ingested, oldest first, in the three steps the hub takes
(``evo_agents.hub.server.kg``): upload the log through a presigned PUT, have the hub check it, upload and commit the
blobs it says it lacks, commit the run. A log the hub holds already from an attempt that stopped half way is named by
its hash instead of being uploaded again.

The hub reads every upload back from its blob store when it is committed, a few round trips each, so a commit takes
longer the more it carries: 0.2.0 hubs on R2 needed about 90 ms per upload, and one commit of 459 outlasted the
30 seconds a request waited. Blobs therefore go in batches of at most COMMIT_BATCH uploads and COMMIT_BYTES bytes,
each asked for, PUT (PARALLEL_PUTS at a time) and committed before the next, so no presigned URL waits long enough
to expire. A commit waits ``commit_timeout`` for its answer and the log check ``log_timeout``, both grown from the
work they carry. A commit that got no answer may still finish on the hub: the push asks which of the batch's blobs
the project holds for as long again before it calls the batch failed.

Every step is idempotent (the hub keeps blobs by hash and runs by id), so a run that fails on the way for a reason
that may pass (no answer, a refused connection, 408, 429, 502, 503, 504) is pushed again from its start, at most
RUN_ATTEMPTS times with RETRY_DELAYS between them; the hub answers what it has already and only the rest is sent. A
PUT to the blob store is tried PUT_ATTEMPTS times. ``progress``, when given, receives a line per run and per batch
(``hub kg push`` prints them on stderr).

A run of a source whose own label the project's hub sink does not clear never leaves the machine: it is counted as
kept here, not sent to be refused. A run the hub refuses (an item raised above the hub sink, a blob over the size
limit) is reported and the next run goes on. A hub that cannot be reached or refuses the project stops that project;
a credential it refuses stops everything. ``push_all`` pushes every project of this machine's projects.json that the
caller may push to on the hub and says why it skipped the others.

Standard library only, like the rest of the client.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.client import Hub, HubError, Unreachable, put_presigned
from evo_agents.hub.kg_ingest import knowledge_config, log_header, run_id_of
from evo_agents.kg.project import Project, ProjectError

MiB = 1024 * 1024
COMMIT_BATCH = 100  # uploads in one blob commit
COMMIT_BYTES = 256 * MiB  # bytes in one blob commit; a larger blob goes alone
PARALLEL_PUTS = 8
BASE_TIMEOUT = 30.0  # seconds any request may take, as Hub's default
SECONDS_PER_UPLOAD = 0.5  # what a commit may take per upload; 0.2.0 hubs needed about 0.09 on R2
READ_RATE = 8 * MiB  # bytes per second the hub reads uploads back at, at the least
LOG_READ_RATE = 1 * MiB  # bytes of a run log per second the hub checks, reading it twice and labelling every item
SETTLE_INTERVAL = 5.0  # seconds between asking whether a commit that got no answer finished after all
RUN_ATTEMPTS = 3
RETRY_DELAYS = (10.0, 30.0)  # seconds before the second and the third attempt at a run
PUT_ATTEMPTS = 3
PUT_DELAYS = (1.0, 5.0)
TRANSIENT = frozenset({408, 429, 502, 503, 504})  # answers that may not come again
CHUNK = 1024 * 1024
STOPS_PROJECT = (403, 404)  # the project itself is refused: no run of it can go


@dataclass
class PushReport:
    project: str
    local_runs: int = 0  # finished runs in the corpus here
    present: int = 0  # of those, runs the hub had already
    pushed: list[str] = field(default_factory=list)
    blobs: int = 0  # blobs uploaded
    errors: list[str] = field(default_factory=list)
    kept: dict[str, int] = field(default_factory=dict)  # runs not sent, by source the hub sink does not clear
    skipped: str | None = None  # why the project was left out, with --all
    build: dict | None = None  # the build the last pushed run queued

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_json(self) -> dict:
        return {
            "project": self.project,
            "ok": self.ok,
            "local_runs": self.local_runs,
            "present": self.present,
            "pushed": self.pushed,
            "blobs": self.blobs,
            "errors": self.errors,
            "kept": self.kept,
            "skipped": self.skipped,
            "build": self.build,
        }

    def summary_line(self) -> str:
        if self.skipped:
            return f"project {self.project}: skipped, {self.skipped}"
        line = (
            f"project {self.project}: pushed {len(self.pushed)} run(s) and {self.blobs} blob(s); "
            f"{self.present} of {self.local_runs} run(s) were on the hub already"
        )
        if self.kept:
            sources = ", ".join(sorted(self.kept))
            line += f"; {sum(self.kept.values())} run(s) of {sources} stay here, above what the hub sink clears"
        if self.build:
            line += f"; build {self.build.get('id')} {self.build.get('status')}"
        return line


def commit_timeout(uploads: int, size: int) -> float:
    """Seconds a commit of ``uploads`` uploads of ``size`` bytes in all may take before the push stops waiting."""
    return BASE_TIMEOUT + uploads * SECONDS_PER_UPLOAD + size / READ_RATE


def log_timeout(size: int) -> float:
    """Seconds the hub may take to check a run log of ``size`` bytes (POST .../runs)."""
    return BASE_TIMEOUT + size / LOG_READ_RATE


def transient(exc: HubError) -> bool:
    """Whether the failure may pass: sending the same request again later may succeed."""
    return isinstance(exc, Unreachable) or exc.status in TRANSIENT


def unanswered(exc: HubError) -> bool:
    """Whether the request may have been carried out although no answer came back."""
    return (isinstance(exc, Unreachable) and not exc.refused) or exc.status in (502, 504)


def batches(sizes: dict[str, int]) -> Iterator[list[str]]:
    """The hashes of ``sizes`` in order, in batches of at most COMMIT_BATCH and COMMIT_BYTES (a larger blob alone)."""
    batch: list[str] = []
    total = 0
    for digest, size in sizes.items():
        if batch and (len(batch) >= COMMIT_BATCH or total + size > COMMIT_BYTES):
            yield batch
            batch, total = [], 0
        batch.append(digest)
        total += size
    if batch:
        yield batch


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Pusher:
    def __init__(
        self,
        hub: Hub,
        project: Project,
        *,
        progress: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.hub = hub
        self.project = project
        self.name = project.name
        self.base = f"/v1/kg/{quote(project.name, safe='')}"
        self.progress = progress
        self.sleep = sleep
        self.clock = clock

    def _say(self, line: str) -> None:
        if self.progress is not None:
            self.progress(line)

    def _retrying(self, what: str, attempt: Callable[[bool], object]):
        """``attempt(again)`` until it succeeds, RUN_ATTEMPTS times at most while it fails with ``transient``;
        ``again`` is True from the second time on."""
        for number in range(1, RUN_ATTEMPTS + 1):
            try:
                return attempt(number > 1)
            except HubError as exc:
                if number == RUN_ATTEMPTS or not transient(exc):
                    raise
                delay = RETRY_DELAYS[min(number, len(RETRY_DELAYS)) - 1]
                self._say(f"{what}: {exc}; trying again in {delay:g}s (attempt {number + 1} of {RUN_ATTEMPTS})")
                self.sleep(delay)
        raise AssertionError("unreachable")

    def _put(self, url: str, path: Path) -> None:
        """PUT the file to a presigned URL, PUT_ATTEMPTS times at most while it fails with ``transient``."""
        for number in range(1, PUT_ATTEMPTS + 1):
            try:
                put_presigned(url, path)
                return
            except HubError as exc:
                if number == PUT_ATTEMPTS or not transient(exc):
                    raise
                self.sleep(PUT_DELAYS[min(number, len(PUT_DELAYS)) - 1])

    def _held_within(self, hashes: list[str], wait: float) -> bool:
        """Whether the project holds every blob of ``hashes`` within ``wait`` seconds, asking every SETTLE_INTERVAL:
        a commit the push stopped waiting for goes on on the hub."""
        deadline = self.clock() + wait
        while True:
            try:
                missing = self.hub.call("POST", f"{self.base}/blobs/check", {"sha256": hashes})["missing"]
            except HubError:
                return False
            if not missing:
                return True
            if self.clock() + SETTLE_INTERVAL > deadline:
                return False
            self.sleep(SETTLE_INTERVAL)

    def _commit(self, tickets: list[dict], sizes: dict[str, int]) -> None:
        hashes = [ticket["sha256"] for ticket in tickets]
        timeout = commit_timeout(len(tickets), sum(sizes[digest] for digest in hashes))
        body = {"project": self.name, "upload_ids": [ticket["upload_id"] for ticket in tickets]}
        try:
            self.hub.call("POST", "/v1/blobs/commit", body, timeout=timeout)
        except HubError as exc:
            if not unanswered(exc) or not self._held_within(hashes, timeout):
                raise

    def _upload_blobs(self, corpus, hashes: list[str], report: PushReport, label: str) -> None:
        """Upload and commit the blobs ``hashes`` from the corpus here, batch by batch; ``report.blobs`` counts those
        uploaded as each batch is committed."""
        paths = {}
        for digest in dict.fromkeys(hashes):
            path = corpus.blob_path(f"sha256:{digest}")
            if not path.is_file():
                raise HubError(f"blob {digest} is missing from the corpus at {corpus.root}")
            paths[digest] = path
        sizes = {digest: path.stat().st_size for digest, path in paths.items()}
        done = 0
        for batch in batches(sizes):
            items = [{"sha256": digest, "size": sizes[digest], "kind": "kg-blob"} for digest in batch]
            asked = self.hub.call("POST", "/v1/blobs/uploads", {"project": self.name, "items": items})
            tickets = asked["uploads"]
            if tickets:
                with ThreadPoolExecutor(max_workers=PARALLEL_PUTS, thread_name_prefix="kg-push") as pool:
                    list(pool.map(lambda ticket: self._put(ticket["url"], paths[ticket["sha256"]]), tickets))
                self._commit(tickets, sizes)
            report.blobs += len(tickets)
            done += len(batch)
            self._say(f"{label}: {done}/{len(sizes)} blob(s) on the hub")

    def push_run(
        self, corpus, path: Path, run_id: str, report: PushReport, label: str = "", again: bool = False
    ) -> None:
        """Push one run; ``again`` when an earlier attempt of this push failed on the way, so a run the hub has
        ingested meanwhile counts as pushed."""
        label = label or f"run {run_id}"
        digest, size = _sha256(path), path.stat().st_size
        asked = self.hub.call(
            "POST",
            "/v1/blobs/uploads",
            {"project": self.name, "items": [{"sha256": digest, "size": size, "kind": "kg-log"}]},
        )
        if asked["uploads"]:
            (ticket,) = asked["uploads"]
            self._put(ticket["url"], path)
            body = {"run_id": run_id, "log_upload_id": ticket["upload_id"]}
        else:  # the hub holds this log already: an earlier push stopped after sending it
            body = {"run_id": run_id, "log_sha256": digest}
        state = self.hub.call("POST", f"{self.base}/runs", body, timeout=log_timeout(size))
        if state["status"] == "ingested":
            if again:
                report.pushed.append(run_id)
            else:
                report.present += 1
            self._say(f"{label}: on the hub already")
            return
        if state["missing"]:
            self._say(f"{label}: {len(set(state['missing']))} blob(s) to send")
            self._upload_blobs(corpus, state["missing"], report, label)
        committed = self.hub.call("POST", f"{self.base}/runs/{run_id}/commit")
        report.pushed.append(run_id)
        if committed.get("build"):
            report.build = committed["build"]
        self._say(f"{label}: ingested")

    def _uncleared_source(self, path: Path) -> str | None:
        """The source of the run logged at ``path`` when the project's hub sink does not clear its label."""
        policy = self.project.policy
        hub = next((sink for sink in policy.sinks.values() if sink.kind == "hub"), None)
        header = log_header(path)
        source = header.get("source") if header else None
        if hub is None or not isinstance(source, str):
            return None  # the hub says what is wrong
        label = policy.source_label(source)
        return None if label.level <= hub.level and label.location <= hub.location else source

    def push(self) -> PushReport:
        report = PushReport(self.name)
        try:
            config = knowledge_config(self.project)
        except ProjectError as exc:
            report.errors.append(str(exc))
            return report
        corpus = self.project.corpus()
        try:
            logs = sorted((run_id, path) for path in corpus.log_dir.iterdir() if (run_id := run_id_of(path)))
            report.local_runs = len(logs)
            where = f"project {self.name}"
            self._retrying(where, lambda again: self.hub.call("PUT", f"{self.base}/config", config))
            held = set(self._retrying(where, lambda again: self.hub.call("GET", f"{self.base}/runs"))["ingested"])
            todo = []
            for run_id, path in logs:
                if run_id in held:
                    report.present += 1
                    continue
                source = self._uncleared_source(path)
                if source is not None:
                    report.kept[source] = report.kept.get(source, 0) + 1
                    continue
                todo.append((run_id, path))
            self._say(f"{where}: {len(todo)} run(s) to push, {report.present} of {len(logs)} on the hub already")
            for number, (run_id, path) in enumerate(todo, 1):
                label = f"[{number}/{len(todo)}] run {run_id}"
                try:
                    self._retrying(label, partial(self.push_run, corpus, path, run_id, report, label))
                except HubError as exc:
                    if exc.status == 401:
                        raise  # the credential: nothing can go
                    report.errors.append(f"run {run_id}: {exc}")
                    if exc.status is None or exc.status in STOPS_PROJECT or exc.status >= 500:
                        break
        except HubError as exc:
            report.errors.append(str(exc))
            if exc.status == 401:
                raise
        finally:
            corpus.close()
        return report


def push_project(hub: Hub, project: Project, progress: Callable[[str], None] | None = None) -> PushReport:
    return Pusher(hub, project, progress=progress).push()


def _pushable(project: dict) -> str | None:
    """Why a project the hub lists cannot take a push from the caller, None when it can."""
    if project.get("role") not in ("writer", "admin"):
        return "pushing needs the writer role on it"
    if not any(sink.get("kind") == "hub" for sink in project.get("sinks") or []):
        return "its knowledge.yaml declares no sink of kind hub"
    return None


def push_all(
    hub: Hub, projects: list[tuple[str, Project | Exception]], progress: Callable[[str], None] | None = None
) -> list[PushReport]:
    """Push each of ``projects`` (name, the project or why it did not load) that is on the hub and takes a push from
    the caller; the others come back skipped, saying why."""
    listed = {p["name"]: p for p in hub.call("GET", "/v1/projects")}
    reports = []
    for name, project in projects:
        if name not in listed:
            reports.append(PushReport(name, skipped="not on the hub, or not visible to you"))
        elif (reason := _pushable(listed[name])) is not None:
            reports.append(PushReport(name, skipped=reason))
        elif isinstance(project, Exception):
            reports.append(PushReport(name, errors=[f"the project does not load here: {project}"]))
        else:
            reports.append(push_project(hub, project, progress))
    return reports
