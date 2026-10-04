"""Which project a command or an MCP session works on, and that project's configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.harness import (
    Harness,
    find_manifest,
    load_manifest,
    load_ontology,
    load_yaml,
    ontology_path,
    registered_harnesses,
)
from evo_agents.kg.corpus import Corpus, kg_home
from evo_agents.kg.policy import Policy
from evo_agents.schema import errors


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
    def ontology(self) -> dict | None:
        """The ontology extension knowledge.yaml points at, or None when it declares none."""
        path = ontology_path(self.knowledge, self.knowledge_path)
        if path is None:
            return None
        data, issues = load_ontology(path)
        problems = [": ".join(filter(None, (i.path, i.message))) for i in errors(issues)]
        if problems:
            raise ProjectError(f"ontology {path}: " + "; ".join(problems))
        return data

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


def _bindings_path(home: Path) -> Path:
    return home / "bound.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_index(home: Path | None = None) -> dict:
    return _read_json(_index_path(home or kg_home()))


def remember(project: Project) -> None:
    """Record where a project's harness and repos live, so an MCP server started in any of those
    directories can find the project without a flag."""
    index = read_index(project.home)
    index[project.name] = {
        "harness_root": str(project.harness.root),
        "dirs": [str(p) for p in project.repo_dirs()],
    }
    _write_json(_index_path(project.home), index)


def read_bindings(home: Path | None = None) -> dict:
    """Explicit directory bindings written by ``evo-agents kg bind``, keyed by resolved directory."""
    return _read_json(_bindings_path(home or kg_home()))


def binding_of(directory: str | Path, bindings: dict) -> tuple[str, object] | None:
    """The binding that governs ``directory``: its own, else that of its nearest bound ancestor, as (the bound
    directory, its entry in ``bindings``); None when neither it nor an ancestor is bound."""
    start = Path(directory).expanduser().resolve()
    for candidate in (start, *start.parents):
        entry = bindings.get(str(candidate))
        if entry is not None:
            return str(candidate), entry
    return None


def bound_project(entry) -> str | None:
    """The project a binding entry names; None when it names none."""
    name = entry.get("project") if isinstance(entry, dict) else None
    return name if isinstance(name, str) and name else None


def bind(directory: str | Path, project: str | None = None, home: Path | None = None) -> Project:
    """Bind a directory and everything below it to one project. Without a name, pin the project the
    directory resolves to now. The project must load before anything is written."""
    home = home or kg_home()
    path = Path(directory).expanduser().resolve()
    bound = _named(project, home) if project else resolve_project(None, path, home)
    bindings = read_bindings(home)
    bindings[str(path)] = {"project": bound.name, "harness_root": str(bound.harness.root)}
    _write_json(_bindings_path(home), bindings)
    return bound


def unbind(directory: str | Path, home: Path | None = None) -> str | None:
    """Drop the binding of exactly this directory; return the key removed, or None if it had none."""
    home = home or kg_home()
    bindings = read_bindings(home)
    raw = Path(directory).expanduser()
    for key in (str(raw.resolve()), str(raw.absolute())):
        if key in bindings:
            del bindings[key]
            _write_json(_bindings_path(home), bindings)
            return key
    return None


def _load_binding(directory: str, entry: dict, home: Path) -> Project:
    name = bound_project(entry)
    root = entry.get("harness_root") if isinstance(entry, dict) else None
    try:
        if not name:
            raise ProjectError("the binding names no project")
        if root and (Path(root) / "harness.yaml").is_file():
            project = load_project_at(Path(root), home)
            if project.name == name:
                return project
        return _by_name(name, home)
    except ProjectError as exc:
        raise ProjectError(
            f"{directory} is bound to project {name!r}, which does not load ({exc}). Rebind it with"
            f" evo-agents kg bind --project NAME {directory}, or drop it with evo-agents kg bind --remove {directory}"
        ) from exc


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


def _named(name: str, home: Path) -> Project:
    path = Path(name).expanduser()
    if path.is_dir() and (path / "harness.yaml").is_file():
        return load_project_at(path, home)
    return _by_name(name, home)


def resolve_project(
    project: str | None = None, directory: str | Path | None = None, home: Path | None = None
) -> Project:
    """Bind to exactly one project: --project, then EVO_KG_PROJECT, then for the directory
    (CLAUDE_PROJECT_DIR or the cwd) its explicit binding or that of its nearest bound ancestor, then
    the harness.yaml above it, then the projects.json index. Zero or several matches is an error,
    never a guess."""
    home = home or kg_home()
    name = project or os.environ.get("EVO_KG_PROJECT")
    if name:
        return _named(name, home)

    start = Path(directory or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).expanduser().resolve()
    found = binding_of(start, read_bindings(home))
    if found is not None:
        return _load_binding(*found, home)
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
            f"no project contains {start}. Bind it with evo-agents kg bind --project NAME {start}, pass"
            " --project, set EVO_KG_PROJECT, or run from a harness that has knowledge.yaml"
        )
    raise ProjectError(
        f"{start} belongs to several projects ({', '.join(sorted(matches))}). Pick one with"
        f" evo-agents kg bind --project NAME {start}, or pass --project"
    )
