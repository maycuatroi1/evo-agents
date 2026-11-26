"""The container stacks runs leave on a worker's container engine, and the engine's caches: what the daemon's cleanup
(``daemon_cleanup``) stops and removes so the drive of its worktrees keeps its reserve.

An agent that brings up a stack with Docker Compose in its worktree leaves containers the engine labels with the
directory it ran from (``com.docker.compose.project.working_dir``), and images Compose built labelled with the project
(``com.docker.compose.project``). Nothing of the run's own ends them: a stack whose run ended keeps running, and its
images and the engine's build cache keep the drive full long after the worktree went. So the cleanup:

- stops the running containers of a stack started from the worktrees of a run that ended (``stop``; they stay, and
  start again with ``docker compose up``),
- removes the containers of a stack started from a worktree it removes, or from one under a worker's worktrees that is
  gone (``remove``), with their anonymous volumes; then the networks and the images of each project no container is
  left of, an image still used by another container staying. Named volumes stay: they may hold what a person wants,
- and, while the drive is short of its reserve, prunes the engine's caches (``prune``): the build cache no image uses
  and the images no tag names. A tagged image no container uses stays: it may be the only copy of a build.

The heartbeat says what the engine holds and how much of it nothing uses (``usage``), so the hub can say why a worker
short of its reserve stays short. Every call goes through the engine's client (docker, or podman, found as for a run's
container, ``environments.find_engine``) with a timeout; a failure of the engine removes nothing and fails nothing.
``EVO_WORKER_ENGINE_CLEANUP=0`` in the daemon's environment leaves the engine alone. Standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents.worker.environments import find_engine, is_podman, run_engine

log = logging.getLogger("evo_agents.worker")

VARIABLE = "EVO_WORKER_ENGINE_CLEANUP"  # 0, false, no or off: the cleanup leaves the container engine alone
PROJECT = "com.docker.compose.project"
WORKING_DIR = "com.docker.compose.project.working_dir"
LIST_TIMEOUT = 30.0  # seconds to list or inspect containers, images or networks
REMOVE_TIMEOUT = 120.0  # seconds to stop or remove a batch of containers or images
PRUNE_TIMEOUT = 600.0  # seconds a prune of the build cache may take on a full drive
USAGE_TIMEOUT = 60.0  # seconds ``system df`` may take with many images
GB = 1024**3
_LABEL = "{{index .Config.Labels " + json.dumps(PROJECT) + "}}"
_DIRECTORY = "{{index .Config.Labels " + json.dumps(WORKING_DIR) + "}}"
_INSPECT = "\t".join(("{{.Id}}", "{{.State.Running}}", _LABEL, _DIRECTORY))
_SIZE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kKMGTP]?i?B)?")
_UNITS = {"": 1, "B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15}
_UNITS |= {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4, "PiB": 1024**5}
_RECLAIMED = re.compile(r"reclaimed space:\s*(\S+)", re.I)


@dataclass(frozen=True)
class Container:
    """A container of a Compose stack: its id, its project, the directory Compose ran from and whether it runs."""

    id: str
    project: str
    working_dir: Path
    running: bool


def enabled(env: Mapping[str, str]) -> bool:
    """Whether the cleanup may touch the container engine: not when ``VARIABLE`` says 0, false, no or off."""
    return (env.get(VARIABLE) or "").strip().lower() not in ("0", "false", "no", "off")


def size_bytes(text: object) -> float | None:
    """The bytes a size the engine prints means (``152.2GB``, ``765.2MB (92%)``, ``0B``, ``1.5GiB``, or a number of
    bytes); None when it is none of these."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int | float):
        return float(text)
    found = _SIZE.match(str(text or ""))
    if found is None or (found.group(2) or "") not in _UNITS:
        return None
    return float(found.group(1)) * _UNITS[found.group(2) or ""]


def reclaimed_gb(text: str) -> float:
    """The gigabytes the ``Total reclaimed space:`` lines of a prune's output add up to; 0 when it says none."""
    total = sum(size_bytes(found) or 0.0 for found in _RECLAIMED.findall(text or ""))
    return round(total / GB, 2)


def read_usage(text: str) -> dict | None:
    """{engine_gb, engine_unused_gb} from ``system df``: what the engine holds (images, containers, volumes and build
    cache) and how much of it nothing uses, from Docker's ``{{json .}}`` lines or Podman's JSON array; None when it
    says nothing of the kind."""
    try:
        rows = json.loads(text)
    except ValueError:
        rows = []
        for line in (text or "").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if isinstance(rows, dict):
        rows = [rows]
    held = unused = 0.0
    seen = False
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        size = size_bytes(row.get("RawSize", row.get("Size")))
        if size is None:
            continue
        seen = True
        held += size
        unused += size_bytes(row.get("RawReclaimable", row.get("Reclaimable"))) or 0.0
    if not seen:
        return None
    return {"engine_gb": round(held / GB, 2), "engine_unused_gb": round(min(unused, held) / GB, 2)}


def read_containers(text: str) -> list[Container]:
    """The containers of the ``inspect`` lines of ``_INSPECT``; one with no working directory is left out."""
    found = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[0].strip() or not parts[3].strip() or parts[3].strip() == "<no value>":
            continue
        project = "" if parts[2].strip() == "<no value>" else parts[2].strip()
        found.append(Container(parts[0].strip(), project, Path(parts[3].strip()), parts[1].strip() == "true"))
    return found


def within(path: Path, directories: Iterable[Path]) -> bool:
    """Whether ``path`` is one of ``directories`` or lies in one, as written or with symbolic links resolved."""
    forms = {Path(os.path.normpath(path))}
    try:
        forms.add(Path(path).resolve())
    except OSError:
        pass
    for directory in directories:
        roots = {Path(os.path.normpath(directory))}
        try:
            roots.add(Path(directory).resolve())
        except OSError:
            pass
        if any(form == root or root in form.parents for form in forms for root in roots):
            return True
    return False


class Engine:
    """The container engine of this machine, through its client ``path`` with the daemon's ``env``."""

    def __init__(self, path: str, env: Mapping[str, str] | None = None):
        self.path, self.env = path, env

    @classmethod
    def find(cls, env: Mapping[str, str]) -> Engine | None:
        """The engine the cleanup may tend: none when ``VARIABLE`` turns it off or no client is on PATH."""
        if not enabled(env):
            return None
        path, _why = find_engine(env)
        return cls(path, env) if path else None

    def _run(self, args: list[str], timeout: float = LIST_TIMEOUT):
        done = run_engine(self.path, args, self.env, timeout)
        if done is not None and done.returncode != 0:
            log.info(
                "the container engine refused a cleanup command",
                extra={"command": args[0], "error": " ".join((done.stderr or "").split())[-300:]},
            )
        return done

    def _ids(self, args: list[str]) -> list[str]:
        done = self._run(args)
        return done.stdout.split() if done is not None and done.returncode == 0 else []

    def containers(self) -> list[Container] | None:
        """The containers, running or not, of every Compose stack the engine has; None when it did not answer."""
        listed = self._run(["ps", "-aq", "--no-trunc", "--filter", f"label={WORKING_DIR}"])
        if listed is None or listed.returncode != 0:
            return None
        ids = listed.stdout.split()
        if not ids:
            return []
        inspected = self._run(["inspect", "--format", _INSPECT, *ids])
        if inspected is None:
            return None
        return read_containers(inspected.stdout)  # a container removed since the list is left out

    def stop(self, found: Iterable[Container]) -> int:
        """Stop the running containers of ``found``; how many."""
        ids = sorted({container.id for container in found if container.running})
        if not ids:
            return 0
        done = self._run(["stop", *ids], REMOVE_TIMEOUT)
        return len(ids) if done is not None and done.returncode == 0 else 0

    def remove(self, found: Iterable[Container]) -> dict:
        """Remove the containers of ``found``, running or not, with their anonymous volumes; then the networks and the
        images of each of their projects no container is left of. {containers, images}: how many went."""
        found = list(found)
        ids = sorted({container.id for container in found})
        if not ids:
            return {"containers": 0, "images": 0}
        done = self._run(["rm", "-f", "-v", *ids], REMOVE_TIMEOUT)
        removed = len(ids) if done is not None and done.returncode == 0 else 0
        images = 0
        for project in sorted({container.project for container in found if container.project}):
            label = f"label={PROJECT}={project}"
            if self._ids(["ps", "-aq", "--filter", label]):
                continue  # another stack of the same project still has containers: its networks and images stay
            networks = self._ids(["network", "ls", "-q", "--filter", label])
            if networks:
                self._run(["network", "rm", *networks])
            for image in sorted(set(self._ids(["images", "-q", "--no-trunc", "--filter", label]))):
                gone = self._run(["rmi", image], REMOVE_TIMEOUT)  # without -f: an image in use stays
                images += 1 if gone is not None and gone.returncode == 0 else 0
        return {"containers": removed, "images": images}

    def prune(self) -> float:
        """Prune the build cache no image uses and the images no tag names; the gigabytes the engine says it freed."""
        freed = 0.0
        if not is_podman(self.path):
            done = self._run(["builder", "prune", "-f"], PRUNE_TIMEOUT)
            freed += reclaimed_gb(done.stdout) if done is not None and done.returncode == 0 else 0.0
        done = self._run(["image", "prune", "-f"], PRUNE_TIMEOUT)
        freed += reclaimed_gb(done.stdout) if done is not None and done.returncode == 0 else 0.0
        return round(freed, 2)

    def usage(self) -> dict | None:
        """{engine_gb, engine_unused_gb} of ``system df``; None when the engine did not say."""
        args = ["system", "df", "--format", "json" if is_podman(self.path) else "{{json .}}"]
        done = self._run(args, USAGE_TIMEOUT)
        return read_usage(done.stdout) if done is not None and done.returncode == 0 else None

    def root(self) -> Path | None:
        """The directory the engine keeps its images and containers in; None when it does not say."""
        template = "{{.Store.GraphRoot}}" if is_podman(self.path) else "{{.DockerRootDir}}"
        done = self._run(["info", "--format", template])
        found = (done.stdout or "").strip() if done is not None and done.returncode == 0 else ""
        return Path(found) if found.startswith("/") else None


def on_drive(engine: Engine, device: int) -> bool:
    """Whether what ``engine`` holds lies on the drive ``device``: yes when its root is there, or cannot be looked at
    from this machine (an engine in a virtual machine whose disk image lies here, as Docker Desktop's)."""
    root = engine.root()
    if root is None:
        return True
    try:
        return root.stat().st_dev == device
    except OSError:
        return True


__all__ = [
    "VARIABLE",
    "Container",
    "Engine",
    "enabled",
    "on_drive",
    "read_containers",
    "read_usage",
    "reclaimed_gb",
    "size_bytes",
    "within",
]
