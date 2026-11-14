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
    "Commit": "a commit: one a git source read from its ref's history, or one prose names as repo@sha",
    "PullRequest": "a pull request of a repo on GitHub, as a git source of that repo read it",
    "Plan": "an exec-plan",
    "PlanStep": "one step of an exec-plan",
    "Seam": "a contract seam from contracts.yaml",
    "Requirement": "a requirement code",
    "UseCase": "a use case code",
    "Ticket": "a ticket or issue",
    "Person": "a person",
    "Chat": "a group chat of the hub",
    "ChatThread": "a thread of a group chat: its first message and the replies to it",
    "ChatMessage": "a message of a group chat, named by the hub, whose text only search reads",
    "Claim": "a claim of a Lean Canvas of the hub, by its code (CL-3): a hypothesis about a product, with its status",
    "SignalKey": "a key of a project's registry of signals on the hub: what a product measures, as kb.search.latency",
    "Problem": "a problem of a project on the hub, by its code (P-3): what hurts, before any solution",
    "Goal": "a goal of a project on the hub, by its code (G-1): what the Loop works toward",
    "Opportunity": "an opportunity of a project on the hub, by its code (O-3): what it could win, for whom, and why now",
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
    "inherits": ("Symbol", "Symbol"),  # a class to a class or interface it extends, implements or mixes in
    "overrides": ("Symbol", "Symbol"),  # a method to the method of the same name up its class's chain
    "mentions": ("*", "*"),
    "implements": ("*", "*"),
    "verifies": ("*", "*"),
    "schedules": ("*", "*"),
    "changes": ("Commit", "*"),  # a commit to a file or document it changed (a merge: against its first parent)
    "merged_as": ("PullRequest", "Commit"),  # a merged pull request to the commit that merged it
    "measured_by": ("Claim", "SignalKey"),  # a claim of a Lean Canvas to a signal key that measures it
    "addresses": ("*", "Problem"),  # a plan, a claim or a goal to a problem of the hub it answers
    "evidences": ("*", "Problem"),  # a line of evidence of the hub to the problem it shows hurts
    "answers": ("Opportunity", "Problem"),  # an opportunity of the hub to a problem of its own project it answers
    "refers_to": ("*", "*"),  # an entity a model stated to the one Symbol or File its name names (kg.entity_links)
    "implemented_by": ("*", "*"),  # an entity an agent mapped to the Directory, File or Symbol implementing it
    "synonym_of": ("*", "*"),  # two entities of one kind whose names are near in meaning (kg.entity_links)
    "merged_from": ("*", "*"),  # an entity to the document whose entity a model merged into it (kg.entity_merge)
}

ITEM_KIND_TO_NODE = {
    "markdown": "Document",
    "docx": "Document",
    "pdf": "Document",
    "pptx": "Document",
    "xlsx": "Document",
    "doc": "Document",
    "text": "Document",
    "html": "Document",
    "wiki": "Document",
    "page": "Document",
    "commit": "Commit",
    "pr": "PullRequest",
    "ticket": "Ticket",
    "chat": "Chat",
    "thread": "ChatThread",
    "message": "ChatMessage",
}

FORBIDDEN_ONTOLOGY_KEYS = {"same_as", "equivalent", "equals", "identify"}


def node_kind_for_item(item_kind: str) -> str:
    return ITEM_KIND_TO_NODE.get(item_kind, "File")


def entry_name(entry) -> str | None:
    """An ontology entry is a bare name or a mapping with ``name``."""
    name = entry.get("name") if isinstance(entry, dict) else entry
    return name if isinstance(name, str) and name else None


CARDINALITIES = ("one_to_one", "many_to_one", "one_to_many", "many_to_many")
# the rule keys an entry may carry (``evo_agents.kg.ontology_rules`` checks a graph against them); a hub kind or
# relation may be named in a mapping that carries only its own refinements, never a new domain or range
RELATION_RULES = ("cardinality", "acyclic")
KIND_RULES = ("keys", "required_props")


def _names(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def _rule_issues(where: str, entry: dict) -> list[Issue]:
    """What is wrong with the rule keys of one mapping entry: ``domain`` and ``range`` lists of kinds, ``cardinality``
    one of CARDINALITIES, ``acyclic`` a boolean, ``keys`` a list of property names or of lists of them (a composite
    key), ``required_props`` a list of property names."""
    issues = []
    for key in ("domain", "range", "required_props"):
        if key in entry and not _names(entry[key]):
            issues.append(Issue(f"{where}.{key}", "must be a list of names"))
    if "cardinality" in entry and entry["cardinality"] not in CARDINALITIES:
        issues.append(Issue(f"{where}.cardinality", f"must be one of {', '.join(CARDINALITIES)}"))
    if "acyclic" in entry and not isinstance(entry["acyclic"], bool):
        issues.append(Issue(f"{where}.acyclic", "must be true or false"))
    keys = entry.get("keys")
    if "keys" in entry and not (isinstance(keys, list) and all(isinstance(k, str) and k or _names(k) for k in keys)):
        issues.append(Issue(f"{where}.keys", "must be a list of property names or of lists of them"))
    return issues


def _refines_hub(entry, rules: tuple[str, ...]) -> bool:
    """Whether ``entry`` names a hub kind or relation only to add rules to it: a mapping of ``name`` and rule keys."""
    return isinstance(entry, dict) and len(entry) > 1 and set(entry) <= {"name", *rules}


def validate_ontology(data: dict) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(data, dict):
        return [Issue("", "ontology must be a mapping")]
    for key in data:
        if key in FORBIDDEN_ONTOLOGY_KEYS:
            issues.append(Issue(key, "an extension may not declare two existing things equal"))
    for key, hub, clash, rules in (
        ("node_kinds", HUB_KINDS, "is a hub kind; extend it with properties instead", KIND_RULES),
        ("relations", HUB_RELATIONS, "is a hub relation", RELATION_RULES),
    ):
        entries = data.get(key) or []
        if not isinstance(entries, list):
            issues.append(Issue(key, "must be a list of names or of mappings with a name"))
            continue
        for i, entry in enumerate(entries):
            name = entry_name(entry)
            if name is None:
                issues.append(Issue(f"{key}[{i}]", "needs a name"))
            elif name in hub and not _refines_hub(entry, rules):
                issues.append(Issue(f"{key}[{i}]", f"{name!r} {clash}"))
            if isinstance(entry, dict):
                issues += _rule_issues(f"{key}[{i}]", entry)
    return issues


def known_kinds(ontology: dict | None) -> set[str]:
    kinds = set(HUB_KINDS)
    entries = (ontology or {}).get("node_kinds") or []
    if isinstance(entries, list):
        kinds.update(name for name in map(entry_name, entries) if name)
    return kinds


def known_relations(ontology: dict | None) -> set[str]:
    """The hub relations and those the project ontology adds: what an edge a person declares may be."""
    rels = set(HUB_RELATIONS)
    entries = (ontology or {}).get("relations") or []
    if isinstance(entries, list):
        rels.update(name for name in map(entry_name, entries) if name)
    return rels
