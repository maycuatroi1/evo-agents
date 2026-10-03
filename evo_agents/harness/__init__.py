"""Harness manifests: one loader and one schema set for every tool that reads a harness.

A harness is a repo (or a directory) holding ``harness.yaml`` plus the files it points at:
``contracts.yaml``, ``plans/*/*.yaml`` and, for projects with a knowledge graph, ``knowledge.yaml``.
Readers are lenient (unknown keys become warnings); validation reports errors and warnings per file.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from functools import cache
from importlib import metadata
from pathlib import Path

import yaml

from evo_agents.schema import Issue, errors, validate

MANIFEST = "harness.yaml"
CONTRACTS = "contracts.yaml"
DEFAULT_KNOWLEDGE = "knowledge.yaml"
SCHEMA_DIR = Path(__file__).parent / "schemas"
BUILTIN_CONNECTORS = ("harness", "git", "exec")
CONNECTOR_GROUP = "evo_agents.kg.connectors"

REGISTRY_PATHS = (
    Path("~/.evo/harness/registry.json"),
    Path("~/.claude/harness/registry.json"),  # written by the harness-engineering skill
)


@cache
def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def _normalize(value):
    """YAML turns unquoted timestamps into datetime objects; schemas expect strings."""
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as fh:
        return _normalize(yaml.safe_load(fh))


def find_manifest(start: Path | str | None = None) -> Path | None:
    """Walk up from ``start`` and return the first directory holding harness.yaml."""
    here = Path(start or Path.cwd()).expanduser().resolve()
    for candidate in (here, *here.parents):
        if (candidate / MANIFEST).is_file():
            return candidate
    return None


@dataclass
class Harness:
    root: Path
    manifest: dict
    issues: list[Issue] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.manifest.get("name", self.root.name)

    @property
    def workspace(self) -> Path:
        ws = self.manifest.get("workspace")
        if not ws:
            return self.root.parent
        path = Path(ws).expanduser()
        return path if path.is_absolute() else (self.root / path).resolve()

    def repos(self) -> list[dict]:
        return list(self.manifest.get("repos") or [])

    def repo(self, name: str) -> dict | None:
        return next((r for r in self.repos() if r.get("name") == name), None)

    def known_repos(self) -> set[str]:
        """Repo names plans and seams may use: every manifest entry (``external: true`` marks one outside
        the cluster) plus the harness repo itself, which a manifest need not list. That repo goes by its
        directory name, as in the harness connector; ``name`` is the cluster's name, not a repo's."""
        names = {r["name"] for r in self.repos() if isinstance(r, dict) and isinstance(r.get("name"), str)}
        return names | {self.root.name}

    def repo_path(self, repo: dict) -> Path | None:
        raw = repo.get("path")
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (self.workspace / path)

    def knowledge_path(self) -> Path | None:
        name = self.manifest.get("knowledge_file")
        if name:
            return self.root / name
        default = self.root / DEFAULT_KNOWLEDGE
        return default if default.is_file() else None


def load_manifest(path: Path | str) -> Harness:
    """Load harness.yaml from a harness root or from the file itself."""
    path = Path(path).expanduser()
    root = path.parent if path.name == MANIFEST else path
    data = load_yaml(root / MANIFEST)
    if not isinstance(data, dict):
        data = {}
    issues = validate(data, load_schema("harness"))
    return Harness(root=root.resolve(), manifest=data, issues=issues)


def repo_paths(harness: Harness) -> list[tuple[dict, Path | None]]:
    return [(repo, harness.repo_path(repo)) for repo in harness.repos()]


def registered_harnesses() -> list[dict]:
    """Harness roots known on this machine, newest registry first, deduplicated by root."""
    seen: dict[str, dict] = {}
    for raw in REGISTRY_PATHS:
        path = raw.expanduser()
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in data.get("clusters", []):
            root = entry.get("root")
            if root and root not in seen:
                seen[root] = entry
    return list(seen.values())


def available_connectors() -> set[str]:
    names = set(BUILTIN_CONNECTORS)
    try:
        names.update(ep.name for ep in metadata.entry_points(group=CONNECTOR_GROUP))
    except Exception:  # pragma: no cover - broken environments still validate builtins
        pass
    return names


# ---------------------------------------------------------------------------------------------
# Validation


@dataclass
class FileReport:
    path: Path
    kind: str
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not errors(self.issues)

    def to_json(self) -> dict:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "ok": self.ok,
            "issues": [{"path": i.path, "message": i.message, "severity": i.severity} for i in self.issues],
        }


def _validate_file(path: Path, kind: str, harness: Harness | None = None) -> FileReport:
    try:
        data = load_yaml(path)
    except yaml.YAMLError as exc:
        return FileReport(path, kind, [Issue("", f"invalid YAML: {exc}")])
    if data is None:
        data = {}
    issues = validate(data, load_schema(kind))
    if kind == "knowledge" and isinstance(data, dict):
        issues += knowledge_semantics(data, harness.manifest if harness else None)
    if kind == "plan" and isinstance(data, dict):
        issues += plan_semantics(data, path, harness)
    if kind == "contracts" and isinstance(data, dict):
        issues += contracts_semantics(data, harness)
    return FileReport(path, kind, issues)


def _repo_issues(name, where: str, known: set[str] | None) -> list[Issue]:
    if known is None or not isinstance(name, str) or not name or name in known:
        return []
    message = f"repo {name!r} is not declared in harness.yaml: add it, with external: true if it is outside the cluster"
    return [Issue(where, message, "warning")]


def _source_issues(source, where: str, known: set[str] | None) -> list[Issue]:
    """``source`` is a list of {repo, path}, so the graph knows which repo each file lives in."""
    if isinstance(source, str):
        return [Issue(where, "a plain path: write source as a list of {repo, path}", "warning")]
    if isinstance(source, dict):
        issues = [Issue(where, "a single {repo, path}: write source as a list", "warning")]
        return issues + _repo_issues(source.get("repo"), f"{where}.repo", known)
    issues: list[Issue] = []
    for j, item in enumerate(source if isinstance(source, list) else []):
        if isinstance(item, str):
            issues.append(Issue(f"{where}[{j}]", "a plain path: write it as {repo, path}", "warning"))
        elif isinstance(item, dict):
            issues += _repo_issues(item.get("repo"), f"{where}[{j}].repo", known)
    return issues


def contracts_semantics(data: dict, harness: Harness | None = None) -> list[Issue]:
    """A seam nothing checks must say why; otherwise its drift goes unnoticed. With the manifest, every
    repo a seam names must be declared there."""
    issues: list[Issue] = []
    known = harness.known_repos() if harness else None
    for i, seam in enumerate(data.get("seams") or []):
        if not isinstance(seam, dict):
            continue
        where = f"seams[{i}]"
        if not seam.get("verify") and not seam.get("verify_waiver"):
            issues.append(Issue(where, "no verify and no verify_waiver: nothing checks this seam", "warning"))
        issues += _repo_issues(seam.get("owner"), f"{where}.owner", known)
        consumers = seam.get("consumers")
        for j, consumer in enumerate(consumers if isinstance(consumers, list) else []):
            issues += _repo_issues(consumer, f"{where}.consumers[{j}]", known)
        issues += _source_issues(seam.get("source"), f"{where}.source", known)
    return issues


def knowledge_semantics(data: dict, manifest: dict | None = None) -> list[Issue]:
    """Checks a schema cannot express: references between sources, labels and the policy."""
    issues: list[Issue] = []
    policy = data.get("policy") or {}
    levels = policy.get("levels") or []
    locations = policy.get("locations") or ["any"]
    repo_names = {r.get("name") for r in (manifest or {}).get("repos") or []}
    known = available_connectors()

    seen: set[str] = set()
    for i, src in enumerate(data.get("sources") or []):
        if not isinstance(src, dict):
            continue
        where = f"sources[{i}]"
        sid = src.get("id")
        if sid in seen:
            issues.append(Issue(f"{where}.id", f"duplicate source id {sid!r}"))
        seen.add(sid)
        connector = src.get("connector")
        if connector and connector not in known and not connector.startswith("python:"):
            issues.append(Issue(f"{where}.connector", f"unknown connector {connector!r}"))
        if connector == "exec" and not src.get("command"):
            issues.append(Issue(f"{where}.command", "an exec connector needs a command"))
        if connector == "git" and not (src.get("repo") or src.get("path")):
            issues.append(Issue(where, "a git source needs repo or path"))
        if connector == "git" and src.get("repo") and manifest is not None and src["repo"] not in repo_names:
            issues.append(Issue(f"{where}.repo", f"repo {src['repo']!r} is not declared in harness.yaml"))
        label = src.get("label")
        if not label:
            issues.append(Issue(f"{where}.label", "no label: treated as the highest level with integrity U", "warning"))
        else:
            if label.get("level") and levels and label["level"] not in levels:
                issues.append(Issue(f"{where}.label.level", f"{label['level']!r} is not in policy.levels"))
            if label.get("location") and label["location"] not in locations:
                issues.append(Issue(f"{where}.label.location", f"{label['location']!r} is not in policy.locations"))

    for i, sink in enumerate(policy.get("sinks") or []):
        clr = (sink or {}).get("clearance") or {}
        if clr.get("level") and levels and clr["level"] not in levels:
            issues.append(Issue(f"policy.sinks[{i}].clearance.level", f"{clr['level']!r} is not in policy.levels"))
        if clr.get("location") and clr["location"] not in locations:
            issues.append(
                Issue(f"policy.sinks[{i}].clearance.location", f"{clr['location']!r} is not in policy.locations")
            )

    for i, ident in enumerate(data.get("identifiers") or []):
        try:
            re.compile((ident or {}).get("pattern", ""))
        except re.error as exc:
            issues.append(Issue(f"identifiers[{i}].pattern", f"invalid regex: {exc}"))
    return issues


# A commit is written ``repo@sha``. A bare token of lowercase hex holding both a digit and a letter reads as
# a commit when it is as long as an abbreviated (7 to 12) or full (40) SHA, unless it is part of a longer
# token (UUIDs, ``sha256:``, URLs, ``key=``, the far end of ``a..b``), is cut short with an ellipsis, or
# one of the two words before it names a digest or an ID.
_BARE_SHA = re.compile(r"(?<![\w@:/.#=-])[0-9a-f]{7,40}(?![\w@/-]|\.\w|\.\.\.|…)")
_NOT_A_COMMIT = re.compile(r"(?i)(?:sha-?\d+|md5|digest|hash|checksum|fingerprint|etag|uuid|run|(?:\w*[_-])?id)")
_WORD = re.compile(r"[\w.-]*\w")


def _bare_commits(text: str, known: set[str] | None) -> list[tuple[str, str | None]]:
    """Bare commit SHAs in ``text``, each with the known repo named in the two words before it (the old
    ``repo sha`` style), if any."""
    found: dict[str, str | None] = {}
    for m in _BARE_SHA.finditer(text):
        sha = m.group()
        if sha in found or 12 < len(sha) < 40 or not re.search(r"\d", sha) or not re.search(r"[a-f]", sha):
            continue
        words = _WORD.findall(text[max(0, m.start() - 80) : m.start()])[-2:]
        if any(_NOT_A_COMMIT.fullmatch(w) for w in words):
            continue
        found[sha] = next((w for w in reversed(words) if known and w in known), None)
    return list(found.items())


def _evidence_issues(text: str, where: str, known: set[str] | None) -> list[Issue]:
    shas = _bare_commits(text, known)
    if not shas:
        return []
    sha, repo = shas[0]
    example = f"{repo or 'repo'}@{sha}"
    if len(shas) == 1:
        return [Issue(where, f"bare commit {sha}: write {example}", "warning")]
    listed = ", ".join(s for s, _ in shas[:3]) + (f" and {len(shas) - 3} more" if len(shas) > 3 else "")
    return [Issue(where, f"bare commits {listed}: write repo@sha, e.g. {example}", "warning")]


def plan_semantics(data: dict, path: Path, harness: Harness | None = None) -> list[Issue]:
    """Step references, evidence and naming conventions. With the manifest, every repo the plan names
    must be declared there."""
    issues: list[Issue] = []
    if data.get("id") and path.stem != data["id"]:
        issues.append(Issue("id", f"plan id {data['id']!r} differs from file name {path.stem!r}", "warning"))
    repos = harness.known_repos() if harness else None
    for i, entry in enumerate(data.get("repos") or []):
        if isinstance(entry, dict):
            issues += _repo_issues(entry.get("repo"), f"repos[{i}].repo", repos)
    ids = [s.get("id") for s in data.get("steps") or [] if isinstance(s, dict)]
    known = {i for i in ids if i is not None}
    for i, step in enumerate(data.get("steps") or []):
        if not isinstance(step, dict):
            continue
        for dep in step.get("depends_on") or []:
            if dep not in known:
                issues.append(Issue(f"steps[{i}].depends_on", f"unknown step {dep!r}", "warning"))
        if step.get("status") == "done" and not step.get("evidence"):
            issues.append(Issue(f"steps[{i}]", "done without evidence", "warning"))
        if "order" in step and step.get("id") is None:
            issues.append(Issue(f"steps[{i}]", "order without id: depends_on and links name steps by id", "warning"))
        issues += _repo_issues(step.get("repo"), f"steps[{i}].repo", repos)
        if isinstance(step.get("evidence"), str):
            issues += _evidence_issues(step["evidence"], f"steps[{i}].evidence", repos)
    return issues


def validate_harness(root: Path | str) -> list[FileReport]:
    """Validate every file a harness owns: manifest, contracts, knowledge, plans."""
    root = Path(root).expanduser().resolve()
    if not (root / MANIFEST).is_file():
        found = find_manifest(root)
        if found is None:
            return [FileReport(root / MANIFEST, "harness", [Issue("", "harness.yaml not found")])]
        root = found
    harness = load_manifest(root)
    reports = [FileReport(root / MANIFEST, "harness", harness.issues)]
    if (root / CONTRACTS).is_file():
        reports.append(_validate_file(root / CONTRACTS, "contracts", harness))
    kpath = harness.knowledge_path()
    if kpath is not None:
        if kpath.is_file():
            reports.append(_validate_file(kpath, "knowledge", harness))
        else:
            reports.append(FileReport(kpath, "knowledge", [Issue("", "knowledge_file points at a missing file")]))
    for plan in sorted((root / "plans").glob("*/*.yaml")):
        reports.append(_validate_file(plan, "plan", harness))
    return reports
