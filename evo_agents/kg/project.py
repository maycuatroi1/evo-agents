"""Which project a command or an MCP session works on, and that project's configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.harness import Harness, find_manifest, load_manifest, load_yaml, registered_harnesses
from evo_agents.kg.corpus import Corpus, kg_home
from evo_agents.kg.policy import Policy


class ProjectError(RuntimeError):
    pass


@dataclass
class Project:
    name: str
    harness: Harness
    knowledge: dict
    knowledge_path: Path
    home: Path = field(default_factory=kg_home)

    @property
    def policy(self) -> Policy:
        return Policy(self.name, self.knowledge)

    @property
    def root(self) -> Path:
        return self.home / self.name

    def sources(self) -> list[dict]:
        return list(self.knowledge.get("sources") or [])

    def source(self, source_id: str) -> dict:
        for src in self.sources():
            if src["id"] == source_id:
                return src
        raise ProjectError(f"project {self.name!r} has no source {source_id!r}")

    def corpus(self) -> Corpus:
        return Corpus(self.name, self.home)

    def repo_dirs(self) -> list[Path]:
        dirs = [self.harness.root]
        for repo in self.harness.repos():
            path = self.harness.repo_path(repo)
            if path is not None:
                dirs.append(path)
        return dirs


def load_project_at(root: Path, home: Path | None = None) -> Project:
    harness = load_manifest(root)
    kpath = harness.knowledge_path()
    if kpath is None or not kpath.is_file():
        raise ProjectError(f"{harness.root} has no knowledge.yaml (set knowledge_file in harness.yaml)")
    knowledge = load_yaml(kpath) or {}
    name = knowledge.get("project")
    if not name:
        raise ProjectError(f"{kpath} does not name a project")
    return Project(name, harness, knowledge, kpath, home or kg_home())


def _index_path(home: Path) -> Path:
    return home / "projects.json"


def read_index(home: Path | None = None) -> dict:
    path = _index_path(home or kg_home())
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def remember(project: Project) -> None:
    """Record where a project's harness and repos live, so an MCP server started in any of those
    directories can find the project without a flag."""
    index = read_index(project.home)
    index[project.name] = {
        "harness_root": str(project.harness.root),
        "dirs": [str(p) for p in project.repo_dirs()],
    }
    path = _index_path(project.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _by_name(name: str, home: Path) -> Project:
    entry = read_index(home).get(name)
    if entry and Path(entry["harness_root"]).is_dir():
        return load_project_at(Path(entry["harness_root"]), home)
    for cluster in registered_harnesses():
        root = Path(cluster["root"])
        if not (root / "harness.yaml").is_file():
            continue
        try:
            project = load_project_at(root, home)
        except ProjectError:
            continue
        if project.name == name:
            return project
    raise ProjectError(f"unknown project {name!r}: run evo-agents kg sync from its harness first")


def resolve_project(
    project: str | None = None, directory: str | Path | None = None, home: Path | None = None
) -> Project:
    """Bind to exactly one project: --project, then EVO_KG_PROJECT, then the directory
    (CLAUDE_PROJECT_DIR or the cwd). Zero or several matches is an error, never a guess."""
    home = home or kg_home()
    name = project or os.environ.get("EVO_KG_PROJECT")
    if name:
        path = Path(name).expanduser()
        if path.is_dir() and (path / "harness.yaml").is_file():
            return load_project_at(path, home)
        return _by_name(name, home)

    start = Path(directory or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).expanduser().resolve()
    root = find_manifest(start)
    if root is not None:
        try:
            return load_project_at(root, home)
        except ProjectError:
            pass
    matches = []
    for pname, entry in read_index(home).items():
        for d in entry.get("dirs", []):
            d = Path(d).expanduser().resolve()
            if start == d or d in start.parents:
                matches.append(pname)
                break
    if len(matches) == 1:
        return _by_name(matches[0], home)
    if not matches:
        raise ProjectError(
            f"no project contains {start}. Pass --project, set EVO_KG_PROJECT, or run from a harness"
            " that has knowledge.yaml"
        )
    raise ProjectError(f"{start} belongs to several projects ({', '.join(sorted(matches))}); pass --project")
