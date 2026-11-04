"""KG strategies: how a graph is configured for a kind of data, as one declarative file (docs/kg-strategies.md).

A strategy file is YAML with these keys (``version`` 1 and ``name`` are required, the rest optional):

- ``description`` and ``suits``: what it is for, and the classes of the data profile it fits (``kg.profile.CLASSES``).
- ``requires``: ``connectors`` (a source of the project must read with one of them) and ``extras`` (installed extras).
- ``knowledge``: parts of knowledge.yaml it adds, ``identifiers`` (kind, pattern, description) and ``ontology``
  (``node_kinds`` and ``relations`` of an ontology extension). It never adds, removes or relabels a source.
- ``settings``: keys of the project's KG settings (``kg.settings``), such as ``passage_chars`` or ``semantic_weight``.
- ``steps``: optional build steps it turns on (``STEPS``).

The built-in strategies are the files of ``evo_agents/kg/strategies``; a project's own are the files of
``<harness>/strategies``, which win over a built-in of the same name. ``plan`` says what applying one changes without
changing anything; ``apply`` checks the file first, then writes the settings and the knowledge parts that are missing,
and names the keys that need a new build. Standard library and PyYAML only.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from evo_agents.kg import settings as kg_settings
from evo_agents.kg.profile import CLASSES
from evo_agents.kg.schema import validate_ontology

BUILTIN_DIR = Path(__file__).with_name("strategies")
CUSTOM_DIR = "strategies"  # under the harness root
NAME = re.compile(kg_settings.STRATEGY_PATTERN)
STEPS = ("entities",)  # build steps a strategy may turn on; the step itself is ``kg.extract_entities``
KEYS = {"version", "name", "description", "suits", "requires", "knowledge", "settings", "steps"}
REQUIRES = {"connectors", "extras"}
KNOWLEDGE = {"identifiers", "ontology"}
IDENTIFIER_KIND = re.compile(r"^[A-Z][A-Za-z0-9]*$")
ONTOLOGY_LISTS = ("node_kinds", "relations")


class StrategyError(Exception):
    """A strategy that cannot be read or applied; the message says what to fix."""


@dataclass(frozen=True)
class Strategy:
    name: str
    description: str = ""
    suits: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()
    identifiers: tuple[dict, ...] = ()
    ontology: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    steps: tuple[str, ...] = ()
    origin: str = "built-in"  # "built-in" or the path of the file

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "suits": list(self.suits),
            "requires": {"connectors": list(self.connectors), "extras": list(self.extras)},
            "knowledge": {"identifiers": list(self.identifiers), "ontology": self.ontology},
            "settings": self.settings,
            "steps": list(self.steps),
            "origin": self.origin,
        }


def _names(value, where: str, problems: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        problems.append(f"{where} must be a list of names")
        return ()
    return tuple(value)


def parse(data, origin: str = "built-in") -> Strategy:
    """The strategy ``data`` (a loaded file) says; StrategyError naming every problem when it is not valid."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise StrategyError(f"{origin}: a strategy is a mapping")
    for key in data:
        if key not in KEYS:
            problems.append(f"unknown key {key!r} (known: {', '.join(sorted(KEYS))})")
    if data.get("version") != 1:
        problems.append("version must be 1")
    name = data.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        problems.append("name must be lowercase letters, digits and hyphens, starting with a letter")
        name = str(name)
    suits = _names(data.get("suits"), "suits", problems)
    for c in suits:
        if c not in CLASSES:
            problems.append(f"suits: {c!r} is not a class of the data profile ({', '.join(CLASSES)})")
    requires = data.get("requires") or {}
    if not isinstance(requires, dict) or set(requires) - REQUIRES:
        problems.append(f"requires holds only {', '.join(sorted(REQUIRES))}")
        requires = {}
    connectors = _names(requires.get("connectors"), "requires.connectors", problems)
    extras = _names(requires.get("extras"), "requires.extras", problems)
    knowledge = data.get("knowledge") or {}
    if not isinstance(knowledge, dict) or set(knowledge) - KNOWLEDGE:
        problems.append(f"knowledge holds only {', '.join(sorted(KNOWLEDGE))}: a strategy does not touch sources")
        knowledge = {}
    identifiers = knowledge.get("identifiers") or []
    if not isinstance(identifiers, list):
        problems.append("knowledge.identifiers must be a list")
        identifiers = []
    for i, ident in enumerate(identifiers):
        if not isinstance(ident, dict) or not IDENTIFIER_KIND.match(str(ident.get("kind", ""))):
            problems.append(f"knowledge.identifiers[{i}] needs a kind such as Ticket")
            continue
        extra = set(ident) - {"kind", "pattern", "description"}
        if extra or not isinstance(ident.get("pattern"), str):
            problems.append(f"knowledge.identifiers[{i}] holds kind, pattern and description only")
            continue
        try:
            re.compile(ident["pattern"])
        except re.error as exc:
            problems.append(f"knowledge.identifiers[{i}].pattern is not a regular expression: {exc}")
    ontology = knowledge.get("ontology") or {}
    problems += [f"knowledge.ontology {i.path}: {i.message}".replace("  ", " ") for i in validate_ontology(ontology)]
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        problems.append("settings must be a mapping of KG settings keys")
        settings = {}
    if "strategy" in settings:
        problems.append("settings.strategy is set by apply: leave it out")
    try:
        kg_settings.of({k: v for k, v in settings.items() if k != "strategy"})
    except kg_settings.SettingsError as exc:
        problems.append(f"settings: {exc}")
    steps = _names(data.get("steps"), "steps", problems)
    for s in steps:
        if s not in STEPS:
            problems.append(f"steps: {s!r} is not a build step ({', '.join(STEPS)})")
    if problems:
        raise StrategyError(f"strategy {name} ({origin}): " + "; ".join(problems))
    return Strategy(
        name=name,
        description=" ".join(str(data.get("description") or "").split()),
        suits=suits,
        connectors=connectors,
        extras=extras,
        identifiers=tuple(identifiers),
        ontology=dict(ontology),
        settings=dict(settings),
        steps=steps,
        origin=origin,
    )


def load_file(path: Path | str) -> Strategy:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StrategyError(f"{path}: {exc.strerror or exc}") from None
    except yaml.YAMLError as exc:
        raise StrategyError(f"{path}: invalid YAML: {exc}") from None
    return parse(data, str(path))


def builtins() -> dict[str, Strategy]:
    return {s.name: replace(s, origin="built-in") for s in (load_file(p) for p in sorted(BUILTIN_DIR.glob("*.yaml")))}


def customs(harness_root: Path | None) -> dict[str, Strategy]:
    """The project's own strategies, by name; StrategyError for a file that is not valid."""
    if harness_root is None:
        return {}
    found = {}
    for path in sorted((Path(harness_root) / CUSTOM_DIR).glob("*.yaml")):
        strategy = load_file(path)
        found[strategy.name] = strategy
    return found


def available(harness_root: Path | None = None) -> dict[str, Strategy]:
    """Built-in strategies, then the project's own over them."""
    return builtins() | customs(harness_root)


def find(name_or_file: str, harness_root: Path | None = None) -> Strategy:
    """The strategy a name or a file says: an existing file path first, else a name of ``available``."""
    path = Path(name_or_file)
    if path.suffix in (".yaml", ".yml") or path.is_file():
        return load_file(path)
    found = available(harness_root)
    if name_or_file not in found:
        raise StrategyError(f"no strategy named {name_or_file!r}; known: {', '.join(sorted(found))}")
    return found[name_or_file]


def steps_of(name: str | None, harness_root: Path | None = None) -> tuple[str, ...]:
    """The build steps the strategy named ``name`` turns on; none for no strategy or one this machine does not know."""
    if not name:
        return ()
    try:
        return available(harness_root)[name].steps
    except (KeyError, StrategyError):
        return ()


def check_requires(strategy: Strategy, sources: list[dict]) -> list[str]:
    """What the project lacks for ``strategy``: a source with one of its connectors, an installed extra."""
    missing = []
    if strategy.connectors and not any(s.get("connector") in strategy.connectors for s in sources):
        missing.append(f"a source read by one of the connectors {', '.join(strategy.connectors)}")
    from importlib.util import find_spec

    for extra in strategy.extras:
        if find_spec(extra) is None:
            missing.append(f"the python package {extra} (an optional extra)")
    return missing


def _entry_name(entry) -> str | None:
    return entry.get("name") if isinstance(entry, dict) else entry if isinstance(entry, str) else None


def _merged_ontology(current: dict, extra: dict) -> dict:
    out = dict(current)
    for key in ONTOLOGY_LISTS:
        wanted = extra.get(key) or []
        have = list(out.get(key) or [])
        known = {_entry_name(e) for e in have}
        add = [e for e in wanted if _entry_name(e) not in known]
        if add:
            out[key] = have + add
    return out


@dataclass
class Plan:
    strategy: Strategy
    settings: dict  # the KG settings keys it sets (strategy included)
    changed: dict  # what each of them changes: key -> [before, after]
    needs_rebuild: list[str]
    identifiers: list[dict]  # identifiers it adds to knowledge.yaml
    ontology: dict  # the additions to the ontology file
    ontology_file: Path | None
    knowledge_file: Path | None
    problems: list[str]

    def to_json(self) -> dict:
        return {
            "strategy": self.strategy.name,
            "origin": self.strategy.origin,
            "settings": self.settings,
            "changed": self.changed,
            "needs_rebuild": self.needs_rebuild,
            "adds_identifiers": [i["kind"] for i in self.identifiers],
            "adds_ontology": {k: [_entry_name(e) for e in v] for k, v in self.ontology.items()},
            "problems": self.problems,
        }


def plan(strategy: Strategy, project) -> Plan:
    """What applying ``strategy`` to ``project`` (a ``kg.project.Project``) changes; nothing is changed."""
    problems = [f"needs {m}" for m in check_requires(strategy, project.sources())]
    before = kg_settings.read(project.root)
    values = dict(strategy.settings) | {"strategy": strategy.name}
    after = kg_settings.of(values, before)
    have = {i.get("kind") for i in project.knowledge.get("identifiers") or []}
    identifiers = [i for i in strategy.identifiers if i["kind"] not in have]
    ontology_file = None
    additions: dict = {}
    if strategy.ontology:
        try:
            current = project.ontology or {}
        except Exception as exc:  # a project whose own ontology is broken: say so, change nothing
            problems.append(str(exc))
            current = {}
        merged = _merged_ontology(current, strategy.ontology)
        additions = {
            k: merged[k][len(current.get(k) or []) :]
            for k in ONTOLOGY_LISTS
            if len(merged.get(k) or []) > len(current.get(k) or [])
        }
        from evo_agents.harness import ontology_path

        ontology_file = (
            ontology_path(project.knowledge, project.knowledge_path) or project.knowledge_path.parent / "ontology.yaml"
        )
    return Plan(
        strategy,
        values,
        kg_settings.diff(before, after),
        kg_settings.rebuild_keys(before, after) + (["identifiers", "ontology"] if identifiers or additions else []),
        identifiers,
        additions,
        ontology_file,
        project.knowledge_path,
        problems,
    )


def _backup(path: Path) -> None:
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".before-strategy"))


def apply(strategy: Strategy, project, *, reason: str | None = None) -> Plan:
    """Apply ``strategy`` to ``project``: the settings (kept in its history), the identifiers and ontology entries it
    lacks. A file it changes is copied to ``<name>.before-strategy`` first. Raises StrategyError, before anything is
    changed, when the project lacks what the strategy requires."""
    found = plan(strategy, project)
    if found.problems:
        raise StrategyError(f"strategy {strategy.name} cannot be applied: " + "; ".join(found.problems))
    sources_before = [dict(s) for s in project.sources()]
    if found.identifiers or (found.ontology and found.ontology_file != _declared(project)):
        _write_knowledge(project, found)
    if found.ontology:
        _write_ontology(project, found)
    kg_settings.write(
        project.root,
        kg_settings.of(found.settings, kg_settings.read(project.root)),
        reason=reason or f"strategy {strategy.name}",
    )
    reread = yaml.safe_load(project.knowledge_path.read_text(encoding="utf-8")) or {}
    if (reread.get("sources") or []) != sources_before:  # the invariant: a strategy never changes a source
        raise StrategyError(
            "applying the strategy changed the sources of knowledge.yaml: restore the .before-strategy file"
        )
    return found


def _declared(project) -> Path | None:
    from evo_agents.harness import ontology_path

    return ontology_path(project.knowledge, project.knowledge_path)


def _write_knowledge(project, found: Plan) -> None:
    """Add the identifiers, and the ``ontology`` key when the project declares none, keeping the file's comments when
    the keys are not there yet (text appended) and writing the file again from its data when they are."""
    path = project.knowledge_path
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    add: dict = {}
    if found.identifiers:
        add["identifiers"] = (data.get("identifiers") or []) + found.identifiers
    if found.ontology and _declared(project) is None:
        add["ontology"] = found.ontology_file.name
    if not add:
        return
    _backup(path)
    if all(k not in data for k in add):
        block = yaml.safe_dump(add, sort_keys=False, allow_unicode=True)
        path.write_text(text.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(data | add, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _write_ontology(project, found: Plan) -> None:
    path = found.ontology_file
    current = {}
    if path.is_file():
        _backup(path)
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    merged = _merged_ontology(current, found.strategy.ontology)
    path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")


def render_list(strategies: dict[str, Strategy]) -> str:
    return "\n".join(
        f"{s.name:<16} {'built-in' if s.origin == 'built-in' else 'custom':<9} {s.description.split('. ')[0].rstrip('.')}"
        for s in strategies.values()
    )
