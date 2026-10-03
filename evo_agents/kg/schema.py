"""Hub schema (definition D4) and per-project ontology extensions.

A project's ``ontology.yaml`` may only add: new node kinds, new relations, new properties. It may not
redefine a hub kind or relation, or declare two existing things equal; that keeps every extension
conservative, so queries written against the hub schema mean the same thing in every project.
"""

from __future__ import annotations

from evo_agents.schema import Issue

HUB_KINDS = {
    "Source": "a declared source of one project",
    "Document": "a document item: markdown file, wiki page, docx",
    "Section": "a heading-delimited part of a document",
    "File": "a file item that is not prose: code, YAML, config",
    "Directory": "a directory of a repo that holds at least one item",
    "Repo": "a repository declared in harness.yaml",
    "Symbol": "a class, function or method",
    "Commit": "a commit, referenced as repo@sha",
    "Plan": "an exec-plan",
    "PlanStep": "one step of an exec-plan",
    "Seam": "a contract seam from contracts.yaml",
    "Requirement": "a requirement code",
    "UseCase": "a use case code",
    "Ticket": "a ticket or issue",
    "Person": "a person",
}

HUB_RELATIONS = {
    "in_source": ("*", "Source"),
    "child_of": ("*", "*"),
    "part_of": ("Section", "*"),
    "declares": ("File", "*"),
    "owned_by": ("Seam", "Repo"),
    "consumed_by": ("Seam", "Repo"),
    "defined_in": ("*", "*"),
    "has_step": ("Plan", "PlanStep"),
    "depends_on": ("*", "*"),
    "in_repo": ("*", "Repo"),
    "touches": ("Plan", "*"),
    "defines": ("*", "*"),
    "member_of": ("Symbol", "Symbol"),
    "imports": ("File", "File"),
    "calls": ("Symbol", "Symbol"),
    "mentions": ("*", "*"),
    "implements": ("*", "*"),
    "verifies": ("*", "*"),
    "schedules": ("*", "*"),
}

ITEM_KIND_TO_NODE = {
    "markdown": "Document",
    "docx": "Document",
    "doc": "Document",
    "text": "Document",
    "html": "Document",
    "wiki": "Document",
    "page": "Document",
}

FORBIDDEN_ONTOLOGY_KEYS = {"same_as", "equivalent", "equals", "identify"}


def node_kind_for_item(item_kind: str) -> str:
    return ITEM_KIND_TO_NODE.get(item_kind, "File")


def _entry_name(entry) -> str | None:
    """An ontology entry is a bare name or a mapping with ``name``."""
    name = entry.get("name") if isinstance(entry, dict) else entry
    return name if isinstance(name, str) and name else None


def validate_ontology(data: dict) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(data, dict):
        return [Issue("", "ontology must be a mapping")]
    for key in data:
        if key in FORBIDDEN_ONTOLOGY_KEYS:
            issues.append(Issue(key, "an extension may not declare two existing things equal"))
    for key, hub, clash in (
        ("node_kinds", HUB_KINDS, "is a hub kind; extend it with properties instead"),
        ("relations", HUB_RELATIONS, "is a hub relation"),
    ):
        entries = data.get(key) or []
        if not isinstance(entries, list):
            issues.append(Issue(key, "must be a list of names or of mappings with a name"))
            continue
        for i, entry in enumerate(entries):
            name = _entry_name(entry)
            if name is None:
                issues.append(Issue(f"{key}[{i}]", "needs a name"))
            elif name in hub:
                issues.append(Issue(f"{key}[{i}]", f"{name!r} {clash}"))
    return issues


def known_kinds(ontology: dict | None) -> set[str]:
    kinds = set(HUB_KINDS)
    entries = (ontology or {}).get("node_kinds") or []
    if isinstance(entries, list):
        kinds.update(name for name in map(_entry_name, entries) if name)
    return kinds
