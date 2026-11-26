"""The values ``evo-agents hub canvas`` declares: the choices of its options and the keys of what ``--json`` prints,
each the properties of the OpenAPI component the hub answers with."""

from __future__ import annotations

from evo_agents.clikit import Arg

CELLS = (
    "problem",
    "customer_segments",
    "unique_value_proposition",
    "solution",
    "channels",
    "revenue_streams",
    "cost_structure",
    "key_metrics",
    "unfair_advantage",
)
STATUSES = ("assumption", "testing", "validated", "invalidated", "retired")
CONFIDENCES = ("low", "medium", "high")
DELIVERIES = ("code", "action")  # tables_canvas.DELIVERIES
ADDED_KINDS = ("interview", "experiment", "market", "doc", "simulation")
DIRECTIONS = ("supports", "contradicts", "neutral")
SUMMARY_KEYS = (
    "slug",
    "name",
    "description",
    "label",
    "created_by",
    "created_at",
    "updated_at",
    "archived_at",
    "claims",
    "statuses",
)
DETAIL_KEYS = (*SUMMARY_KEYS, "cells")
CLAIM_KEYS = (
    "code",
    "number",
    "cell",
    "position",
    "text",
    "status",
    "confidence",
    "test_card",
    "archived",
    "use_cases",
    "signal_keys",
    "problems",
    "opportunities",
    "delivery",
    "rev",
    "evidence_count",
    "real_support_count",
    "label",
    "created_by",
    "created_at",
    "updated_at",
    "archived_at",
)
CLAIM_DETAIL_KEYS = (*CLAIM_KEYS, "canvas", "revs", "evidence", "hidden_evidence")
EDIT_KEYS = ("claims", "written")
EVIDENCE_KEYS = (
    "id",
    "kind",
    "direction",
    "summary",
    "url",
    "observed_on",
    "synthetic",
    "provenance",
    "created_at",
    "confirmed_by",
    "confirmed_at",
    "withdrawn_at",
    "withdrawn_by",
    "withdrawn_reason",
    "real",
    "not_real_because",
    "basis",
    "rests_on_run",
)
PROPOSAL_STATUSES = ("open", "accepted", "rejected", "stale")
PROPOSAL_KEYS = (
    "id",
    "kind",
    "claim",
    "base_rev",
    "fields",
    "changes",
    "reason",
    "synthetic",
    "provenance",
    "created_at",
    "status",
    "rev",
    "decided_by",
    "decided_at",
    "decided_reason",
    "evidence_id",
)
BRAINSTORM_KEYS = ("run_id", "title")
BASELINE_SUMMARY_KEYS = (
    "number",
    "kind",
    "week",
    "due_at",
    "time_zone",
    "status",
    "created_by",
    "created_at",
    "approved_at",
    "expired_at",
    "item_count",
    "waiting",
)
BASELINE_KEYS = (*BASELINE_SUMMARY_KEYS, "canvas", "items", "approved_by", "snapshot")
BASELINE_DECISIONS = (
    "accepted",
    "deferred",
    "extended",
    "rejected",
    "retired",
)  # canvas baseline_models.MEMBER_DECISIONS
IMPORT_KEYS = ("dry_run", "canvas", "canvas_created", "created", "edited", "unchanged", "evidence", "problems")


PROJECT = Arg("--project", required=True, metavar="PROJECT", help="the project that enabled plugin canvas")
SLUG = Arg("slug", metavar="SLUG", help="the canvas's slug")
CLAIM = Arg("claim", metavar="CODE", help="the claim, as CL-3")
REASON = Arg("--reason", metavar="TEXT", help="why, kept with the rev")
