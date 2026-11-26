"""The routes of a project's knowledge graph on the hub that the ``hub kg`` commands ask, one gateway each.

- :class:`KgResource` is the gateway to one collection under /v1/kg/{project}: the one place that quotes the project
  and the parts of a path. :class:`Builds`, :class:`Sources`, :class:`Uploads`, :class:`AnswerJobs` and
  :class:`InitRuns` are its collections, :class:`GraphHealth` the measures of the graph the Curator takes
  (/v1/projects/{project}/curator/kg-health).
- :class:`KgAdmin` holds the routes of a hub admin, which name no project in their path: prune, scrub and purge,
  kg_init of every project, and the shared KG pool.

A gateway also does what takes several calls: an archive sent in parts (:meth:`Uploads.send`), every page of a job's
answers (:meth:`AnswerJobs.answers`), a scrub page by page and its purge batch by batch (:meth:`KgAdmin.scrub`,
:meth:`KgAdmin.purge`). :func:`follow` reads a flow or a job again until it ends. A call is made with the arguments the
command made it with before (no body when it had none), so a fake hub of a test sees the same calls. Standard library
only, like the rest of the client.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from evo_agents.hub.client import Hub, HubError, put_presigned_part, transient
from evo_agents.hub.kg_hub_sync import wait_for
from evo_agents.hub.kg_push import PUT_ATTEMPTS, PUT_DELAYS
from evo_agents.hub.kg_push import transient as put_transient

PARALLEL = 4  # parts of an archive sent at once
URLS_AT_ONCE = 2 * PARALLEL  # parts whose URLs one call asks for: each works 15 minutes, so ask as they are needed
HASH_CHUNK = 4 * 1024 * 1024
PART_TIMEOUT = 600.0  # seconds for one part's PUT
ANSWER_PAGE = 1000  # lines of one read of a job's answers
ANSWER_LINE = ("id", "question", "answer", "evidence", "model", "input_tokens", "output_tokens")
PRUNE_TIMEOUT = 600.0  # seconds a prune may take: it deletes objects in the blob store
SCRUB_TIMEOUT = 600.0  # seconds one page of a scrub, or one purge, may take: it reads objects in the blob store
PURGE_BATCH = 500  # hashes in one purge request; the hub takes 1000
MEASURE_TIMEOUT = 600.0  # seconds a measure may take: the hub opens the graph and asks the questions twice


def asked(hub: Hub, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> Any:
    """``hub.call`` with the body and timeout only when there are any."""
    if body is None:
        return hub.call(method, path) if timeout is None else hub.call(method, path, timeout=timeout)
    return hub.call(method, path, body) if timeout is None else hub.call(method, path, body, timeout=timeout)


class KgResource:
    """One collection of a project's graph on the hub (Gateway): ``<prefix>/<project>/<collection>``."""

    prefix: ClassVar[str] = "/v1/kg"
    collection: ClassVar[str]

    def __init__(self, hub: Hub, project: str):
        self.hub = hub
        self.project = project

    @property
    def root(self) -> str:
        """``<prefix>/<project>``, the project quoted whole."""
        return f"{self.prefix}/{quote(self.project, safe='')}"

    def path(self, *parts: Any, **query: Any) -> str:
        """``<root>/<collection>/<part>...?<query>``, each part quoted whole; a query value of None is left out."""
        tail = "".join(f"/{quote(str(part), safe='')}" for part in parts)
        given = {name: value for name, value in query.items() if value is not None}
        return f"{self.root}/{self.collection}{tail}" + (f"?{urlencode(given)}" if given else "")

    def call(self, method: str, *parts: Any, body: dict | None = None, **query: Any) -> Any:
        return asked(self.hub, method, self.path(*parts, **query), body)

    def web(self, *parts: Any) -> str:
        """The page of the hub that shows the project's graph, or ``parts`` below it."""
        return f"{self.hub.url}/p/{quote(self.project, safe='')}/kg" + "".join(f"/{part}" for part in parts)


class Builds(KgResource):
    """GET and POST /v1/kg/{p}/builds: a project's builds and the jobs still queued; a build queued."""

    collection = "builds"

    def list(self, limit: int) -> dict:
        return self.call("GET", limit=limit)

    def queue(self) -> dict:
        return self.call("POST")

    def wait(self, build_id: int, timeout: float) -> dict:
        """The build once it has finished, read every few seconds for ``timeout`` seconds at most."""
        return wait_for(self.hub, self.root, build_id, timeout)


class Sources(KgResource):
    """DELETE /v1/kg/{p}/sources/{source}: every run of a source taken off the hub, a build queued without it."""

    collection = "sources"

    def forget(self, sources: list[str]) -> list[dict]:
        """Each of ``sources`` taken off in order: what the hub answered for each."""
        return [self.call("DELETE", source) for source in sources]


class GraphHealth(KgResource):
    """GET /v1/projects/{p}/curator/kg-health: the health of a project's graph over time, as the hub measures it."""

    prefix = "/v1/projects"
    collection = "curator/kg-health"

    def measures(self, limit: int) -> dict:
        return self.call("GET", limit=limit)


class InitRuns(KgResource):
    """/v1/kg/{p}/init: the kg_init runs of a project, in which the Curator configures its graph."""

    collection = "init"

    def list(self, limit: int) -> dict:
        return self.call("GET", limit=limit)

    def start(self) -> dict:
        """A kg_init run asked for, or the one in flight."""
        return self.call("POST", body={})

    def decide(self, run: int, decision: dict) -> dict:
        return self.call("POST", int(run), "decision", body={"decision": decision})

    def send_questions(self, run: int, questions: list) -> dict:
        return self.call("POST", int(run), "questions", body={"questions": questions})


class AnswerJobs(KgResource):
    """/v1/kg/{p}/answers: the jobs that ask a file of questions of the graph, and their answers."""

    collection = "answers"

    def ask(self, questions: list, batch: int) -> dict:
        return self.call("POST", body={"questions": questions, "batch": batch}) or {}

    def get(self, job_id: int) -> dict:
        return self.call("GET", job_id) or {}

    def list(self, limit: int) -> dict:
        return self.call("GET", limit=limit) or {}

    def stop(self, job_id: int) -> dict:
        return self.call("POST", job_id, "stop") or {}

    def reply(self, job_id: int, answers: list) -> dict:
        return self.call("POST", job_id, "replies", body={"answers": answers}) or {}

    def answers(self, job_id: int, ids: dict | None = None) -> list[dict]:
        """The job's answers in the file's order, as answers.jsonl holds them; ``ids`` gives back the ids of the file
        (an integer id stays one)."""
        out: list[dict] = []
        offset = 0
        while True:
            found = self.call("GET", job_id, "lines", offset=offset, limit=ANSWER_PAGE) or {}
            for item in found["lines"]:
                line = {key: item[key] for key in ANSWER_LINE}
                line["id"] = (ids or {}).get(item["id"], item["id"])
                if item.get("error"):
                    line["error"] = item["error"]
                out.append(line)
            offset += len(found["lines"])
            if not found["lines"] or offset >= found["total"]:
                return out


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def put_part(url: str, path: Path, offset: int, size: int) -> None:
    """PUT one part, PUT_ATTEMPTS times at most while it fails with ``kg_push.transient``."""
    for number in range(1, PUT_ATTEMPTS + 1):
        try:
            put_presigned_part(url, path, offset, size, PART_TIMEOUT)
            return
        except HubError as exc:
            if number == PUT_ATTEMPTS or not put_transient(exc):
                raise
            time.sleep(PUT_DELAYS[min(number - 1, len(PUT_DELAYS) - 1)])


class Uploads(KgResource):
    """/v1/kg/{p}/uploads: archives sent as sources of the graph, and the flow that follows each."""

    collection = "uploads"

    def list(self, limit: int) -> dict:
        return self.call("GET", limit=limit) or {}

    def pipeline(self, upload_id: int) -> dict:
        return self.call("GET", upload_id, "pipeline") or {}

    def send(
        self,
        path: Path,
        *,
        source: str | None = None,
        label: str | None = None,
        new_project: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[dict, int, int]:
        """Send the archive at ``path``, into the project the hub first creates with ``new_project``; (the hub's
        answer for the upload once joined, parts sent, parts kept)."""
        say = progress or (lambda line: None)
        ask: dict[str, Any] = {"name": path.name, "size": path.stat().st_size, "sha256": file_sha256(path)}
        ask |= {key: value for key, value in (("source", source), ("label", label)) if value}
        if new_project:
            ask["new_project"] = True
        started = self.call("POST", body=ask)
        upload_id, each = started["id"], started["part_size"]
        kept = set(started["done"])
        missing = [n for n in range(1, started["parts"] + 1) if n not in kept]
        if kept:
            say(f"upload {upload_id} resumed: {len(kept)} of {started['parts']} parts are on the hub already")
        with ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="kg-upload") as pool:
            for start in range(0, len(missing), URLS_AT_ONCE):
                batch = missing[start : start + URLS_AT_ONCE]
                parts = self.call("POST", upload_id, "parts", body={"numbers": batch})["parts"]
                futures = [pool.submit(put_part, p["url"], path, (p["number"] - 1) * each, p["size"]) for p in parts]
                for future in futures:
                    future.result()
                say(f"sent {start + len(batch)} of {len(missing)} parts")
        done = self.call("POST", upload_id, "complete", body={}) or {}
        return done, len(missing), len(kept)


class KgAdmin:
    """The routes of a hub admin under /v1/admin, which name no project in their path (Gateway)."""

    def __init__(self, hub: Hub):
        self.hub = hub

    def project_names(self) -> list[str]:
        """Every project on the hub."""
        return [project["name"] for project in self.hub.call("GET", "/v1/projects") or []]

    def prune(self, project: str | None, keep: int | None, dry_run: bool, graph_files: bool) -> dict:
        body = {"project": project, "keep": keep, "dry_run": dry_run, "graph_files": graph_files}
        return asked(self.hub, "POST", "/v1/admin/kg/prune", body, PRUNE_TIMEOUT)

    def init_all(self) -> dict:
        """A kg_init run asked for every project with a knowledge config."""
        return self.hub.call("POST", "/v1/admin/kg-init", {}) or {}

    def pool(self) -> dict:
        return self.hub.call("GET", "/v1/admin/kg-pool") or {}

    def set_pool(self, owner: str, pool: str) -> dict:
        return self.hub.call("PUT", "/v1/admin/kg-pool", {"owner": owner, "pool": pool}) or {}

    def clear_pool(self) -> dict:
        return self.hub.call("DELETE", "/v1/admin/kg-pool") or {}

    def scrub(self, project: str, progress: Callable[[str], None] | None = None) -> dict:
        """Every page of POST /v1/admin/kg/scrub for ``project``, added up; ``purged`` is None until a purge."""
        report: dict[str, Any] = {
            "project": project,
            "graph": None,
            "scanned": 0,
            "scanned_bytes": 0,
            "missing": 0,
            "found": [],
            "purged": None,
        }
        after = None
        while True:
            body = {"project": project, **({"after": after} if after else {})}
            page = asked(self.hub, "POST", "/v1/admin/kg/scrub", body, SCRUB_TIMEOUT) or {}
            for key in ("scanned", "scanned_bytes", "missing"):
                report[key] += page[key]
            report["found"] += page["found"]
            report["graph"] = report["graph"] or page.get("graph")
            if progress:
                progress(f"{project}: {report['scanned']} object(s) read, {len(report['found'])} hold a secret")
            after = page["next"]
            if not after:
                return report

    def purge(self, report: dict) -> None:
        """Ask the hub to delete the old objects ``report`` found, PURGE_BATCH at a time; what it answered goes into
        ``report["purged"]``."""
        old = [found["sha256"] for found in report["found"] if found["state"] == "old"]
        purged = {"purged": [], "kept": [], "builds": 0, "deleted": 0, "deleted_bytes": 0, "pending": 0}
        for start in range(0, len(old), PURGE_BATCH):
            body = {"project": report["project"], "sha256": old[start : start + PURGE_BATCH]}
            answer = asked(self.hub, "POST", "/v1/admin/kg/scrub/purge", body, SCRUB_TIMEOUT)
            for key in ("purged", "kept", "builds", "deleted", "deleted_bytes"):
                purged[key] += answer[key]
            purged["pending"] = answer["pending"]
        report["purged"] = purged


def follow(
    read: Callable[[], dict],
    line_of: Callable[[dict], str],
    ended: Callable[[dict], bool],
    say: Callable[[str], Any],
    *,
    what: str,
    poll: float,
    sleep: Callable[[float], Any] | None = None,
) -> dict:
    """``read`` again every ``poll`` seconds until ``ended``, calling ``say`` with each ``line_of`` that changed; the
    end. A read the hub did not answer (``client.transient``: a redeploy, a proxy's 502) is read again, not an end;
    ``what`` names what is read in the line that says so."""
    last = None
    while True:
        try:
            found = read()
        except HubError as exc:
            if not transient(exc):
                raise
            line = f"the hub did not answer ({exc}); reading {what} again in a moment"
            if line != last:
                say(line)
                last = line
            (sleep or time.sleep)(poll)
            continue
        line = line_of(found)
        if line != last:
            say(line)
            last = line
        if ended(found):
            return found
        (sleep or time.sleep)(poll)
