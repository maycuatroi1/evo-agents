"""An in-memory hub of the kg routes, member's and worker's, for the tests of ``kg sync`` and the worker's sync against
the hub (``evo_agents.hub.kg_hub_sync``): the runs it ingested are merged into a corpus as the hub's build merges them,
at once, so the state of a source it answers (GET .../sources/{s}/state) is that of a hub whose every run is built.
Its facts are those ``evo_agents.hub.kg_source_state`` gives: the newest run and the newest ok one, the cursor of the
newest run that sent one, the lineage of the highest (epoch, run id), the live items, and the held removals of each
scope of the newest ok run that listed it."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from evo_agents.hub.client import HubError
from evo_agents.hub.kg_ingest import blob_refs
from evo_agents.kg.corpus import Corpus


def records_of(path: Path) -> list[dict]:
    """The records of a run log the store holds under its hash, with no suffix to say it is gzip."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class KgHub:
    """The kg routes of the hub, member's and worker's, in memory: the runs it ingested are merged into ``corpus`` as
    the hub's build merges them. Presigned PUTs go through ``put`` (patched in for ``kg_push.put_presigned``), and the
    presigned GET of a log is a file URL of the store."""

    url = "https://hub.test"

    def __init__(self, root: Path, project: str = "demo"):
        self.project = project
        self.store = root / "store"
        self.store.mkdir(parents=True)
        self.corpus = Corpus(project, root / "hub-kg")
        self.blobs: dict[str, Path] = {}
        self.uploads: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.ingested: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.on_duty = True
        self.builds = 0
        self.unread = 0  # runs pushed before migration 0252 that no build has read: a build reads them
        self.max_items = 200_000  # MAX_ITEMS of the state
        self.inits = 0  # kg_init runs asked

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
            raise HubError(f"worker mac is not on duty for the night shift of project {self.project}", 403)
        base = f"/v1/worker/kg/{self.project}" if worker else f"/v1/kg/{self.project}"
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
        if found := re.fullmatch(rf"{re.escape(base)}/sources/([^/]+)/state", route):
            return self.state(found.group(1), query.get("scope", [None])[0], query.get("items") != ["false"])
        if (method, route) == ("POST", f"{base}/builds") and not worker:
            self.builds += 1
            self.unread = 0
            return {"queued": True, "build": {"id": self.builds, "status": "queued"}}
        if (method, route) == ("POST", f"{base}/init") and not worker:
            self.inits += 1
            return {"id": self.inits, "via": "cli", "state": "queued", "summary": "the Curator reads the profile"}
        if (found := re.fullmatch(rf"{re.escape(base)}/builds/([0-9]+)", route)) and not worker:
            return {"id": int(found.group(1)), "status": "succeeded", "error": None}
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
        records = records_of(path)
        end = next((r for r in records if r.get("type") == "run_end"), {})
        cursors = [r["cursor"] for r in records if r.get("type") == "state"]
        self.ingested[run_id] = {
            "source": run["source"],
            "log_sha256": run["log_sha256"],
            "log_size": path.stat().st_size,
            "status": end.get("status", "failed"),
            "finished_at": end.get("finished_at"),
            "cursor": cursors[-1] if cursors else None,
            "lineage": records[0].get("lineage"),
            "epoch": records[0].get("epoch"),
            "listed": end.get("listed"),
            "held": end.get("held"),
        }
        with self.corpus.transaction():
            self.corpus.merge(records_of(path), run_id)
        return {"run_id": run_id, "status": "ingested", "created": True, "build": None, "queued": False}

    def state(self, source: str, scope: str | None = None, items: bool = True) -> dict:
        """GET .../sources/{source}/state, as ``evo_agents.hub.kg_source_state.source_state`` answers it."""
        runs = sorted((run_id, run) for run_id, run in self.ingested.items() if run["source"] == source)

        def info(found):
            if not found:
                return None
            run_id, run = found[-1]
            return {"run_id": run_id, "status": run["status"], "started_at": None, "finished_at": run["finished_at"]}

        with_cursor = [(run_id, run) for run_id, run in runs if run["cursor"] is not None]
        lineages = sorted((run["epoch"], run_id, run["lineage"]) for run_id, run in runs if run["lineage"])
        held, listed = [], set()
        for run_id, run in reversed([(r, run) for r, run in runs if run["status"] == "ok"]):
            for name in sorted(set(run["listed"] or ()) - listed):
                held += [{"run_id": run_id, **h} for h in run["held"] or () if h["scope"] == name]
                listed.add(name)
        alive = sorted(
            (r for r in self.corpus.items(source) if not scope or r.item_id.startswith(scope)), key=lambda r: r.item_id
        )
        alive = alive if items else []
        return {
            "source": source,
            "runs": len(runs),
            "unread": self.unread,
            "last_run": info(runs),
            "last_ok": info([(r, run) for r, run in runs if run["status"] == "ok"]),
            "cursor": {"run_id": with_cursor[-1][0], "cursor": with_cursor[-1][1]["cursor"]} if with_cursor else None,
            "lineage": (
                {"run_id": lineages[-1][1], "lineage": lineages[-1][2], "epoch": lineages[-1][0]} if lineages else None
            ),
            "items": [
                {"id": r.item_id, "rev": r.rev, "rev_time": r.record.get("rev_time")} for r in alive[: self.max_items]
            ],
            "items_cut": len(alive) > self.max_items,
            "unbuilt": 0,
            "held": held,
        }

    def files(self) -> dict[str, str]:
        """``app:file:<path>`` -> rev of every file item the hub's merged state holds alive."""
        return {r.item_id: r.rev for r in self.corpus.items("app") if r.item_id.startswith("app:file:")}

    def pushes(self) -> int:
        return sum(1 for method, path in self.calls if method == "POST" and path.endswith("/runs"))

    def close(self) -> None:
        """Closes the corpus's SQLite connection: Windows keeps an open database's file locked."""
        self.corpus.close()


class KgHubs:
    """A hub of several projects, each a ``KgHub``, that lists them all (GET /v1/projects) as projects the caller
    pushes to: the writer role and a hub sink."""

    url = "https://hub.test"

    def __init__(self, root: Path, names: list[str]):
        self.hubs = {name: KgHub(root / name, name) for name in names}

    def put(self, url: str, path: Path) -> None:
        upload_id = url.removeprefix("fake://")
        next(hub for hub in self.hubs.values() if upload_id in hub.uploads).put(url, path)

    def call(self, method: str, path: str, body=None, *, timeout=None):
        if (method, path) == ("GET", "/v1/projects"):
            return [{"name": name, "role": "writer", "sinks": [{"id": "hub", "kind": "hub"}]} for name in self.hubs]
        parts = urlsplit(path).path.split("/")
        name = body["project"] if parts[2] == "blobs" else parts[4] if parts[2] == "worker" else parts[3]
        if name not in self.hubs:
            raise HubError(f"project {name} not found", 404)
        return self.hubs[name].call(method, path, body, timeout=timeout)

    def close(self) -> None:
        for hub in self.hubs.values():
            hub.close()
