"""The worker on duty keeps a project's graph fresh (``evo_agents.worker.kg_sync``), in this process against an
in-memory hub of the kg routes whose merged state is a corpus, as the hub's build merges the runs it ingested: real git
repos, a member's laptop syncing and pushing through the member routes, the worker through its own.

Checked: the worker reads knowledge.yaml at the harness's origin, syncs only the git sources whose ref is
origin/<branch>, fetching first, and names the others skipped; it pushes nothing when nothing changed; laptop and
worker pushing one source in turn, the laptop once from a stale clone, leave the hub with the files of the newest
commit and nothing of an older one, once the worker looked after the laptop's push; a sync that fails, a fetch that
fails, a refused hub and a missing checkout show in the report the heartbeat carries, which the hub's model takes; the
daemon says the feature, looks at a project the heartbeat answer names, sends what it found, and does neither with
EVO_WORKER_KG_SYNC=off."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from evo_agents.hub import kg_push
from evo_agents.hub.client import HubError
from evo_agents.hub.kg_ingest import blob_refs
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.project import load_project_at
from evo_agents.kg.sync import sync_source
from evo_agents.worker import kg_sync
from evo_agents.worker.kg_sync import KgSync

PROJECT = "demo"
ORIGIN = "https://github.com/acme/app.git"
T = ["2026-10-01T00:00:00+00:00", "2026-10-01T01:00:00+00:00", "2026-10-01T02:00:00+00:00", "2026-10-01T03:00:00+00:00"]
KNOWLEDGE = """version: 1
project: demo
policy:
  levels: [public, internal]
  sinks:
    - id: hub
      kind: hub
      clearance: {level: internal}
sources:
  - id: harness
    connector: harness
    label: {level: internal}
  - id: app
    connector: git
    repo: app
    ref: origin/main
    fetch: true
    pulls: false
    label: {level: internal}
  - id: app-head
    connector: git
    repo: app
    label: {level: internal}
  - id: app-tag
    connector: git
    repo: app
    ref: v1
    label: {level: internal}
  - id: other
    connector: git
    repo: other
    ref: origin/main
    label: {level: internal}
"""
HARNESS = f"""name: demo
knowledge_file: knowledge.yaml
hub:
  project: demo
repos:
  - name: app
    path: app
    origin: {ORIGIN}
    default_branch: main
  - name: other
    path: other
"""


def git(repo: Path, *args: str, when: str = T[0]) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    return done.stdout


class Upstream:
    """A repo on 'origin': a bare repository, and the seed it is pushed from."""

    def __init__(self, tmp: Path, name: str, files: dict[str, str]):
        self.origin = tmp / "origins" / f"{name}.git"
        self.seed = tmp / "seeds" / name
        self.seed.mkdir(parents=True)
        self.published = False
        git(self.seed, "init", "-q", "-b", "main")
        self.commit(files, T[0])
        self.origin.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(self.seed), str(self.origin)], check=True)
        git(self.seed, "remote", "add", "origin", str(self.origin))
        self.published = True

    def commit(self, changes: dict[str, str | None], when: str) -> None:
        for path, text in changes.items():
            if text is None:
                (self.seed / path).unlink()
            else:
                (self.seed / path).parent.mkdir(parents=True, exist_ok=True)
                (self.seed / path).write_text(text, encoding="utf-8")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", f"at {when}", when=when)
        if self.published:
            git(self.seed, "push", "-q", "origin", "main")

    def clone(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(dest)], check=True)
        return dest

    def files(self) -> dict[str, str]:
        """``app:file:<path>`` -> the blob sha of every file at the head of main."""
        found = {}
        for line in git(self.seed, "ls-tree", "-r", "HEAD").splitlines():
            meta, path = line.split("\t", 1)
            found[f"app:file:{path}"] = meta.split()[2]
        return found


def records_of(path: Path) -> list[dict]:
    """The records of a run log the store holds under its hash, with no suffix to say it is gzip."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class KgHub:
    """The kg routes of the hub, member's and worker's, in memory: the runs it ingested are merged into ``corpus`` as
    the hub's build merges them. Presigned PUTs go through ``put`` (patched in for ``kg_push.put_presigned``), and the
    presigned GET of a log is a file URL of the store."""

    url = "https://hub.test"

    def __init__(self, root: Path):
        self.store = root / "store"
        self.store.mkdir(parents=True)
        self.corpus = Corpus(PROJECT, root / "hub-kg")
        self.blobs: dict[str, Path] = {}
        self.uploads: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.ingested: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.on_duty = True

    def put(self, url: str, path: Path) -> None:
        upload_id = url.removeprefix("fake://")
        target = self.store / f"upload-{upload_id}"
        shutil.copy(path, target)
        self.uploads[upload_id]["path"] = target

    def keep(self, path: Path) -> str:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        target = self.store / digest
        if not target.exists():
            shutil.copy(path, target)
        self.blobs[digest] = target
        return digest

    def call(self, method: str, path: str, body=None, *, timeout=None):
        self.calls.append((method, path))
        parts = urlsplit(path)
        route, query = parts.path, parse_qs(parts.query)
        worker = route.startswith("/v1/worker/kg/")
        if worker and not self.on_duty:
            raise HubError(f"worker mac is not on duty for the night shift of project {PROJECT}", 403)
        base = f"/v1/worker/kg/{PROJECT}" if worker else f"/v1/kg/{PROJECT}"
        blobs = f"{base}/blobs" if worker else "/v1/blobs"
        if (method, route) == ("PUT", f"{base}/config") and not worker:
            return {"digest": "sha256:" + "0" * 64, "changed": False}
        if (method, route) == ("GET", f"{base}/runs"):
            source = query.get("source", [None])[0]
            ids = sorted(r for r, run in self.ingested.items() if source in (None, run["source"]))
            return {"ingested": ids, "pending": sorted(self.pending)}
        if (method, route) == ("POST", f"{base}/runs"):
            return self._push(body)
        if found := re.fullmatch(rf"{re.escape(base)}/runs/([0-9a-f-]+)/commit", route):
            return self._commit(found.group(1))
        if (found := re.fullmatch(rf"{re.escape(base)}/runs/([0-9a-f-]+)/log", route)) and worker:
            run = self.ingested[found.group(1)]
            url = self.blobs[run["log_sha256"]].as_uri()
            return {"run_id": found.group(1), **run, "url": url}
        if route == f"{base}/blobs/check":
            return {"missing": [digest for digest in body["sha256"] if digest not in self.blobs]}
        if route == f"{blobs}/uploads":
            tickets = []
            for item in body["items"]:
                if item["sha256"] not in self.blobs:
                    upload_id = str(uuid.uuid4())
                    self.uploads[upload_id] = {**item, "path": None}
                    tickets.append({"sha256": item["sha256"], "upload_id": upload_id, "url": f"fake://{upload_id}"})
            return {"uploads": tickets, "present": [], "expires_at": "2026-10-02T00:00:00Z"}
        if route == f"{blobs}/commit":
            for upload_id in body["upload_ids"]:
                self.keep(self.uploads.pop(upload_id)["path"])
            return {"blobs": [], "added": len(body["upload_ids"])}
        raise HubError(f"no route {method} {route}", 404)

    def _push(self, body: dict) -> dict:
        run_id = body["run_id"]
        if run_id in self.ingested:
            return {"run_id": run_id, "status": "ingested", "log_sha256": None, "blobs": 0, "missing": []}
        if body.get("log_upload_id"):
            digest = self.keep(self.uploads.pop(body["log_upload_id"])["path"])
        else:
            digest = body["log_sha256"]
        records = records_of(self.blobs[digest])
        refs = sorted({ref for r in records if r.get("type") == "item" for ref in blob_refs(r)})
        self.pending[run_id] = {"source": records[0]["source"], "log_sha256": digest, "refs": refs}
        missing = [ref for ref in refs if ref not in self.blobs]
        return {"run_id": run_id, "status": "pending", "log_sha256": digest, "blobs": len(refs), "missing": missing}

    def _commit(self, run_id: str) -> dict:
        run = self.pending.pop(run_id)
        assert all(ref in self.blobs for ref in run["refs"]), "committed before its blobs"
        path = self.blobs[run["log_sha256"]]
        self.ingested[run_id] = {
            "source": run["source"],
            "log_sha256": run["log_sha256"],
            "log_size": path.stat().st_size,
        }
        with self.corpus.db:
            self.corpus.merge(records_of(path), run_id)
        return {"run_id": run_id, "status": "ingested", "created": True, "build": None, "queued": False}

    def files(self) -> dict[str, str]:
        """``app:file:<path>`` -> rev of every file item the hub's merged state holds alive."""
        return {r.item_id: r.rev for r in self.corpus.items("app") if r.item_id.startswith("app:file:")}

    def pushes(self) -> int:
        return sum(1 for method, path in self.calls if method == "POST" and path.endswith("/runs"))


class Stage:
    """The app's origin and the harness's, a laptop with its clones, the worker with its checkouts, and the hub."""

    def __init__(self, tmp: Path, monkeypatch):
        self.app = Upstream(tmp, "app", {"a.md": "# A\n\none\n", "b.md": "# B\n\none\n", "c.md": "# C\n\none\n"})
        self.harness = Upstream(tmp, "harness", {"harness.yaml": HARNESS, "knowledge.yaml": KNOWLEDGE})
        self.laptop_app = self.app.clone(tmp / "laptop" / "app")
        self.laptop_harness = self.harness.clone(tmp / "laptop" / "harness")
        self.checkouts = {
            "app": self.app.clone(tmp / "worker" / "ws" / "app"),
            "harness": self.harness.clone(tmp / "worker" / "ws" / "harness"),
        }
        self.hub = KgHub(tmp / "hub")
        monkeypatch.setattr(kg_push, "put_presigned", self.hub.put)
        self.root = tmp / "worker" / "home"
        self.sync = KgSync(self.root, self.hub, secrets=("evw_secret",))
        self.laptop = load_project_at(self.laptop_harness, tmp / "laptop" / "kg-home")

    def laptop_push(self, *, force: bool = False):
        result = sync_source(self.laptop, "app", force=force)
        report = kg_push.Pusher(self.hub, self.laptop).push()
        assert report.ok, report.errors
        return result

    def look(self) -> kg_sync.ProjectState:
        return self.sync.look(PROJECT, self.checkouts)


@pytest.fixture
def stage(tmp_path, monkeypatch):
    return Stage(tmp_path, monkeypatch)


def test_kg_sync_reads_the_ref_sources_from_origins_harness_and_pushes_nothing_new_twice(stage):
    state = stage.look()
    assert state.error is None
    statuses = {sid: (found.status, found.error) for sid, found in state.sources.items()}
    assert statuses["app"] == ("ok", None)
    assert statuses["app-head"][0] == "skipped" and "reads the checkout's HEAD" in statuses["app-head"][1]
    assert statuses["app-tag"][0] == "skipped" and "is not origin/<branch>" in statuses["app-tag"][1]
    assert statuses["other"][0] == "failed" and "no checkout of repo other" in statuses["other"][1]
    assert "harness" not in statuses, "a source of another connector is a member's machine's alone"
    app = state.sources["app"]
    assert app.commit == git(stage.app.seed, "rev-parse", "HEAD").strip() and app.behind == 0 and app.pushed == 1
    assert stage.hub.files() == stage.app.files()
    view = kg_sync.kg_dir(stage.root) / "harness" / PROJECT
    assert (view / "knowledge.yaml").read_text(encoding="utf-8") == KNOWLEDGE, "knowledge.yaml byte for byte"
    assert "path: " + str(stage.checkouts["app"]) in (view / "harness.yaml").read_text(encoding="utf-8")

    pushes = stage.hub.pushes()
    again = stage.look()
    assert again.sources["app"].status == "ok" and again.sources["app"].pushed == 0
    assert stage.hub.pushes() == pushes, "nothing changed: nothing is pushed"
    assert not any(method == "PUT" for method, _ in stage.hub.calls), "the worker never sends the knowledge config"


def test_kg_sync_reads_knowledge_yaml_at_origin_not_the_checkouts_working_tree(stage):
    (stage.checkouts["harness"] / "knowledge.yaml").write_text(KNOWLEDGE.replace("origin/main", "HEAD"), "utf-8")
    assert stage.look().sources["app"].status == "ok"
    stage.harness.commit({"knowledge.yaml": KNOWLEDGE.replace("    ref: origin/main\n    fetch: true\n", "")}, T[1])
    state = stage.look()
    assert state.error is None, state.error
    assert state.sources["app"].status == "skipped", "the harness's origin moved: its knowledge.yaml counts"


def test_kg_sync_interleaved_pushes_of_laptop_and_worker_leave_the_newest_commit_alone(stage):
    stage.laptop_push()  # C1 from the laptop
    assert stage.hub.files() == stage.app.files()

    stage.app.commit({"a.md": "# A\n\ntwo\n", "b.md": None, "d.md": "# D\n\none\n"}, T[1])  # C2
    state = stage.look()
    assert state.sources["app"].pulled == 1, "the laptop's run, merged before the worker read"
    assert stage.hub.files() == stage.app.files(), "b, which only the laptop's run named, is gone from the hub too"
    c2 = stage.app.files()

    git(stage.laptop_app, "remote", "set-url", "origin", str(stage.app.origin) + "-gone")  # the laptop goes offline
    stale = stage.laptop_push(force=True)  # it reads its clone's origin/main, still C1, and pushes it
    assert any("fetch failed" in warning for warning in stale.warnings)
    assert "app:file:b.md" in stage.hub.files(), "the stale run brings b back for now"
    state = stage.look()
    assert state.sources["app"].pulled == 1 and state.sources["app"].pushed == 1
    assert stage.hub.files() == c2, "the worker read C2 again: the hub holds C2 and nothing of C1"

    git(stage.laptop_app, "remote", "set-url", "origin", str(stage.app.origin))  # back online
    stage.app.commit({"c.md": "# C\n\ntwo\n", "d.md": None}, T[2])  # C3 drops d, which only the worker's runs named
    stage.laptop_push()
    assert "app:file:d.md" in stage.hub.files(), "the laptop never knew d: its run leaves it"
    stage.look()
    assert stage.hub.files() == stage.app.files(), "the worker's look after the laptop's push removes d"
    commit_items = {r.item_id for r in stage.hub.corpus.items("app") if r.item_id.startswith("app:commit:")}
    assert len(commit_items) == 3


def test_kg_sync_report_names_a_failed_fetch_a_refused_hub_and_a_missing_harness(stage):
    pytest.importorskip("fastapi", reason="the hub's model of the report needs the hub-server extra")
    from evo_agents.hub.server.runs import models

    stage.look()
    git(stage.checkouts["app"], "remote", "set-url", "origin", str(stage.app.origin) + "-gone")
    stage.app.commit({"a.md": "# A\n\nthree\n"}, T[3])
    state = stage.look()
    assert state.sources["app"].status == "failed" and state.sources["app"].error.startswith("fetch failed")

    stage.hub.on_duty = False
    state = stage.look()
    assert state.sources["app"].status == "failed"
    assert "merging the hub's runs" in state.sources["app"].error and "not on duty" in state.sources["app"].error

    missing = stage.sync.look("other-project", stage.checkouts)
    assert missing.error.startswith("no checkout of the harness of project other-project")
    report = stage.sync.report([PROJECT, "other-project", "never-looked-at"])
    assert [entry["project"] for entry in report["projects"]] == [PROJECT, "other-project"]
    checked = models.KgSyncReport.model_validate(report)
    assert {source.status for source in checked.projects[0].sources} == {"failed", "skipped"}
    assert "evw_secret" not in json.dumps(report)
    saved = kg_sync.shown_state(stage.root)
    assert saved == report["projects"], "state.json holds what the heartbeat sends"
    lines = kg_sync.status_lines(saved)
    assert lines[0].startswith(f"graph of {PROJECT}: looked at ") and any("source app: failed" in x for x in lines)


def test_kg_sync_errors_are_cut_and_masked():
    text = "fetch failed: https://x-access-token:ghp_" + "a" * 36 + "@github.com/acme/app.git " + "word " * 200
    shown = kg_sync.cut(text)
    assert len(shown) <= 500 and "ghp_" not in shown and shown.endswith("...")


# The daemon

aiohttp = pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")
from tests.worker.test_plan_runs import machine  # noqa: E402, F401 (a fixture)


def test_the_daemon_says_kg_sync_looks_at_the_projects_of_the_answer_and_reports_them(machine, monkeypatch):  # noqa: F811
    import asyncio

    from tests.worker.test_plan_runs import wait_for, with_daemon

    looked: list[tuple[str, list[str]]] = []

    def look(self, project, checkouts):
        looked.append((project, sorted(checkouts)))
        state = self.state.setdefault(project, kg_sync.ProjectState(project))
        state.checked_at = "2026-10-19T00:00:00+00:00"
        state.sources["app"] = kg_sync.SourceState("app", status="failed", error="fetch failed: offline")
        self.reports[project] = state.report()
        return state

    monkeypatch.setattr(KgSync, "look", look)

    async def body(hub, daemon):
        assert "kg_sync" in hub.last_heartbeat["features"] and "kg_sync" not in hub.last_heartbeat
        hub.kg_duties = [{"project": "demo", "received_at": None}]
        await wait_for(lambda: looked, "a look at the project the answer names")
        assert looked[0] == ("demo", ["alpha", "beta"])
        await wait_for(lambda: "kg_sync" in hub.last_heartbeat, "a heartbeat with what the look found")
        (entry,) = hub.last_heartbeat["kg_sync"]["projects"]
        assert entry["project"] == "demo" and entry["sources"][0]["error"] == "fetch failed: offline"
        hub.kg_duties = [{"project": "demo", "received_at": "2026-10-19T01:00:00+00:00"}]
        await wait_for(lambda: len(looked) == 2, "a look once the hub took a run of the project")
        await asyncio.sleep(0.5)
        assert len(looked) == 2, "the next look waits for kg_every"

    with_daemon(machine, body)


def test_the_daemon_with_kg_sync_off_neither_says_it_nor_looks(machine, monkeypatch):  # noqa: F811
    import asyncio

    from tests.worker.test_plan_runs import with_daemon

    monkeypatch.setattr(KgSync, "look", lambda *args: pytest.fail("looked with the sync off"))
    machine.env[kg_sync.ENABLED_VARIABLE] = "off"

    async def body(hub, daemon):
        hub.kg_duties = [{"project": "demo", "received_at": None}]
        await asyncio.sleep(1.0)
        assert "kg_sync" not in hub.last_heartbeat["features"] and "kg_sync" not in hub.last_heartbeat

    with_daemon(machine, body)
