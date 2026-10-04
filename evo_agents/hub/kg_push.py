"""``evo-agents hub kg push`` and ``kg sync --push``: send the runs of a project's corpus that the hub lacks.

For a project: send its knowledge config (``evo_agents.hub.kg_ingest.knowledge_config``), ask which runs the hub has,
then push every finished run here that it has not ingested, oldest first, in the three steps the hub takes
(``evo_agents.hub.server.kg``): upload the log through a presigned PUT, have the hub check it, upload and commit the
blobs it says it lacks, commit the run. A log the hub holds already from an attempt that stopped half way is named by
its hash instead of being uploaded again. Blobs go in batches of UPLOAD_BATCH, PARALLEL_PUTS at a time.

A run of a source whose own label the project's hub sink does not clear never leaves the machine: it is counted as
kept here, not sent to be refused. A run the hub refuses (an item raised above the hub sink, a blob over the size
limit) is reported and the next run goes on. A hub that cannot be reached or refuses the project stops that project;
a credential it refuses stops everything. ``push_all`` pushes every project of this machine's projects.json that the
caller may push to on the hub and says why it skipped the others.

Standard library only, like the rest of the client.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.client import Hub, HubError, put_presigned
from evo_agents.hub.kg_ingest import knowledge_config, log_header, run_id_of
from evo_agents.kg.project import Project, ProjectError

UPLOAD_BATCH = 1000  # items in one request of the blob routes
PARALLEL_PUTS = 8
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Pusher:
    def __init__(self, hub: Hub, project: Project):
        self.hub = hub
        self.project = project
        self.name = project.name
        self.base = f"/v1/kg/{quote(project.name, safe='')}"

    def _upload_blobs(self, corpus, hashes: list[str]) -> int:
        """Upload and commit the blobs ``hashes`` from the corpus here; how many were uploaded."""
        uploaded = 0
        for start in range(0, len(hashes), UPLOAD_BATCH):
            paths = {}
            for digest in hashes[start : start + UPLOAD_BATCH]:
                path = corpus.blob_path(f"sha256:{digest}")
                if not path.is_file():
                    raise HubError(f"blob {digest} is missing from the corpus at {corpus.root}")
                paths[digest] = path
            items = [
                {"sha256": digest, "size": path.stat().st_size, "kind": "kg-blob"} for digest, path in paths.items()
            ]
            asked = self.hub.call("POST", "/v1/blobs/uploads", {"project": self.name, "items": items})
            tickets = asked["uploads"]
            sends = [(ticket["url"], paths[ticket["sha256"]]) for ticket in tickets]
            with ThreadPoolExecutor(max_workers=PARALLEL_PUTS, thread_name_prefix="kg-push") as pool:
                list(pool.map(lambda send: put_presigned(*send), sends))
            if tickets:
                upload_ids = [ticket["upload_id"] for ticket in tickets]
                self.hub.call("POST", "/v1/blobs/commit", {"project": self.name, "upload_ids": upload_ids})
            uploaded += len(tickets)
        return uploaded

    def push_run(self, corpus, path: Path, run_id: str, report: PushReport) -> None:
        digest, size = _sha256(path), path.stat().st_size
        asked = self.hub.call(
            "POST",
            "/v1/blobs/uploads",
            {"project": self.name, "items": [{"sha256": digest, "size": size, "kind": "kg-log"}]},
        )
        if asked["uploads"]:
            (ticket,) = asked["uploads"]
            put_presigned(ticket["url"], path)
            body = {"run_id": run_id, "log_upload_id": ticket["upload_id"]}
        else:  # the hub holds this log already: an earlier push stopped after sending it
            body = {"run_id": run_id, "log_sha256": digest}
        state = self.hub.call("POST", f"{self.base}/runs", body)
        if state["status"] == "ingested":
            report.present += 1
            return
        report.blobs += self._upload_blobs(corpus, state["missing"])
        committed = self.hub.call("POST", f"{self.base}/runs/{run_id}/commit")
        report.pushed.append(run_id)
        if committed.get("build"):
            report.build = committed["build"]

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
            self.hub.call("PUT", f"{self.base}/config", config)
            held = set(self.hub.call("GET", f"{self.base}/runs")["ingested"])
            for run_id, path in logs:
                if run_id in held:
                    report.present += 1
                    continue
                source = self._uncleared_source(path)
                if source is not None:
                    report.kept[source] = report.kept.get(source, 0) + 1
                    continue
                try:
                    self.push_run(corpus, path, run_id, report)
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


def push_project(hub: Hub, project: Project) -> PushReport:
    return Pusher(hub, project).push()


def _pushable(project: dict) -> str | None:
    """Why a project the hub lists cannot take a push from the caller, None when it can."""
    if project.get("role") not in ("writer", "admin"):
        return "pushing needs the writer role on it"
    if not any(sink.get("kind") == "hub" for sink in project.get("sinks") or []):
        return "its knowledge.yaml declares no sink of kind hub"
    return None


def push_all(hub: Hub, projects: list[tuple[str, Project | Exception]]) -> list[PushReport]:
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
            reports.append(push_project(hub, project))
    return reports
