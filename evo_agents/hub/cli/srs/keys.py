"""The values ``evo-agents hub srs`` declares: the choices of its options, the keys of what ``--json`` prints (each the
properties of the OpenAPI component the hub answers with) and the arguments its commands share."""

from __future__ import annotations

from evo_agents.clikit import Arg

ROLES = ("ba", "qa", "dev")
KINDS = ("srs", "spec")
SUMMARY_KEYS = (
    "slug",
    "title",
    "kind",
    "template",
    "approved_version",
    "latest_version",
    "latest_state",
    "label",
    "created_by",
    "created_at",
    "archived_at",
)
DOCUMENT_KEYS = (*SUMMARY_KEYS, "versions", "shown")
ITEM_KEYS = (
    "key",
    "kind",
    "code",
    "aliases",
    "title",
    "parent",
    "heading_level",
    "fields",
    "body",
    "refs",
    "rev",
    "released",
)
ITEM_DETAIL_KEYS = (*ITEM_KEYS, "document", "version", "path", "children", "revisions")
IMPORT_KEYS = ("document", "created", "version", "dry_run", "preview")
RETIRE_KEYS = ("document", "version", "dry_run", "dropped")
FINDING_STATUSES = ("open", "resolved", "waived")
FINDING_KEYS = (
    "id",
    "version",
    "item",
    "item_code",
    "kind",
    "level",
    "sources",
    "summary",
    "status",
    "resolution",
    "resolution_sources",
    "settled_by",
    "settled_at",
    "chat_link",
    "run_id",
    "created_at",
    "options",
)
VERSION_KEYS = (
    "number",
    "parent_number",
    "state",
    "round",
    "author",
    "drafted_by_run",
    "summary",
    "created_at",
    "submitted_at",
    "decided_at",
    "review_run",
    "reviews",
)
USE_CASE_VERSION_KEYS = (*VERSION_KEYS, "use_cases")
SUBMITTED_KEYS = (*USE_CASE_VERSION_KEYS, "use_case", "use_case_review_run")
QUEUE_KEYS = (*USE_CASE_VERSION_KEYS, "document", "document_title", "held_by", "deciding", "may_review")

COMMENT_KEYS = (
    "id",
    "version",
    "item",
    "item_code",
    "parent_id",
    "author",
    "run_id",
    "body",
    "created_at",
    "resolved_by",
    "resolved_at",
    "field",
    "quote",
    "prefix",
    "suffix",
    "start_offset",
    "anchor",
    "ask_run_id",
)

PROJECT = Arg("--project", required=True, metavar="PROJECT", help="the project that enabled plugin srs")
SLUG = Arg("slug", metavar="SLUG")
NUMBER = Arg("number", type=int, metavar="N")
OPTIONAL_NUMBER = Arg("number", type=int, metavar="N", nargs="?", help="the version (default: see the command)")
