"""What a hook that runs on every grep needs of the bound project, kept between runs: the project's name, which
the hub knows it by, and the directories of its harness and repos. The graph itself is the hub's
(``evo_agents.kg.post_search``).

Resolving the project reads harness.yaml and knowledge.yaml with PyYAML, and importing the harness loader and PyYAML
took about 0.1 s of the hook's start-up on a loaded laptop (2026-10-18), more than its lookup. So the hook resolves a
directory once, writes what it found to CACHE under the kg home, and reads it back while no file the resolution read
has changed: the bindings, the project index, both harness registries, the harness.yaml found above the directory,
and the project's harness.yaml and knowledge.yaml (their size and mtime). A directory that belongs to no project is
kept too, so an unbound checkout costs no YAML either. A cache that cannot be read or written is resolved again.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from evo_agents.infra.files import JsonFile

CACHE = "hook-scope.json"
MAX_ENTRIES = 64  # directories kept; the oldest go first
MANIFEST = "harness.yaml"
REGISTRIES = ("~/.evo/harness/registry.json", "~/.claude/harness/registry.json")


@dataclass
class Scope:
    """The project's name and (repo name, real directory) of the harness and each repo, longest first."""

    project: str
    roots: list[tuple[str, str]]


def kg_home(env) -> Path:
    return Path(env.get("EVO_KG_HOME") or "~/.evo/kg").expanduser()


def stamp(path) -> list[int] | None:
    try:
        found = os.stat(path)
    except OSError:
        return None
    return [found.st_mtime_ns, found.st_size]


def manifest_above(start: Path) -> str | None:
    """The first harness.yaml in ``start`` or above it, as ``evo_agents.harness.find_manifest`` walks."""
    for candidate in (start, *start.parents):
        if (candidate / MANIFEST).is_file():
            return str(candidate / MANIFEST)
    return None


def watched(home: Path, start: Path, files=()) -> dict[str, list[int] | None]:
    paths = [home / "bound.json", home / "projects.json", *(Path(p).expanduser() for p in REGISTRIES), *files]
    above = manifest_above(start)
    return {**{str(p): stamp(p) for p in paths}, "above": [above] if above else None}


def repo_roots(project) -> list[tuple[str, str]]:
    """(repo name, real directory) of the harness and each repo with a checkout, longest directory first: the
    name a file node's ``props.repo`` carries."""
    harness = project.harness
    roots = [(harness.root.name, os.path.realpath(harness.root))]
    for repo in harness.repos():
        path = harness.repo_path(repo) if isinstance(repo, dict) and isinstance(repo.get("name"), str) else None
        if path is not None:
            roots.append((repo["name"], os.path.realpath(path)))
    return sorted(roots, key=lambda r: -len(r[1]))


def resolve(start: Path, home: Path) -> tuple[Scope | None, list[Path]]:
    """The scope of the project ``start`` resolves to, and the project files it read; (None, []) for none."""
    from evo_agents.kg.project import ProjectError, resolve_project

    try:
        project = resolve_project(None, start, home)
    except ProjectError:
        return None, []
    scope = Scope(project.name, repo_roots(project))
    return scope, [project.harness.root / MANIFEST, project.knowledge_path]


def _file(path: Path) -> JsonFile:
    return JsonFile(path, mode=0o644, durable=False, ensure_ascii=False)


def _read(path: Path) -> dict:
    return _file(path).read_object()


def _write(path: Path, data: dict) -> None:
    with contextlib.suppress(OSError):
        _file(path).write(data)


def scope_for(env=None) -> Scope | None:
    """The scope of the project the hook's directory (CLAUDE_PROJECT_DIR, else the cwd) resolves to, from CACHE while
    nothing it was resolved from has changed; None when the directory belongs to no project."""
    env = os.environ if env is None else env
    home = kg_home(env)
    start = Path(env.get("CLAUDE_PROJECT_DIR") or os.getcwd()).expanduser().resolve()
    key = "\0".join((str(start), env.get("EVO_KG_PROJECT", "")))
    path = home / CACHE
    cache = _read(path)
    entry = cache.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("watched"), dict):
        files = [Path(p) for p in entry.get("files") or []]
        if entry["watched"] == watched(home, start, files):
            found = entry.get("scope")
            if not found:
                return None
            if isinstance(found, dict) and isinstance(found.get("project"), str):  # else an older hook's: read again
                return Scope(found["project"], [tuple(r) for r in found["roots"]])
    before = watched(home, start)
    scope, files = resolve(start, home)
    after = watched(home, start, files)
    if any(after.get(name) != value for name, value in before.items()):
        return scope  # something changed while it was read: keep nothing
    cache.pop(key, None)
    kept = dict(list(cache.items())[-(MAX_ENTRIES - 1) :])
    kept[key] = {
        "scope": None if scope is None else {"project": scope.project, "roots": scope.roots},
        "files": [str(f) for f in files],
        "watched": after,
    }
    _write(path, kept)
    return scope
