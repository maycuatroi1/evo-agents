"""The core plugin srs: a project's BA documents on the hub, with versions, coded items and approval (criterion a2 of
plan srs-tasks-plugins; ``docs/plugins.md``, Core plugins).

A document is an SRS written to a template (``srs-basic`` by default) or a free specification. Every part of it is an
item with a key the hub gives and keeps: sections, use cases (``UC ORD-3``), business rules (``BR ORD3-2``), screens
(``SCR-4``), non-functional requirements (``NFR-SEC-01``) and common business rules (``CBR 2``). A code is unique in the
project, aliases included, stays when the item is renamed or moved, and is never given again once the item is dropped.

Each save is a version, never changed after, in one straight line per document: draft, in review, approved, and
superseded once a newer version is approved; or withdrawn, for good, when its author or an admin takes it out of
review. A writer submits a version; a writer who wrote no version since the one approved last approves it or asks for
changes, as many times per role as the config's ``approval_policy`` asks (one approval when it asks nothing). With
the config's ``self_approval`` on, the author may give one of those reviews too. Two versions compare item by item.
A Markdown file whose headings follow the template imports as a document or as its next version, and a version
exports as Markdown with the same structure and codes.

The routes are under /v1/x/srs/<project>/ (``server``) for members with a grant on a project that enabled the plugin;
readers read, writers write, admins drop coded items (by their codes too: ``retire``), archive documents and
unarchive them. The hub's web shows them on the plugin's native pages (``PAGES``): the documents, a document's
contents with each section read and edited in place, an item's page with the trace the knowledge graph holds, the
versions, the comparison of two versions, the review and the comments.
``evo-agents hub srs list|show|import|export|submit|approve|withdraw|retire|archive|unarchive|findings|waive`` is
their client. The tables are the hub's (``evo_agents.hub.tables_srs``, migrations 0046, 0064, 0069, 0070 and 0071),
every row of a document carrying its label, the project's by default. The version of each document approved last, of the documents not archived, is a
source of the project's knowledge graph (``kg``): its sections, and its use cases and requirements as nodes with the
ids a mention of their codes gets.

The plugin contributes the agent type ``ba`` (criterion a7): the profile @ba with the pack ``srs/ba`` (``packs/ba``:
its instructions and the skills srs-writing, srs-review, srs-settle and srs-tasks), which a project's writers mention
in a group chat. Its
agent reads, searches and compares documents and proposes and comments with the plugin's tools on the hub's /mcp
(``agent``): a proposal is the document's next version, in review at once, written as the person who mentioned @ba,
who therefore never approves it unless the project turned ``self_approval`` on. The type declares ``chat_runs:
run_as``: every run of @ba goes to a worker of the member the project enabled the type under. Each time a version
enters review, in every round, from a member's submit or from a proposal, @ba reviews it in a task run on that worker
(``auto_review``), while the type is on and within its cap; when it is not, the log says why and approval waits for no
review. The review compares each use case the version changes with every use case of every document, drafts included,
and with the project's knowledge graph (kg_search, kg_context), records each conflict it can show a source for as a
finding, settles from the graph what a plan's Decision line, an approved item or a newer document already answers, and
resolves the open findings the version settles. Its comments on the template are advice and block nothing.

A conflict @ba finds is a finding (``findings``, criteria a4 and a5 of plan srs-conflicts-and-tasks): the agent of a
run of @ba records it with its sources, which the hub looks up first, and resolves, waives or reopens it with the tool
srs__finding. Members read them (``evo-agents hub srs findings``), an admin of the project waives an open one
(``evo-agents hub srs waive``), and a version is not approved while its document has an open finding of level decide or
@ba's review of its round is still queued or running. When that review ends with findings of level decide open, the
hub opens a settle chat for the version (``settle``, criterion a7, through the plugin's events contribution
``reviews``): the people the rules name, @ba woken by every message, and anyone there settles; @ba records the
settling on the finding and proposes the next version as the author of the version the chat is for.

When a version is approved and the project enables the plugin tasks (``auto_tasks``, criterion a8), each use case new
in it gets a frame task linked to it, the open tasks of a use case that changed get a comment, and those of a use case
it dropped are cancelled; then @ba, in a task run, splits each frame task into child tasks with tasks__create, for the
member who approved the version. A second call for the same version does nothing (srs_approval_tasks).

The hub imports this package for the manifest only: ``template`` and ``markdown`` read and write documents,
``checks`` finds what is wrong with one, ``store`` and ``writes`` keep them, ``reviews`` decides versions and keeps
comments, ``findings`` keeps the conflicts and gates approval, ``diff`` compares two versions, ``models`` and
``server`` are the API, ``kg`` gives the knowledge graph its approved versions, ``agent``, ``auto_review`` and
``settle`` are @ba's, ``auto_tasks`` keeps the tasks of an approved version.
"""

SERVER = "evo_agents.core_plugins.srs.server"
KG = "evo_agents.core_plugins.srs.kg"
AGENT = "evo_agents.core_plugins.srs.agent"
SETTLE = "evo_agents.core_plugins.srs.settle"
TEMPLATES = ("srs-basic",)
ROLES = ("ba", "qa", "dev")  # evo_agents.hub.tables_srs.REVIEW_ROLES
CODED_KINDS = ("use_case", "business_rule", "screen", "nfr", "cbr")
# The fields of a use case the templates name, in their order (markdown.ITEM_FIELDS reads them): the classic ones
# (objective, pre- and post-condition), those of intent, context and expectation, or both.
USE_CASE_FIELDS = (
    "id",
    "status",
    "objective",
    "intent",
    "actor",
    "context",
    "trigger",
    "pre_condition",
    "post_condition",
    "expectation",
)
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
        "required_use_case_fields": {
            "title": "Required use case fields",
            "description": "The fields every use case fills, besides its actor, before its version is submitted, such "
            'as ["intent", "context", "expectation"]: a row of its table of items (Intent, Context, Expectation, '
            "Objective, Trigger, Pre-condition, Post-condition, Status, ID).",
            "type": "array",
            "uniqueItems": True,
            "items": {"enum": [field for field in USE_CASE_FIELDS if field != "actor"]},
            "default": [],
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
            "wrote none of the versions under review (any writer, with self_approval on) is enough.",
            "type": "object",
            "additionalProperties": False,
            "properties": {kind: _POLICY for kind in DOCUMENT_KINDS},
        },
        "self_approval": {
            "title": "Self-approval",
            "description": "Whether the author of a version may approve it or ask for changes, as any writer of the "
            "project may: on, a writer who wrote a version since the one approved last reviews it too, admins "
            "included. Every other rule stays: writers only, one review per member per round, roles and the "
            "approval policy, and the agent of a run never reviews. Off by default.",
            "type": "boolean",
            "default": False,
        },
    },
}

# @ba, the agent type (``agent``, ``auto_review``, ``packs/ba``)
BA = "ba"
BA_INSTRUCTIONS = (
    "You are @ba, the business analyst agent of this project's SRS documents on the evo-agents hub. You read, search, "
    "draft and review them with the tools srs__read, srs__search, srs__diff, srs__propose, srs__comment and "
    "srs__finding, and make tasks of their items with tasks__create when the project has it. A change is a proposal: a "
    "version in review that members approve. A conflict is a finding with its sources. You never approve, return, drop "
    "or renumber anything. Follow the instructions of your pack."
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
_SOURCE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "ref"],
    "properties": {
        "kind": {
            "enum": ["node", "code", "path", "version"],
            "description": "node: the id of a node of the project's knowledge graph (kg_search, kg_node); code: a code "
            "an item of the project holds, such as UC ORD-2; path: repo:path, or a path, a file or document node of "
            "the graph carries; version: a later version of this document, which only settles a finding",
        },
        "ref": {"type": "string", "minLength": 1, "maxLength": 300},
        "note": {"type": "string", "maxLength": 500, "description": "what the source says"},
    },
}
FINDING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "document"],
    "properties": {
        "action": {
            "enum": ["record", "resolve", "waive", "reopen"],
            "description": "record a finding (kind, level, summary, sources; version, item); resolve or waive an open "
            "one (finding, resolution; settled_by, chat_link, and sources to resolve it); reopen one settled within "
            "24 hours (finding)",
        },
        "document": _SLUG,
        "finding": {"type": "integer", "minimum": 1, "description": "the finding's id"},
        "version": {**_VERSION, "description": "the version it was found on; the latest when left out"},
        "item": {**_REF, "description": "the item it is about: its code, an alias or its key; left out: the document"},
        "kind": {"enum": ["duplicate", "contradiction", "kg_conflict", "overlap"]},
        "level": {
            "enum": ["info", "advisory", "decide"],
            "description": "decide: members must settle it, and it holds approval while open",
        },
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "the conflict in a sentence"},
        "sources": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": _SOURCE,
            "description": "what shows the conflict (record), or what settled it (resolve); each is looked up",
        },
        "resolution": {
            "type": "string",
            "minLength": 1,
            "maxLength": 10000,
            "description": "how it was settled, or why it is waived",
        },
        "settled_by": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "the login of the member who settled it; left out: settled from the graph by its sources",
        },
        "chat_link": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "description": "the link of the message that settled it",
        },
    },
}
TOOLS = (
    (
        "read",
        "Read the SRS documents",
        "Read a BA document of the project: without document, the list of documents; with it, a version as Markdown "
        "(the latest unless version names one) with its state and the document's open findings; with item, that item "
        "and the items under it.",
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
    (
        "finding",
        "Record or settle a conflict",
        "Record a conflict of a document as a finding with the sources that show it (graph nodes, codes, paths), each "
        "looked up first, or resolve, waive or reopen one. An open finding of level decide holds the approval of the "
        "document's versions. Only the agent of a run of @ba may call it.",
        FINDING_SCHEMA,
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
    ("srs_findings", "The conflicts found in a document, their sources and how each was settled"),
    ("srs_settle_chats", "The chat where members settle the conflicts of a version with @ba"),
    ("srs_approval_tasks", "What the approval of a version did to the project's tasks, and @ba's run that splits them"),
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
                "events": ["reviews"],
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
        "events": [
            {
                "name": "reviews",
                "title": "The end of @ba's review of a version",
                "description": "Opens the settle chat of a version whose review left findings of level decide open.",
                "on": ["task.result"],
                "python": f"{SETTLE}:review_ended",
            }
        ],
        "routes": [{"name": "documents", "title": "The BA documents of a project", "python": f"{SERVER}:router"}],
        "kg_sources": [
            {"name": "documents", "title": "The approved version of each document", "python": f"{KG}:kg_source"}
        ],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
        "pages": [{"name": name, **page} for name, page in PAGES.items()],
    },
}
