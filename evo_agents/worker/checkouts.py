"""The checkouts of the worker's projects on this machine: the heartbeat's ``checkouts``, keyed ``<project>/<repo>``
as the project and its repo are named on the hub, each ``{path, branch}``. A run of a repo is made from the checkout
of that key, so a project's repo without one here is a run this worker cannot take, and the hub does not hand it.

Three sources, the first that names a key winning:

1. ``checkouts`` of config.json, ``{"<project>/<repo>": "/path"}``, set by hand.
2. The project's repos as the hub lists them (``project_repos``: name and path), kept in config.json when the
   machine registered or joined while signed in to the hub (``evo-agents hub login``). Each path is placed in the
   workspace of the project's cluster in the harness registry, else in the workspace the project was registered
   with, as ``evo-agents hub registry pull`` places it.
3. The harness registry (``~/.evo/harness/registry.json``, then ``~/.claude/harness/registry.json``): the repos of the
   cluster whose ``hub.project`` is the project (on the worker's hub, when the cluster names one), or of the cluster
   named as the project when it names no hub, each keyed by its directory name.

Only a directory that is a git work tree counts. Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable
from pathlib import Path

from evo_agents.harness import REGISTRY_PATHS
from evo_agents.hub.registration import place
from evo_agents.worker.home import WorkerConfig

KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}/[^\x00-\x1f\x7f/][^\x00-\x1f\x7f]{0,199}$")  # as the hub takes it
MAX_PATH_CHARS = 1024
MAX_BRANCH_CHARS = 255


def registry_clusters(paths: Iterable[Path] | None = None) -> list[dict]:
    """The clusters of every registry file there is, in the order of ``paths``."""
    clusters = []
    for path in paths if paths is not None else REGISTRY_PATHS:
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
            found = data.get("clusters") if isinstance(data, dict) else None
            clusters += [cluster for cluster in found or [] if isinstance(cluster, dict)]
    return clusters


def _same_hub(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def project_clusters(clusters: list[dict], project: str, hub_url: str) -> list[dict]:
    """The clusters of ``project`` on the hub at ``hub_url``."""
    found = []
    for cluster in clusters:
        hub = cluster.get("hub")
        if isinstance(hub, dict):
            url = hub.get("url")
            if hub.get("project") == project and (not isinstance(url, str) or _same_hub(url, hub_url)):
                found.append(cluster)
        elif cluster.get("name") == project:
            found.append(cluster)
    return found


def git_branch(path: Path) -> str | None:
    """The branch checked out at the work tree ``path``, read from its HEAD; None when detached or unreadable."""
    dot_git = path / ".git"
    with contextlib.suppress(OSError, ValueError):
        if dot_git.is_file():  # a linked worktree or a submodule: "gitdir: <path>"
            text = dot_git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            git_dir = Path(text.removeprefix("gitdir:").strip())
            dot_git = git_dir if git_dir.is_absolute() else (path / git_dir)
        head = (dot_git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: refs/heads/"):
            return head.removeprefix("ref: refs/heads/") or None
    return None


def is_work_tree(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def _entry(path: Path) -> dict | None:
    if not is_work_tree(path) or len(str(path)) > MAX_PATH_CHARS:
        return None
    branch = git_branch(path)
    if branch is not None and (len(branch) > MAX_BRANCH_CHARS or not branch.isprintable()):
        branch = None
    return {"path": str(path), "branch": branch}


def discover(config: WorkerConfig, clusters: list[dict] | None = None) -> dict[str, dict]:
    """``{"<project>/<repo>": {"path", "branch"}}`` for the worker's projects (see the module's docstring)."""
    clusters = registry_clusters() if clusters is None else clusters
    found: dict[str, dict] = {}

    def add(key: str, path: Path) -> None:
        if key in found or not KEY.match(key) or key.split("/", 1)[0] not in config.projects:
            return
        entry = _entry(path)
        if entry is not None:
            found[key] = entry

    for key, path in (config.checkouts or {}).items():
        add(key, Path(path).expanduser())
    for project in config.projects:
        own = project_clusters(clusters, project, config.url)
        held = config.repos.get(project) or {}
        workspace = next((c.get("workspace") for c in own if isinstance(c.get("workspace"), str)), None)
        workspace = workspace or held.get("workspace")
        for repo in held.get("repos") or []:
            name = repo.get("name") if isinstance(repo, dict) else None
            if isinstance(name, str) and workspace:
                add(f"{project}/{name}", place(repo.get("path") or name, Path(workspace).expanduser()))
        for cluster in own:
            for path in cluster.get("repos") or []:
                if isinstance(path, str) and path:
                    local = Path(path).expanduser()
                    add(f"{project}/{local.name}", local)
    return found


def default_branch_of(config: WorkerConfig, project: str, repo: str) -> str | None:
    """The repo's default branch as the hub lists it, when config.json holds the project's repos."""
    for entry in (config.repos.get(project) or {}).get("repos") or []:
        if isinstance(entry, dict) and entry.get("name") == repo and isinstance(entry.get("default_branch"), str):
            return entry["default_branch"]
    return None


def hub_repos(project: dict) -> dict:
    """What config.json keeps of a project as GET /v1/projects/{p} answers it: the workspace it was registered with
    and its repos."""
    harness = project.get("harness") if isinstance(project.get("harness"), dict) else {}
    repos = []
    for repo in project.get("repos") or []:
        if isinstance(repo, dict) and isinstance(repo.get("name"), str):
            repos.append({"name": repo["name"], "path": repo.get("path"), "default_branch": repo.get("default_branch")})
    workspace = harness.get("workspace") if isinstance(harness.get("workspace"), str) else None
    return {"workspace": workspace, "repos": repos}
