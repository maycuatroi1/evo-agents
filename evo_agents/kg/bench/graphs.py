"""The graphs of the bench: one per repo and base commit, built in a kg home of the bench's own, never the hub's.

Layout under the bench cache::

    repos/OWNER__NAME.git              a bare clone of each repo, fetched once, every base commit read from it
    kg/EXTRACTOR/home/                 EVO_KG_HOME of the builds: a project per repo and commit
    kg/EXTRACTOR/memo/OWNER__NAME.sqlite  the memo of a repo's builds: its commits share most of their work
    kg/EXTRACTOR/harness/NAME/         the harness of that project: a git source at the base commit, no pulls
    kg/EXTRACTOR/builds.jsonl          the time each build took, which ``estimate`` reads

A snapshot is the commit read straight from the bare clone (the git connector reads a ref, not a working tree), with
its history up to it as any graph has; the pull requests are not read, since one of them is the fix. EXTRACTOR is a
digest of what makes the graph (``extractor``): the store's schema, the version of each stage, the graphify used, the
code of the git connector and the text the search index is made of; not evo-agents' version, so a release that
changes only the ranking finds its graphs built and searches them again. A build that succeeded leaves ``bench.json`` in its project and is not done again.

Room. The graphs are a cache that grows with every snapshot (a graph takes up to 700 MB; the four sets made 400 GB
and filled a worker's disk), so each build first makes room (``make_room``, docs/kg-bench.md "Room and time"): it
evicts graphs, those of another extractor first and then the least recently used, while the graphs pass
``Room.cap_gb`` or the disk would have less than ``Room.reserve_gb`` free after the build. A graph the caller still
needs (``keep``) or that any run used within GRACE is never evicted. When the reserve cannot be kept, the build raises
DiskFull and the run stops before writing to the disk. ``bench.json`` holds the graph's size, and its mtime is when
a run last used the graph.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from evo_agents.kg.bench.datasets import BenchError
from evo_agents.kg.bench.tasks import SOURCE_ID, Task
from evo_agents.kg.project import Project, load_project_at

GITHUB_URL = "https://github.com/{repo}.git"
RENAMED = {"mui/material": "mui/material-ui"}  # a set names the repo by a name GitHub no longer serves
GIT_TIMEOUT = 3600.0
MARKER = "bench.json"
SLUG = re.compile(r"[^a-z0-9._-]+")
GB = 1_000_000_000
CAP_GB = 200.0  # the graphs kept at most, of every extractor
RESERVE_GB = 50.0  # the free space a build leaves on the cache's disk, at most
RESERVE_SHARE = 0.1  # of a disk too small for RESERVE_GB to be a tenth of it
GRACE = 3600.0  # seconds: a graph a run used this recently may be in use by another process, so it stays
GRAPH_GB = 1.0  # the room a build is taken to need before any graph of the extractor was measured


class DiskFull(BenchError):
    """No room for the next build: the reserve cannot be kept even with every graph not in use evicted."""


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

    def memo(self, repo: str) -> Path:
        """The memo of the repo's builds, its own so that builds of two repos at once never wait on one file."""
        return self.base / "memo" / f"{slug(repo)}.sqlite"

    def vectors(self, repo: str) -> Path:
        """The vectors of the repo's snapshots, by the digest of a row's text: a snapshot embeds only the rows no
        other snapshot of the repo had (with the API backend, that is what is paid for)."""
        return self.base / "vectors" / f"{slug(repo)}.sqlite"

    def harness(self, task: Task) -> Path:
        return self.base / "harness" / self.name(task)

    def project_dir(self, task: Task) -> Path:
        return self.home / self.name(task)

    def built(self, task: Task) -> bool:
        root = self.project_dir(task)
        return (root / MARKER).is_file() and (root / "graph.sqlite").is_file()


def layout(cache: Path) -> Layout:
    return Layout(cache, extractor())


# -- room ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Room:
    """What the graphs may take of the cache's disk (see the module): a cap on them (None: none) and a reserve of free
    space no build goes under."""

    cap_gb: float | None = CAP_GB
    reserve_gb: float | None = None  # None: RESERVE_GB, or RESERVE_SHARE of a smaller disk

    def reserve(self, disk_total: int) -> float:
        if self.reserve_gb is not None:
            return self.reserve_gb * GB
        return min(RESERVE_GB * GB, RESERVE_SHARE * disk_total)


DEFAULT_ROOM = Room()


@dataclass
class Cached:
    """A graph in the cache, of any extractor: its project, when a run last used it, and its size."""

    project: Path
    used: float
    bytes: int
    current: bool  # built by this extractor

    @property
    def name(self) -> str:
        return self.project.name


def tree_bytes(path: Path) -> int:
    """What the files under ``path`` take on disk."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            blocks = getattr(st, "st_blocks", None)  # none on Windows
            total += blocks * 512 if blocks is not None else st.st_size
    return total


def disk_usage(path: Path):
    """The disk ``path`` is on: total, used and free bytes."""
    return shutil.disk_usage(path)


def _marker(project: Path) -> dict | None:
    try:
        found = json.loads((project / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def _sized(project: Path, marker: dict) -> int:
    """The graph's size from its marker; a marker written before markers held it gets it, its mtime (last use) kept."""
    size = marker.get("bytes")
    if isinstance(size, int):
        return size
    size = tree_bytes(project)
    path = project / MARKER
    with contextlib.suppress(OSError):
        st = path.stat()
        spare = path.with_name(f"{MARKER}.{os.getpid()}")
        spare.write_text(json.dumps({**marker, "bytes": size}) + "\n", encoding="utf-8")
        os.replace(spare, path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return size


def cached(lay: Layout) -> list[Cached]:
    """Every graph under the cache's kg directory, of this extractor or another, built or not (a build in flight or
    one that died has no marker: its last use is the newest mtime of its files)."""
    found = []
    for home in sorted((lay.cache / "kg").glob("*/home")):
        current = home.parent.name == lay.extractor
        try:
            projects = sorted(p for p in home.iterdir() if p.is_dir())
        except OSError:  # an extractor another process evicted whole
            continue
        for project in projects:
            marker = _marker(project)
            try:
                if marker is not None:
                    used, size = (project / MARKER).stat().st_mtime, _sized(project, marker)
                else:
                    used = max([project.stat().st_mtime] + [p.stat().st_mtime for p in project.iterdir()])
                    size = tree_bytes(project)
            except OSError:  # evicted meanwhile by another process
                continue
            found.append(Cached(project, used, size, current))
    return found


def _need(task: Task, found: list[Cached]) -> int:
    """The room the task's build is taken to need: the mean graph of its repo, else of the extractor, else GRAPH_GB."""
    mine = [g.bytes for g in found if g.current]
    repo = [g.bytes for g in found if g.current and g.name.rsplit("-", 1)[0] == slug(task.repo)]
    sizes = repo or mine
    return int(sum(sizes) / len(sizes)) if sizes else int(GRAPH_GB * GB)


def _evict(graph: Cached) -> None:
    """Delete a graph, its marker first so no run takes it for built meanwhile; an extractor left with no graph goes
    whole (its memo, harness and shards were for graphs no run builds any more)."""
    (graph.project / MARKER).unlink(missing_ok=True)
    shutil.rmtree(graph.project, ignore_errors=True)
    base = graph.project.parent.parent
    if graph.current:
        shutil.rmtree(base / "harness" / graph.name, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            if not any((base / "home").iterdir()):
                shutil.rmtree(base, ignore_errors=True)


def make_room(lay: Layout, task: Task, room: Room, keep: frozenset[str] = frozenset()) -> list[Cached]:
    """Evict graphs until the task's build fits under the cap and leaves the reserve free (see the module): another
    extractor's first, then the least recently used, never one in ``keep`` (names of this extractor's graphs) nor one
    used within GRACE. Raises DiskFull when the reserve cannot be kept; going over the cap only evicts what it can."""
    lay.base.mkdir(parents=True, exist_ok=True)
    found = cached(lay)
    need = _need(task, found)
    total = sum(g.bytes for g in found)
    cap = None if room.cap_gb is None else room.cap_gb * GB
    disk = disk_usage(lay.base)
    reserve, free = room.reserve(disk.total), disk.free
    now = time.time()
    evicted = []
    for graph in sorted(found, key=lambda g: (g.current, g.used)):
        if free - need >= reserve and (cap is None or total + need <= cap):
            break
        if (graph.current and graph.name in keep) or now - graph.used < GRACE:
            continue
        _evict(graph)
        evicted.append(graph)
        total -= graph.bytes
        free = disk_usage(lay.base).free
    if free - need < reserve:
        raise DiskFull(
            f"the disk of {lay.cache} has {free / GB:.1f} GB free, under the {reserve / GB:.1f} GB reserve once the "
            f"graph of {task.repo}@{task.commit[:12]} (about {need / GB:.1f} GB) is built, and no graph unused for "
            f"{GRACE / 60:.0f} min is left to evict: stopping before the disk fills (the repos asked so far are kept; "
            "free the disk or lower --reserve-gb)"
        )
    return evicted


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
        url = git_url.format(repo=RENAMED.get(task.repo.lower(), task.repo))
        out = _git("clone", "--bare", "--quiet", url, str(clone))
        if out.returncode != 0:
            raise BenchError(f"cannot clone {url}: {out.stderr.strip()[:300]}")
    if _git("cat-file", "-e", f"{task.commit}^{{commit}}", cwd=clone).returncode != 0:
        out = _git("fetch", "--quiet", "origin", task.commit, cwd=clone)
        if out.returncode != 0 or _git("cat-file", "-e", f"{task.commit}^{{commit}}", cwd=clone).returncode != 0:
            raise BenchError(f"{task.repo} has no commit {task.commit[:12]}: {out.stderr.strip()[:200]}")
    return clone


def write_harness(lay: Layout, task: Task, clone: Path) -> Path:
    """The harness of the task's graph: one git source at the base commit, labelled public (``build_graph`` gives the
    build the memo of its repo)."""
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


def build_graph(
    lay: Layout, task: Task, *, git_url: str = GITHUB_URL, room: Room = DEFAULT_ROOM, keep: frozenset[str] = frozenset()
) -> Built:
    """The graph of the task's repo at its base commit: reused when built with this extractor (its last use noted),
    else synced and built once ``make_room`` made room for it, ``keep`` and the task's own graph never evicted.
    DiskFull goes up to the caller: no task can be asked once the disk is short."""
    if lay.built(task):
        with contextlib.suppress(OSError):
            os.utime(lay.project_dir(task) / MARKER)
        return Built(task, reused=True)
    from evo_agents.kg.build import build_project
    from evo_agents.kg.memo import Memo
    from evo_agents.kg.sync import sync_project

    make_room(lay, task, room, keep | {lay.name(task)})
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
            lay.memo(task.repo).parent.mkdir(parents=True, exist_ok=True)
            memo = Memo(lay.memo(task.repo))
            try:
                report = build_project(project, memo=memo)
            finally:
                memo.close()
            if not report.ok:
                return Built(task, False, time.monotonic() - start, f"build: {'; '.join(report.errors[:2])}")
    except BenchError as exc:
        return Built(task, False, time.monotonic() - start, str(exc))
    except Exception as exc:  # a build that crashes fails its task, not the set's whole run
        return Built(task, False, time.monotonic() - start, f"build crashed: {type(exc).__name__}: {str(exc)[:200]}")
    seconds = round(time.monotonic() - start, 1)
    marker = {"repo": task.repo, "commit": task.commit, "extractor": lay.extractor, "seconds": seconds}
    size = tree_bytes(lay.project_dir(task))
    (lay.project_dir(task) / MARKER).write_text(json.dumps({**marker, "bytes": size}) + "\n", encoding="utf-8")
    with (lay.base / "builds.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({**marker, "bench": task.bench}) + "\n")
    return Built(task, False, seconds)


def prune_graph(lay: Layout, task: Task) -> None:
    """Delete the graph of the task's repo and commit, and its harness: ``run --prune`` keeps only graphs in flight."""
    (lay.project_dir(task) / MARKER).unlink(missing_ok=True)
    shutil.rmtree(lay.project_dir(task), ignore_errors=True)
    shutil.rmtree(lay.harness(task), ignore_errors=True)


def prune_repo(lay: Layout, repo: str) -> None:
    """Delete the bare clone of ``repo``, the memo of its builds and its cache of vectors."""
    shutil.rmtree(lay.clone(repo), ignore_errors=True)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{lay.memo(repo)}{suffix}").unlink(missing_ok=True)
        Path(f"{lay.vectors(repo)}{suffix}").unlink(missing_ok=True)


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
