"""The graphs of the bench: one per repo and base commit, built in a kg home of the bench's own, never the hub's.

Layout under the bench cache::

    repos/OWNER__NAME.git              a bare clone of each repo, fetched once, every base commit read from it
    kg/EXTRACTOR/home/                 EVO_KG_HOME of the builds: a project per repo and commit, one shared memo
    kg/EXTRACTOR/harness/NAME/         the harness of that project: a git source at the base commit, no pulls
    kg/EXTRACTOR/builds.jsonl          the time each build took, which ``estimate`` reads

A snapshot is the commit read straight from the bare clone (the git connector reads a ref, not a working tree), with
its history up to it as any graph has; the pull requests are not read, since one of them is the fix. EXTRACTOR is a
digest of what makes the graph (``extractor``): the store's schema, the version of each stage, the graphify used, the
code of the git connector and the text the search index is made of; not evo-agents' version, so a release that
changes only the ranking finds its graphs built and searches them again. A build that succeeded leaves ``bench.json`` in its project and is not done again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from evo_agents.kg.bench.datasets import BenchError
from evo_agents.kg.bench.tasks import SOURCE_ID, Task
from evo_agents.kg.project import Project, load_project_at

GITHUB_URL = "https://github.com/{repo}.git"
GIT_TIMEOUT = 3600.0
MARKER = "bench.json"
SLUG = re.compile(r"[^a-z0-9._-]+")


def extractor() -> str:
    """Digest of what a bench graph is made with; a change of it builds the graphs again (see the module)."""
    from evo_agents.kg import store, text
    from evo_agents.kg.connectors import _git, git
    from evo_agents.kg.extract import graphify as gfy
    from evo_agents.kg.pipeline import curation
    from evo_agents.kg.pipeline.binding import BINDING_VERSION
    from evo_agents.kg.pipeline.link import LINK_VERSION
    from evo_agents.kg.pipeline.stages import MAP_VERSION, STRUCTURE_VERSION

    parts = [
        store.SCHEMA_VERSION,
        MAP_VERSION,
        STRUCTURE_VERSION,
        LINK_VERSION,
        BINDING_VERSION,
        curation.CURATION_VERSION,
        gfy.version() if gfy.available() else None,
        *(hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in (text, git, _git)),
    ]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


def slug(repo: str) -> str:
    return SLUG.sub("-", repo.strip().lower().replace("/", "__")).strip("-")


@dataclass(frozen=True)
class Layout:
    cache: Path
    extractor: str

    @property
    def base(self) -> Path:
        return self.cache / "kg" / self.extractor

    @property
    def home(self) -> Path:
        return self.base / "home"

    def clone(self, repo: str) -> Path:
        return self.cache / "repos" / f"{slug(repo)}.git"

    def name(self, task: Task) -> str:
        return f"{slug(task.repo)}-{task.commit[:12].lower()}"

    def harness(self, task: Task) -> Path:
        return self.base / "harness" / self.name(task)

    def project_dir(self, task: Task) -> Path:
        return self.home / self.name(task)

    def built(self, task: Task) -> bool:
        root = self.project_dir(task)
        return (root / MARKER).is_file() and (root / "graph.sqlite").is_file()


def layout(cache: Path) -> Layout:
    return Layout(cache, extractor())


@contextlib.contextmanager
def bench_home(home: Path):
    """EVO_KG_HOME set to the bench's kg home while a build or a run reads it, then put back."""
    before = os.environ.get("EVO_KG_HOME")
    os.environ["EVO_KG_HOME"] = str(home)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("EVO_KG_HOME", None)
        else:
            os.environ["EVO_KG_HOME"] = before


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    command = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise BenchError(f"git {' '.join(args[:2])} took over {GIT_TIMEOUT:.0f}s") from None


def ensure_commit(lay: Layout, task: Task, git_url: str = GITHUB_URL) -> Path:
    """The bare clone of the task's repo, holding its base commit: cloned or fetched as needed."""
    clone = lay.clone(task.repo)
    if not clone.is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        url = git_url.format(repo=task.repo)
        out = _git("clone", "--bare", "--quiet", url, str(clone))
        if out.returncode != 0:
            raise BenchError(f"cannot clone {url}: {out.stderr.strip()[:300]}")
    if _git("cat-file", "-e", f"{task.commit}^{{commit}}", cwd=clone).returncode != 0:
        out = _git("fetch", "--quiet", "origin", task.commit, cwd=clone)
        if out.returncode != 0 or _git("cat-file", "-e", f"{task.commit}^{{commit}}", cwd=clone).returncode != 0:
            raise BenchError(f"{task.repo} has no commit {task.commit[:12]}: {out.stderr.strip()[:200]}")
    return clone


def write_harness(lay: Layout, task: Task, clone: Path) -> Path:
    """The harness of the task's graph: one git source at the base commit, labelled public, memo shared."""
    root = lay.harness(task)
    root.mkdir(parents=True, exist_ok=True)
    name = lay.name(task)
    (root / "harness.yaml").write_text(f"name: {name}\nrepos: []\nknowledge_file: knowledge.yaml\n", encoding="utf-8")
    knowledge = {
        "version": 1,
        "project": name,
        "policy": {
            "levels": ["public"],
            "memo": "shared",
            "sinks": [{"id": "bench", "kind": "agent-session", "clearance": {"level": "public"}}],
        },
        "sources": [
            {
                "id": SOURCE_ID,
                "connector": "git",
                "path": str(clone),
                "ref": task.commit,
                "pulls": False,
                "label": {"level": "public", "integrity": "U"},
            }
        ],
    }
    # JSON is YAML: knowledge.yaml without a YAML writer
    (root / "knowledge.yaml").write_text(json.dumps(knowledge, indent=1) + "\n", encoding="utf-8")
    return root


def project_of(lay: Layout, task: Task) -> Project:
    return load_project_at(lay.harness(task), lay.home)


@dataclass
class Built:
    task: Task
    reused: bool
    seconds: float = 0.0
    error: str | None = None


def build_graph(lay: Layout, task: Task, *, git_url: str = GITHUB_URL) -> Built:
    """The graph of the task's repo at its base commit: reused when built with this extractor, else synced and built."""
    if lay.built(task):
        return Built(task, reused=True)
    from evo_agents.kg.build import build_project
    from evo_agents.kg.sync import sync_project

    start = time.monotonic()
    try:
        clone = ensure_commit(lay, task, git_url)
        write_harness(lay, task, clone)
        with bench_home(lay.home):
            project = project_of(lay, task)
            runs = sync_project(project)
            failed = [r for r in runs if not r.ok]
            if failed:
                why = failed[0].exception or "; ".join(failed[0].issues[:2]) or failed[0].status
                return Built(task, False, time.monotonic() - start, f"sync: {why}")
            report = build_project(project)
            if not report.ok:
                return Built(task, False, time.monotonic() - start, f"build: {'; '.join(report.errors[:2])}")
    except BenchError as exc:
        return Built(task, False, time.monotonic() - start, str(exc))
    seconds = round(time.monotonic() - start, 1)
    marker = {"repo": task.repo, "commit": task.commit, "extractor": lay.extractor, "seconds": seconds}
    (lay.project_dir(task) / MARKER).write_text(json.dumps(marker) + "\n", encoding="utf-8")
    with (lay.base / "builds.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({**marker, "bench": task.bench}) + "\n")
    return Built(task, False, seconds)


def build_seconds(lay: Layout, bench: str) -> float | None:
    """The mean time a build of ``bench`` took with this extractor (any set's when it has none yet); None before any."""
    try:
        rows = [json.loads(line) for line in (lay.base / "builds.jsonl").read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return None
    mine = [r["seconds"] for r in rows if r.get("bench") == bench] or [r["seconds"] for r in rows]
    return sum(mine) / len(mine) if mine else None


def snapshots(tasks: list[Task]) -> list[Task]:
    """One task per repo and commit: the graphs ``tasks`` need."""
    seen: dict[tuple[str, str], Task] = {}
    for task in tasks:
        seen.setdefault((task.repo.lower(), task.commit.lower()), task)
    return list(seen.values())


def estimate(lay: Layout, bench: str, tasks: list[Task]) -> float | None:
    """Hours the graphs of ``tasks`` not built yet would take, at the mean of the builds so far; None before any."""
    mean = build_seconds(lay, bench)
    if mean is None:
        return None
    return sum(1 for t in snapshots(tasks) if not lay.built(t)) * mean / 3600
