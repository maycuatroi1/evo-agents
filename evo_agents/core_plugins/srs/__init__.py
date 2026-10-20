"""The core plugin srs: a project's BA documents on the hub, with versions, coded items and approval (criterion a2 of
plan srs-tasks-plugins; ``docs/plugins.md``, Core plugins).

A document is an SRS written to a template (``srs-basic`` by default) or a free specification. Every part of it is an
item with a key the hub gives and keeps: sections, use cases (``UC ORD-3``), business rules (``BR ORD3-2``), screens
(``SCR-4``), non-functional requirements (``NFR-SEC-01``) and common business rules (``CBR 2``). A code is unique in the
project, aliases included, stays when the item is renamed or moved, and is never given again once the item is dropped.

Each save is a version, never changed after, in one straight line per document: draft, in review, approved, and
superseded once a newer version is approved; or withdrawn, for good, when its author or an admin takes it out of
review. A writer submits a version; a writer who wrote no version since the one approved last approves it or asks for
changes, as many times per role as the config's ``approval_policy`` asks (one approval when it asks nothing). Two
versions compare item by item. A Markdown file whose headings follow the template imports as a document or as its
next version, and a version exports as Markdown with the same structure and codes.

The routes are under /v1/x/srs/<project>/ (``server``) for members with a grant on a project that enabled the plugin;
readers read, writers write, admins drop coded items (by their codes too: ``retire``), archive documents and
unarchive them. The hub's web shows them on the plugin's native pages (``PAGES``): the documents, a document's
contents with each section read and edited in place, an item's page with the trace the knowledge graph holds, the
versions, the comparison of two versions, the review and the comments.
``evo-agents hub srs list|show|import|export|submit|approve|withdraw|retire|archive|unarchive`` is their client. The
tables are the hub's (``evo_agents.hub.tables_srs``, migrations 0046 and 0064), every row of a document carrying its
label, the project's by default. The version of each document approved last, of the documents not archived, is a
source of the project's knowledge graph (``kg``): its sections, and its use cases and requirements as nodes with the
ids a mention of their codes gets.

The plugin contributes the agent type ``ba`` (criterion a7): the profile @ba with the pack ``srs/ba`` (``packs/ba``:
its instructions and the skills srs-writing and srs-review), which a project's writers mention in a group chat. Its
agent reads, searches and compares documents and proposes and comments with the plugin's tools on the hub's /mcp
(``agent``): a proposal is the document's next version, in review at once, written as the person who mentioned @ba,
who therefore never approves it. The type declares ``chat_runs: run_as``: every run of @ba goes to a worker of the
member the project enabled the type under, and each version that enters review for the first time is reviewed by @ba
in a task run on that worker (``auto_review``), whose comments are advice and block nothing.

The hub imports this package for the manifest only: ``template`` and ``markdown`` read and write documents,
``checks`` finds what is wrong with one, ``store`` and ``writes`` keep them, ``reviews`` decides versions and keeps
comments, ``diff`` compares two versions, ``models`` and ``server`` are the API, ``kg`` gives the knowledge graph its
approved versions, ``agent`` and ``auto_review`` are @ba's.
"""

SERVER = "evo_agents.core_plugins.srs.server"
KG = "evo_agents.core_plugins.srs.kg"
AGENT = "evo_agents.core_plugins.srs.agent"
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

# @ba, the agent type (``agent``, ``auto_review``, ``packs/ba``)
BA = "ba"
BA_INSTRUCTIONS = (
    "You are @ba, the business analyst agent of this project's SRS documents on the evo-agents hub. You read, search, "
    "draft and review them with the tools srs__read, srs__search, srs__diff, srs__propose and srs__comment, and make "
    "tasks of their items with tasks__create when the project has it. A change is a proposal: a version in review that "
    "members approve. You never approve, return, drop or renumber anything. Follow the instructions of your pack."
)
_SLUG = {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,99}$", "description": "the document's slug"}
_VERSION = {"type": "integer", "minimum": 1, "description": "a version's number; the latest when left out"}
_REF = {"type": "string", "minLength": 1, "maxLength": 100, "description": "an item's code, an alias or its key"}
READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document": {**_SLUG, "description": "the document's slug; left out: the documents of the project"},
        "version": _VERSION,
        "item": {**_REF, "description": "an item's code, an alias or its key: that item and the items under it"},
    },
}
SEARCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 200, "description": "a code, an alias or words"},
        "document": {**_SLUG, "description": "one document's slug; left out: every document"},
        "kinds": {
            "type": "array",
            "items": {"enum": ["section", "use_case", "business_rule", "screen", "nfr", "cbr", "glossary"]},
            "maxItems": 7,
            "description": "the kinds of item to keep",
        },
    },
}
DIFF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "from", "to"],
    "properties": {
        "document": _SLUG,
        "from": {"type": "integer", "minimum": 1},
        "to": {"type": "integer", "minimum": 1},
    },
}
_NEW_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "title"],
    "properties": {
        "kind": {"enum": ["section", "use_case", "business_rule", "screen", "nfr", "cbr", "glossary"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 500},
        "body": {"type": "string", "maxLength": 200_000},
        "code": {"type": "string", "minLength": 1, "maxLength": 100, "description": "a new code the template allows"},
        "parent": {"type": "string", "description": "the key of its parent"},
        "after": {"type": "string", "description": "the key of the sibling it follows"},
        "heading_level": {"type": "integer", "minimum": 1, "maximum": 6},
        "steps": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
    },
}
_CHANGE = {
    "type": "object",
    "additionalProperties": False,
    "description": "a change of the item key (its title, body, steps or links), or a new item",
    "properties": {
        "key": {"type": "string", "minLength": 1, "maxLength": 64, "description": "the key of the item it changes"},
        "title": {"type": "string", "minLength": 1, "maxLength": 500},
        "body": {"type": "string", "maxLength": 200_000, "description": "the item's whole Markdown body"},
        "steps": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
        "refs": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["rel", "to"],
                "properties": {
                    "rel": {"enum": ["uses_screen", "refines", "relates"]},
                    "to": {"type": "string", "minLength": 1, "maxLength": 100},
                },
            },
            "description": "the item's links, the whole list",
        },
        "new": _NEW_ITEM,
    },
}
PROPOSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "base_version", "summary", "changes"],
    "properties": {
        "document": _SLUG,
        "base_version": {"type": "integer", "minimum": 1, "description": "the version the changes were made on"},
        "summary": {"type": "string", "minLength": 1, "maxLength": 500, "description": "what it changes and why"},
        "changes": {"type": "array", "minItems": 1, "maxItems": 200, "items": _CHANGE},
    },
}
COMMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document", "body"],
    "properties": {
        "document": _SLUG,
        "body": {"type": "string", "minLength": 1, "maxLength": 20_000},
        "version": {"type": "integer", "minimum": 1, "description": "the version it is about"},
        "item": {**_REF, "description": "the item it is about: its code, an alias or its key"},
        "parent_id": {"type": "integer", "minimum": 1, "description": "the comment it answers"},
    },
}
TOOLS = (
    (
        "read",
        "Read the SRS documents",
        "Read a BA document of the project: without document, the list of documents; with it, a version as Markdown "
        "(the latest unless version names one) with its state; with item, that item and the items under it.",
        READ_SCHEMA,
    ),
    (
        "search",
        "Search the SRS items",
        "Find items of the latest version of each document by code, alias or words of their title or body.",
        SEARCH_SCHEMA,
    ),
    ("diff", "Compare two versions", "What changed between two versions of a document, item by item.", DIFF_SCHEMA),
    (
        "propose",
        "Propose a new version",
        "Propose changes of a document's items as its next version, which goes to review at once and which members "
        "approve. Only the agent of a chat run of @ba may call it; it never drops an item or changes a code.",
        PROPOSE_SCHEMA,
    ),
    (
        "comment",
        "Comment on a document",
        "Comment on a document, a version or an item, as the member the run works for, marked as drafted by the run. "
        "Only the agent of a run of @ba may call it; a comment is advice and blocks no approval.",
        COMMENT_SCHEMA,
    ),
)

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

# The native pages of the hub's web (web/src/app/(app)/p/[project]/x/srs, web/src/components/srs): the documents,
# with a document's contents, items, versions, comparisons and reviews under it, in the project's menu; the import,
# reached from the documents.
PAGES = {
    "documents": {"title": "SRS", "icon": "file-text", "description": "The project's BA documents"},
    "import": {"title": "Import SRS", "icon": "file-text", "menu": False},
}

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
        "agent_packs": [
            {
                "name": BA,
                "title": "BA",
                "description": "Writing and reviewing SRS documents to their template, keeping every code.",
            }
        ],
        "agent_types": [
            {
                "name": BA,
                "title": "BA",
                "description": "Drafts, reviews and checks the SRS documents for the project's writers; members approve.",
                "mentioned_by": "writer",
                "chat_runs": "run_as",
                "packs": [BA],
                "profile": {
                    "handle": BA,
                    "name": "BA",
                    "description": "Drafts and reviews the SRS documents; every change is a version members approve.",
                    "instructions": BA_INSTRUCTIONS,
                },
            }
        ],
        "mcp_tools": [
            {"name": name, "title": title, "description": text, "python": f"{AGENT}:{name}", "input_schema": schema}
            for name, title, text, schema in TOOLS
        ],
        "routes": [{"name": "documents", "title": "The BA documents of a project", "python": f"{SERVER}:router"}],
        "kg_sources": [
            {"name": "documents", "title": "The approved version of each document", "python": f"{KG}:kg_source"}
        ],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
        "pages": [{"name": name, **page} for name, page in PAGES.items()],
    },
}
