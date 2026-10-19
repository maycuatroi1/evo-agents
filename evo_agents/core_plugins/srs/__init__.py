"""The core plugin srs: a project's BA documents on the hub, with versions, coded items and approval (criterion a2 of
plan srs-tasks-plugins; ``docs/plugins.md``, Core plugins).

A document is an SRS written to a template (``srs-basic`` by default) or a free specification. Every part of it is an
item with a key the hub gives and keeps: sections, use cases (``UC ORD-3``), business rules (``BR ORD3-2``), screens
(``SCR-4``), non-functional requirements (``NFR-SEC-01``) and common business rules (``CBR 2``). A code is unique in the
project, aliases included, stays when the item is renamed or moved, and is never given again once the item is dropped.

Each save is a version, never changed after, in one straight line per document: draft, in review, approved, and
superseded once a newer version is approved. A writer submits a version; a writer who wrote no version since the one
approved last approves it or asks for changes, as many times per role as the config's ``approval_policy`` asks
(one approval when it asks nothing). Two versions compare item by item. A Markdown file whose headings follow the
template imports as a document or as its next version, and a version exports as Markdown with the same structure and
codes.

The routes are under /v1/x/srs/<project>/ (``server``) for members with a grant on a project that enabled the plugin;
readers read, writers write, admins drop coded items. ``evo-agents hub srs list|show|import|export|submit|approve``
is their client. The tables are the hub's (``evo_agents.hub.tables_srs``, migration 0044), every row of a document
carrying its label, the project's by default.

The hub imports this package for the manifest only: ``template`` and ``markdown`` read and write documents,
``checks`` finds what is wrong with one, ``store`` and ``writes`` keep them, ``reviews`` decides versions and keeps
comments, ``diff`` compares two versions, ``models`` and ``server`` are the API.
"""

SERVER = "evo_agents.core_plugins.srs.server"
TEMPLATES = ("srs-basic",)
ROLES = ("ba", "qa", "dev")  # evo_agents.hub.tables_srs.REVIEW_ROLES
CODED_KINDS = ("use_case", "business_rule", "screen", "nfr", "cbr")
DOCUMENT_KINDS = ("srs", "spec")  # evo_agents.hub.tables_srs.DOCUMENT_KINDS

_LOGINS = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 100}}
_ROLE_LIST = {"type": "array", "items": {"enum": list(ROLES)}}
_POLICY = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"required": _ROLE_LIST, "optional": _ROLE_LIST},
}

CONFIG = {
    "type": "object",
    "properties": {
        "template": {
            "title": "Template",
            "description": "The template documents are read and written with.",
            "enum": list(TEMPLATES),
            "default": TEMPLATES[0],
        },
        "code_patterns": {
            "title": "Code patterns",
            "description": "A regular expression per kind of coded item that replaces the template's, such as "
            '{"use_case": "^UC [A-Z]+-\\\\d+$"}.',
            "type": "object",
            "additionalProperties": False,
            "properties": {kind: {"type": "string", "minLength": 2, "maxLength": 200} for kind in CODED_KINDS},
        },
        "roles": {
            "title": "Roles",
            "description": "The members (logins) who hold each role in reviews: ba, qa and dev.",
            "type": "object",
            "additionalProperties": False,
            "properties": {role: _LOGINS for role in ROLES},
        },
        "approval_policy": {
            "title": "Approval policy",
            "description": "Per kind of document (srs, spec), the roles whose approval a version needs, such as "
            '{"srs": {"required": ["ba", "qa"], "optional": ["dev"]}}. Without one, one approval of a writer who '
            "wrote none of the versions under review is enough.",
            "type": "object",
            "additionalProperties": False,
            "properties": {kind: _POLICY for kind in DOCUMENT_KINDS},
        },
    },
}

TABLES = (
    ("srs_documents", "A project's BA documents and the label each carries"),
    ("srs_versions", "Each save of a document, never changed after, and its state"),
    ("srs_texts", "The Markdown bodies of items, once each by SHA-256"),
    ("srs_items", "The identity of an item across versions: key, kind and code"),
    ("srs_codes", "Every code and alias an item held, unique in the project"),
    ("srs_item_versions", "An item as one version has it"),
    ("srs_refs", "Links between the items of a version"),
    ("srs_reviews", "Approvals and requests for changes of a version"),
    ("srs_comments", "Comments on a document, a version or an item"),
)

MANIFEST = {
    "id": "srs",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "SRS",
    "description": "BA documents with versions and approval: use cases, business rules, screens and NFRs with codes.",
    "engines": {"evo-agents": ">=0.20"},
    "plugin_api": 1,
    "config": CONFIG,
    "contributes": {
        "routes": [{"name": "documents", "title": "The BA documents of a project", "python": f"{SERVER}:router"}],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
    },
}
