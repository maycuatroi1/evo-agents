"""Harness manifests: one loader and one schema set for every tool that reads a harness.

A harness is a repo (or a directory) holding ``harness.yaml`` plus the files it points at:
``contracts.yaml``, ``plans/*/*.yaml`` and, for projects with a knowledge graph, ``knowledge.yaml``.
Readers are lenient (unknown keys become warnings); validation reports errors and warnings per file.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from importlib import metadata
from pathlib import Path

import yaml

from evo_agents.kg.policy import Policy
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.schema import known_kinds, validate_ontology
from evo_agents.plugins.core import python_contributions
from evo_agents.schema import Issue, errors, validate

MANIFEST = "harness.yaml"
CONTRACTS = "contracts.yaml"
DEFAULT_KNOWLEDGE = "knowledge.yaml"
SCHEMA_DIR = Path(__file__).parent / "schemas"
BUILTIN_CONNECTORS = ("harness", "git", "exec")
CONNECTOR_GROUP = "evo_agents.kg.connectors"

HUB_KEY = "hub"  # what the evo-agents hub adds to the read-only copy of a plan it keeps
SKILL_REGISTRY = Path("~/.claude/harness/registry.json")  # the harness-engineering skill and `hub registry pull`
REGISTRY_PATHS = (Path("~/.evo/harness/registry.json"), SKILL_REGISTRY)


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


def parse_yaml(text: str):
    """``load_yaml`` for text already in hand."""
    return _normalize(yaml.safe_load(text))


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


def ontology_path(knowledge: dict, knowledge_path: Path) -> Path | None:
    """The file knowledge.yaml's ``ontology`` key points at, relative to knowledge.yaml; None without it."""
    name = knowledge.get("ontology")
    if not isinstance(name, str) or not name:
        return None
    path = Path(name).expanduser()
    return path if path.is_absolute() else knowledge_path.parent / path


def load_ontology(path: Path) -> tuple[dict, list[Issue]]:
    """Read an ontology extension and check that it only adds to the hub schema."""
    if not path.is_file():
        return {}, [Issue("", "file not found")]
    try:
        data = load_yaml(path)
    except yaml.YAMLError as exc:
        return {}, [Issue("", f"invalid YAML: {exc}")]
    if data is None:
        data = {}
    return (data if isinstance(data, dict) else {}), validate_ontology(data)


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
    """The builtin connectors, those core plugins contribute (``evo_agents.plugins.core``) and the entry points of
    group evo_agents.kg.connectors."""
    names = set(BUILTIN_CONNECTORS)
    names.update(python_contributions("kg_connectors"))
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


def _validate_file(
    path: Path, kind: str, harness: Harness | None = None, plan_checks: Callable[[dict], list[Issue]] | None = None
) -> FileReport:
    try:
        data = load_yaml(path)
    except yaml.YAMLError as exc:
        return FileReport(path, kind, [Issue("", f"invalid YAML: {exc}")])
    if data is None:
        data = {}
    issues = validate(data, load_schema(kind))
    if kind == "knowledge" and isinstance(data, dict):
        issues += knowledge_semantics(data, harness.manifest if harness else None, path)
    if kind == "plan" and isinstance(data, dict):
        issues += plan_semantics(data, path, harness)
        issues += mirror_issues(data, path, harness)
        if plan_checks is not None:
            issues += plan_checks(data)
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


NO_REF = (
    "no ref: the source reads HEAD of its checkout, whatever branch that has, and machines that push to one hub may "
    "read different branches; name the deployed branch, e.g. ref: origin/main with fetch: true"
)


def _git_source_issues(src: dict, where: str, repo_names: set | None) -> list[Issue]:
    """A git source reads a repo harness.yaml declares or a path, at a ref it names. ``repo_names`` is None when
    there is no harness.yaml to check the repo against."""
    issues: list[Issue] = []
    if not (src.get("repo") or src.get("path")):
        issues.append(Issue(where, "a git source needs repo or path"))
    if src.get("repo") and repo_names is not None and src["repo"] not in repo_names:
        issues.append(Issue(f"{where}.repo", f"repo {src['repo']!r} is not declared in harness.yaml"))
    if not src.get("ref"):
        issues.append(Issue(f"{where}.ref", NO_REF, "warning"))
    return issues


def knowledge_semantics(data: dict, manifest: dict | None = None, path: Path | None = None) -> list[Issue]:
    """Checks a schema cannot express: references between sources, labels and the policy, and, given
    the path of knowledge.yaml, the ontology it points at."""
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
        if connector == "git":
            issues += _git_source_issues(src, where, repo_names if manifest is not None else None)
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
    issues += _hub_semantics(data, manifest)

    for i, ident in enumerate(data.get("identifiers") or []):
        try:
            re.compile((ident or {}).get("pattern", ""))
        except re.error as exc:
            issues.append(Issue(f"identifiers[{i}].pattern", f"invalid regex: {exc}"))
    if path is not None:
        issues += _ontology_semantics(data, path)
    return issues


def _hub_semantics(data: dict, manifest: dict | None) -> list[Issue]:
    """The hub keys: one sink of kind hub at most (its clearance bounds what may be pushed), the project
    harness.yaml names under hub is the one knowledge.yaml declares, and memories and plans, which default
    to the harness source's label, can be pushed at all."""
    issues: list[Issue] = []
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    hub_sinks = [s for s in policy.get("sinks") or [] if isinstance(s, dict) and s.get("kind") == "hub"]
    if len(hub_sinks) > 1:
        names = ", ".join(repr(s.get("id")) for s in hub_sinks)
        issues.append(Issue("policy.sinks", f"sinks {names} are all of kind hub: declare one, it bounds every push"))
    project = data.get("project")
    hub = (manifest or {}).get("hub")
    named = hub.get("project") if isinstance(hub, dict) else None
    if isinstance(named, str) and project and named != project:
        issues.append(Issue("project", f"harness.yaml says hub.project {named!r}, but this file names {project!r}"))

    sources = [s for s in data.get("sources") or [] if isinstance(s, dict)]
    harness = next((s for s in sources if s.get("connector") == "harness" and isinstance(s.get("id"), str)), None)
    sink = hub_sinks[0] if len(hub_sinks) == 1 else None
    if harness is None or sink is None or not isinstance(sink.get("id"), str) or not isinstance(project, str):
        return issues
    clearance = sink.get("clearance") if isinstance(sink.get("clearance"), dict) else {}
    knowledge = {
        "policy": {**policy, "sinks": [{"id": sink["id"], "kind": "hub", "clearance": clearance}]},
        "sources": [harness],
    }
    rules = Policy(project, knowledge)
    label = rules.source_label(harness["id"])
    if not rules.allows(label, sink["id"]):
        where = f"sources[{(data.get('sources') or []).index(harness)}].label"
        message = (
            f"memories and plans default to this label ({rules.describe(label)['level']}), which hub sink "
            f"{sink['id']!r} does not clear: pushing them without a label of their own would be refused"
        )
        issues.append(Issue(where, message, "warning"))
    return issues


def _ontology_semantics(data: dict, path: Path) -> list[Issue]:
    """Without an ``ontology`` key nothing is checked, so an ontology.yaml no knowledge.yaml points at
    (another tool's vocabulary file, say) stays out of validation."""
    opath = ontology_path(data, path)
    if opath is None:
        return []
    ontology, found = load_ontology(opath)
    name = data["ontology"]
    issues = [Issue("ontology", ": ".join(filter(None, (name, i.path, i.message))), i.severity) for i in found]
    kinds = known_kinds(ontology)
    for i, ident in enumerate(data.get("identifiers") or []):
        kind = ident.get("kind") if isinstance(ident, dict) else None
        if isinstance(kind, str) and kind not in kinds:
            issues.append(Issue(f"identifiers[{i}].kind", f"{kind!r} is neither a hub kind nor declared in {name}"))
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
    return issues + _structure_issues(data)


# The shape create-exec-plan asks of a plan, which a draft written by hand often misses: step ids unique and
# acyclic, titles short enough for a step list, and acceptance criteria and steps that name each other by id.
TITLE_LIMIT = 60  # characters, not bytes: titles are often Vietnamese
_CRITERION_ID = re.compile(r"a\d+")


def _structure_issues(data: dict) -> list[Issue]:
    given = data.get("steps") if isinstance(data.get("steps"), list) else []
    steps = [(i, s) for i, s in enumerate(given) if isinstance(s, dict)]
    return _duplicate_ids(steps) + _dependency_issues(steps) + _title_issues(steps) + _criteria_issues(data, steps)


def _step_key(value) -> str | int | None:
    """``value`` when it can be a step's id (a string or an integer), else None."""
    return value if isinstance(value, str | int) and not isinstance(value, bool) else None


def _duplicate_ids(steps: list[tuple[int, dict]]) -> list[Issue]:
    """Each id two steps share, once, at the second step that has it."""
    seen: set = set()
    reported: set = set()
    issues: list[Issue] = []
    for i, step in steps:
        key = _step_key(step.get("id"))
        if key in seen and key not in reported:
            reported.add(key)
            message = (
                f"duplicate step id {key!r}: an earlier step has it too, and depends_on and a step mark by id "
                "reach only one of them; give this step an id of its own"
            )
            issues.append(Issue(f"steps[{i}].id", message, "warning"))
        if key is not None:
            seen.add(key)
    return issues


def _dependency_issues(steps: list[tuple[int, dict]]) -> list[Issue]:
    """A step that depends on itself, and each cycle of steps that depend on each other, once."""
    first: dict = {}
    for i, step in steps:
        key = _step_key(step.get("id"))
        if key is not None:
            first.setdefault(key, i)
    graph: dict = {key: [] for key in first}
    issues: list[Issue] = []
    for i, step in steps:
        key, deps = _step_key(step.get("id")), step.get("depends_on")
        if key is None or not isinstance(deps, list):
            continue
        deps = [d for d in map(_step_key, deps) if d is not None]
        if key in deps:
            message = f"step {key!r} depends on itself: remove {key!r} from its depends_on"
            issues.append(Issue(f"steps[{i}].depends_on", message, "warning"))
        graph[key] += [d for d in deps if d in first and d != key and d not in graph[key]]
    for members in _cycles(graph):
        start = min(members, key=first.__getitem__)
        path = " -> ".join(str(k) for k in _cycle_path(graph, members, start))
        listed = ", ".join(str(k) for k in sorted(members, key=first.__getitem__))
        message = (
            f"steps {listed} depend on each other in a cycle ({path}), so none of them can start first: "
            "remove the depends_on that points back"
        )
        issues.append(Issue(f"steps[{first[start]}].depends_on", message, "warning"))
    return issues


def _cycles(graph: dict) -> list[set]:
    """The strongly connected components of ``graph`` with more than one node: Tarjan's algorithm without
    recursion, so the hub, which checks every plan pushed to it, cannot run out of stack on a long chain."""
    index: dict = {}
    low: dict = {}
    stack: list = []
    on_stack: set = set()
    found: list[set] = []

    def enter(node) -> tuple:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        return node, iter(graph[node])

    for root in graph:
        work = [] if root in index else [enter(root)]
        while work:
            node, edges = work[-1]
            nxt = next(edges, None)  # None is never a step id
            if nxt is not None and nxt not in index:
                work.append(enter(nxt))
            elif nxt is not None and nxt in on_stack:
                low[node] = min(low[node], index[nxt])
            elif nxt is None:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    members = set()
                    while node not in members:
                        members.add(stack.pop())
                    on_stack -= members
                    if len(members) > 1:
                        found.append(members)
    return found


def _cycle_path(graph: dict, members: set, start) -> list:
    """The shortest way from ``start`` back to itself inside ``members``, both ends included."""
    came: dict = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in graph[node]:
            if nxt == start:
                back = [node]
                while came[back[-1]] is not None:
                    back.append(came[back[-1]])
                return [*reversed(back), start]
            if nxt in members and nxt not in came:
                came[nxt] = node
                queue.append(nxt)
    return [start, start]


def _title_issues(steps: list[tuple[int, dict]]) -> list[Issue]:
    """A title longer than a step list shows. A missing one is not reported: older plans have none."""
    issues: list[Issue] = []
    for i, step in steps:
        title = step.get("title")
        length = len(unicodedata.normalize("NFC", title)) if isinstance(title, str) else 0
        if length > TITLE_LIMIT:
            message = (
                f"title is {length} characters, over {TITLE_LIMIT}: shorten it to the outcome the step names; "
                "what keeps the full description"
            )
            issues.append(Issue(f"steps[{i}].title", message, "warning"))
    return issues


def _named(acceptance) -> list[str]:
    """The words of a step's acceptance, in order: a list of criterion ids or sentences, or a string such as
    ``a1; a9``. Each whole item counts as a word too, so an id such as ``AC-1`` is found."""
    words: list[str] = []
    for item in acceptance if isinstance(acceptance, list) else [acceptance]:
        if isinstance(item, str):
            words += [item.strip(), *re.findall(r"\w+", item)]
        elif _step_key(item) is not None:
            words.append(str(item))
    return words


def _criteria_issues(data: dict, steps: list[tuple[int, dict]]) -> list[Issue]:
    """A criterion id a step names that the plan's acceptance lacks, and a criterion with an id no step names
    (an invariant, which every step that could break it checks, needs none). Only ``a<number>`` words of a step's
    acceptance read as ids: older plans hold sentences there."""
    criteria = data.get("acceptance") if isinstance(data.get("acceptance"), list) else []
    ids = {str(c["id"]) for c in criteria if isinstance(c, dict) and _step_key(c.get("id")) is not None}
    issues: list[Issue] = []
    named: set[str] = set()
    for i, step in steps:
        if "acceptance" not in step:
            continue
        words = _named(step["acceptance"])
        named.update(words)
        unknown = [w for w in dict.fromkeys(words) if _CRITERION_ID.fullmatch(w) and w not in ids]
        for word in unknown:
            message = (
                f"acceptance names {word!r}, which no criterion of the plan's acceptance has as its id: "
                "add that criterion, or name an existing one"
            )
            issues.append(Issue(f"steps[{i}].acceptance", message, "warning"))
    for j, criterion in enumerate(criteria):
        if not isinstance(criterion, dict) or criterion.get("invariant") is True:
            continue
        key = _step_key(criterion.get("id"))
        if key is not None and str(key) not in named:
            message = (
                f"no step's acceptance names criterion {key!r}: add {key} to the acceptance of the step whose "
                "verify proves it, or mark it invariant: true if it must hold after every step"
            )
            issues.append(Issue(f"acceptance[{j}]", message, "warning"))
    return issues


def plan_body(data: dict) -> dict:
    """The plan without the hub key: what the hub stores and what its digest covers."""
    return {key: value for key, value in data.items() if key != HUB_KEY}


def plan_digest(data: dict) -> str:
    """``sha256:<hex>`` of the canonical JSON of kg/1 (keys sorted, no whitespace, UTF-8) of the plan as loaded
    by ``load_yaml``, without the hub key. Key order, comments and YAML styles do not change it."""
    return sha256(canonical_json(plan_body(data)))


def hub_project(harness: Harness | None) -> str | None:
    """The project harness.yaml names under ``hub``; set, the hub owns the plans and git keeps copies."""
    hub = (harness.manifest if harness else {}).get(HUB_KEY)
    project = hub.get("project") if isinstance(hub, dict) else None
    return project if isinstance(project, str) and project else None


def mirror_issues(data: dict, path: Path, harness: Harness | None = None) -> list[Issue]:
    """In a harness whose plans the hub owns, a plan file is a copy the hub wrote: its hub key names the project
    of harness.yaml, and its digest is the digest of what the file holds now. A file edited by hand fails here,
    with the two ways out."""
    project = hub_project(harness)
    if project is None:
        return []
    try:
        where = path.resolve().relative_to(harness.root).as_posix()
    except ValueError:
        where = path.as_posix()
    hub = data.get(HUB_KEY)
    if not isinstance(hub, dict):
        message = (
            f"{where} has no hub key, so it is not a copy of a hub plan. Push it with "
            f"`evo-agents hub plan put {where}` or restore the hub's copy with `evo-agents hub plan export .`"
        )
        return [Issue(HUB_KEY, message)]
    if hub.get("project") != project:
        message = (
            f"{where} is a copy of a plan of hub project {hub.get('project')!r}, but harness.yaml says hub.project "
            f"{project!r}: restore it with `evo-agents hub plan export .`"
        )
        return [Issue(f"{HUB_KEY}.project", message)]
    if hub.get("digest") != plan_digest(data):
        revision = hub.get("revision") if isinstance(hub.get("revision"), int) else "N"
        message = (
            f"{where} was edited outside the hub (digest mismatch). Push it with "
            f"`evo-agents hub plan put {where} --if-revision {revision}` or restore it with "
            "`evo-agents hub plan export .`"
        )
        return [Issue(f"{HUB_KEY}.digest", message)]
    return []


def validate_harness(root: Path | str, plan_checks: Callable[[dict], list[Issue]] | None = None) -> list[FileReport]:
    """Validate every file a harness owns: manifest, contracts, knowledge, plans. ``plan_checks`` adds the issues of
    a package above this one to each plan's (``evo-agents harness validate`` warns of a verify that cannot be shell)."""
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
        reports.append(_validate_file(plan, "plan", harness, plan_checks))
    return reports
