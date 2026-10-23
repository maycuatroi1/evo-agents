"""The tables of the core plugin canvas (0073, 0075, 0076, 0077, 0078, 0131), which ``evo_agents.hub.tables`` adds to its
metadata as ``tables.canvas``: queries name them there (``tables.canvas.claims``). The plugin's code is
``evo_agents.core_plugins.canvas``; the hub owns the tables because its one chain of migrations creates them
(``docs/plugins.md``, Core plugins).

- canvas_canvases: the Lean Canvas of one product of a project: its slug (unique in the project), the product's name,
  a description, the label every row of it carries (the project's by default), who made it, and when an admin
  archived it.
- canvas_claims: each claim of a canvas as it is now: its number (1 and up within the project, shown ``CL-<n>`` and
  never given again), its cell (one of the nine of a Lean Canvas), its place in the cell, its text, status, confidence
  and test card, its rev (1 and up, one more on every change) and when it was archived; and what it links (0078): the
  codes of the use cases of the plugin srs that realize it and the keys of the project's signals that measure it, and
  the codes of the problems of the project it answers (0131), each sorted, as its rev's content holds them.
- canvas_claim_revs: every rev of every claim, never changed: the whole content of the claim at that rev, what kind of
  change made it (``undo_of`` names the rev an undo took back), and its provenance: a person, an agent or the hub, the
  member the write was made for, the run with its runtime and model, the chat message that asked for it, and why.
- canvas_evidence: the evidence of a claim, only ever added: its kind, which way it points, a summary, a link, the day
  it was taken, whether it is synthetic (always, for a simulation), its provenance, the writer who confirmed what an
  agent added, and when and why it was withdrawn. Evidence is no part of a rev: adding some leaves the claim's rev.
- canvas_proposals (0075): a change of a claim at the rev it was read at, or a new claim, that an agent or the hub
  proposes instead of writing it (a task run of @strategist, the value check, later the Loop): the fields it sets,
  why, whether it rests on simulated users, its provenance, the evidence a value check proposed it on (0076), and what
  became of it: open, accepted by a writer (the claim and the rev that wrote it), rejected (by whom and why), or stale
  once the claim changed since its rev.
- canvas_judgings (0076): each item a judging run of @strategist puts before it for one claim: a piece of feedback of
  the signal key the claim's test card names, or a neutral interview, experiment or market figure of the claim; what
  the run read of it, its label, whether it is synthetic or rests on a signal a run pushed, when it was measured, the
  run (the judging pass), and once judged the verdict (supports, contradicts, irrelevant), why, and the evidence of
  kind feedback the hub wrote from it.

The value check (0076) also writes evidence: ``basis`` says what a piece the hub wrote rests on (the figures of a check
of the test card, or the item judged), and ``rests_on_run`` marks one that rests on signals the agent of a run pushed,
which never validates a claim.

- canvas_baselines (0077): the baselines of a canvas, numbered 1 and up within it: a weekly one the plugin's job
  proposes (its ISO week, unique in the canvas, the moment it was due and the time zone it was due in) or one a writer
  opens for a claim at once (``push``, by whom); proposed, approved once every item has a decision, or expired when the
  next week's came first; and once approved its snapshot, never changed: each claim not archived with its code, its
  rev, the status in effect and the SHA-256 of its content.
- canvas_baseline_items (0077): one claim of a baseline and what it asks: a change of status members made since the
  status in effect (``status``), a status the value check proposed (``value_check``, its proposal), a test card past
  its deadline without real evidence (``overdue``, extended or retired), or a new claim at assumption, included as it is
  (``new``); the status in effect, the status the claim was at, the one asked, its rev, its evidence, the members who
  changed its status since and so may not decide it, and the decision: who, when, why, a new deadline, the rev it wrote.

canvas_claim_revs.baseline_id (0077) names the baseline whose decision wrote a rev (change ``baseline``).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

CELLS = (  # canvas_claims.cell: the nine cells of a Lean Canvas, in the order a canvas shows them
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
STATUSES = ("assumption", "testing", "validated", "invalidated", "retired")  # canvas_claims.status
CONFIDENCES = ("low", "medium", "high")  # canvas_claims.confidence
CHANGES = (  # canvas_claim_revs.change
    "create",
    "edit",
    "archive",
    "unarchive",
    "undo",
    "import",
    "proposal",  # a writer accepted a proposal (plan living-lean-canvas, step 5)
    "baseline",  # the decision on an item of a baseline (step 6)
)
ACTORS = ("person", "agent", "hub")  # who wrote a rev or evidence
EVIDENCE_KINDS = ("signal", "feedback", "interview", "experiment", "market", "doc", "simulation")
DIRECTIONS = ("supports", "contradicts", "neutral")  # canvas_evidence.direction
SLUG = r"^[a-z0-9][a-z0-9-]{0,99}$"  # canvas_canvases.slug
MAX_NAME_CHARS = 200  # of a canvas's product
MAX_DESCRIPTION_CHARS = 10_000
MAX_TEXT_CHARS = 2_000  # of a claim
MAX_REASON_CHARS = 1_000  # of a rev, and of withdrawing evidence
MAX_SUMMARY_CHARS = 4_000  # of evidence
MAX_URL_CHARS = 2_000
PROPOSAL_KINDS = ("create", "change")  # canvas_proposals.kind: a new claim, or a change of one at its base_rev
PROPOSAL_STATUSES = ("open", "accepted", "rejected", "stale")  # canvas_proposals.status
VERDICTS = ("supports", "contradicts", "irrelevant")  # canvas_judgings.verdict
JUDGED_SOURCES = ("signal", "evidence")  # canvas_judgings.source: a piece of feedback, or a claim's evidence
JUDGED_EVIDENCE_KINDS = ("interview", "experiment", "market")  # the neutral evidence a judging run judges
MAX_JUDGED_TEXT_CHARS = 4_096  # of what a judging run reads of an item: feedback is at most 4 KiB in UTF-8
BASELINE_KINDS = ("weekly", "push")  # canvas_baselines.kind: the job's, or a writer's for one claim at once
BASELINE_STATUSES = ("proposed", "approved", "expired")  # canvas_baselines.status
ITEM_KINDS = ("status", "value_check", "overdue", "new")  # canvas_baseline_items.kind
DECISIONS = (  # canvas_baseline_items.decision
    "accepted",  # the status asked takes effect (status, value_check)
    "rejected",  # the status in effect stays, the claim goes back to it (status, value_check)
    "deferred",  # to the next baseline (any item a member decides)
    "extended",  # an overdue test card gets a new deadline (overdue)
    "retired",  # an overdue claim goes to retired (overdue)
    "included",  # a new claim at assumption, as it is, with no member deciding (new)
)
WEEK = r"^[0-9]{4}-W[0-9]{2}$"  # canvas_baselines.week: an ISO week, as 2026-W43
MAX_TIME_ZONE_CHARS = 64  # of canvas_baselines.time_zone, an IANA zone


@dataclass(frozen=True)
class CanvasTables:
    """The tables of the core plugin canvas, by what each holds."""

    canvases: Table
    claims: Table
    revs: Table
    evidence: Table
    proposals: Table
    judgings: Table
    baselines: Table
    baseline_items: Table


def _key(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _canvases(metadata: MetaData) -> Table:
    name = "canvas_canvases"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("slug", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("description", Text, nullable=False, server_default=""),
        Column("label", JSONB, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("archived_at", DateTime(timezone=True)),
        Column("archived_by", BigInteger),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        _key(name, "archived_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="canvas_canvases_pkey"),
        UniqueConstraint("project_id", "slug", name="canvas_canvases_slug_key"),
    )


def _claims(metadata: MetaData) -> Table:
    name = "canvas_claims"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("canvas_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("cell", Text, nullable=False),
        Column("position", Integer, nullable=False, server_default="0"),
        Column("text", Text, nullable=False),
        Column("status", Text, nullable=False, server_default="assumption"),
        Column("confidence", Text, nullable=False, server_default="medium"),
        Column("test_card", JSONB),
        Column("rev", Integer, nullable=False, server_default="1"),
        Column("label", JSONB, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("archived_at", DateTime(timezone=True)),
        Column("use_cases", ARRAY(Text), nullable=False, server_default="{}"),  # 0078
        Column("signal_keys", ARRAY(Text), nullable=False, server_default="{}"),  # 0078
        Column("problems", ARRAY(Text), nullable=False, server_default="{}"),  # 0131
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "canvas_id", "canvas_canvases", ondelete="CASCADE"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="canvas_claims_pkey"),
        UniqueConstraint("project_id", "number", name="canvas_claims_number_key"),
        Index("canvas_claims_canvas_idx", "canvas_id", "cell", "position"),
    )


def _revs(metadata: MetaData) -> Table:
    name = "canvas_claim_revs"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("claim_id", BigInteger, nullable=False),
        Column("rev", Integer, nullable=False),
        Column("content", JSONB, nullable=False),
        Column("change", Text, nullable=False),
        Column("undo_of", Integer),
        Column("actor", Text, nullable=False),
        Column("user_id", BigInteger),
        Column("run_id", BigInteger),
        Column("runtime", Text),
        Column("model", Text),
        Column("chat_message_id", BigInteger),
        Column("reason", Text, nullable=False, server_default=""),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("baseline_id", BigInteger),
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "chat_message_id", "chat_messages", ondelete="SET NULL"),
        _key(name, "baseline_id", "canvas_baselines", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="canvas_claim_revs_pkey"),
        UniqueConstraint("claim_id", "rev", name="canvas_claim_revs_rev_key"),
    )


def _evidence(metadata: MetaData) -> Table:
    name = "canvas_evidence"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("claim_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("direction", Text, nullable=False),
        Column("summary", Text, nullable=False),
        Column("url", Text),
        Column("observed_on", Date),
        Column("synthetic", Boolean, nullable=False, server_default=false()),
        Column("label", JSONB, nullable=False),
        Column("actor", Text, nullable=False),
        Column("user_id", BigInteger),
        Column("run_id", BigInteger),
        Column("runtime", Text),
        Column("model", Text),
        Column("chat_message_id", BigInteger),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("confirmed_by", BigInteger),
        Column("confirmed_at", DateTime(timezone=True)),
        Column("withdrawn_at", DateTime(timezone=True)),
        Column("withdrawn_by", BigInteger),
        Column("withdrawn_reason", Text),
        Column("basis", JSONB),
        Column("rests_on_run", Boolean, nullable=False, server_default=false()),
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "chat_message_id", "chat_messages", ondelete="SET NULL"),
        _key(name, "confirmed_by", "users", ondelete="RESTRICT"),
        _key(name, "withdrawn_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="canvas_evidence_pkey"),
        Index("canvas_evidence_claim_idx", "claim_id", "id"),
    )


def _proposals(metadata: MetaData) -> Table:
    name = "canvas_proposals"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("canvas_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("claim_id", BigInteger),
        Column("base_rev", Integer),
        Column("fields", JSONB, nullable=False),
        Column("reason", Text, nullable=False),
        Column("synthetic", Boolean, nullable=False, server_default=false()),
        Column("label", JSONB, nullable=False),
        Column("actor", Text, nullable=False),
        Column("user_id", BigInteger),
        Column("run_id", BigInteger),
        Column("runtime", Text),
        Column("model", Text),
        Column("chat_message_id", BigInteger),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("status", Text, nullable=False, server_default="open"),
        Column("rev", Integer),
        Column("decided_by", BigInteger),
        Column("decided_at", DateTime(timezone=True)),
        Column("decided_reason", Text),
        Column("evidence_id", BigInteger),
        _key(name, "canvas_id", "canvas_canvases", ondelete="CASCADE"),
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "chat_message_id", "chat_messages", ondelete="SET NULL"),
        _key(name, "decided_by", "users", ondelete="RESTRICT"),
        _key(name, "evidence_id", "canvas_evidence", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="canvas_proposals_pkey"),
        Index("canvas_proposals_canvas_idx", "canvas_id", "status", "id"),
        Index("canvas_proposals_claim_idx", "claim_id", "status"),
    )


def _judgings(metadata: MetaData) -> Table:
    name = "canvas_judgings"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("canvas_id", BigInteger, nullable=False),
        Column("claim_id", BigInteger, nullable=False),
        Column("run_id", BigInteger),
        Column("source", Text, nullable=False),
        Column("kind", Text, nullable=False),
        Column("key_id", BigInteger),
        Column("signal_id", BigInteger),
        Column("evidence_id", BigInteger),
        Column("text", Text, nullable=False),
        Column("synthetic", Boolean, nullable=False, server_default=false()),
        Column("rests_on_run", Boolean, nullable=False, server_default=false()),
        Column("label", JSONB, nullable=False),
        Column("measured_at", DateTime(timezone=True), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("verdict", Text),
        Column("reason", Text),
        Column("judged_at", DateTime(timezone=True)),
        Column("verdict_evidence_id", BigInteger),
        _key(name, "canvas_id", "canvas_canvases", ondelete="CASCADE"),
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "key_id", "signal_keys", ondelete="RESTRICT"),
        _key(name, "signal_id", "signals", ondelete="SET NULL"),
        _key(name, "evidence_id", "canvas_evidence", ondelete="SET NULL"),
        _key(name, "verdict_evidence_id", "canvas_evidence", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="canvas_judgings_pkey"),
        Index("canvas_judgings_claim_idx", "claim_id", "measured_at"),
        Index("canvas_judgings_run_idx", "run_id"),
    )


def _baselines(metadata: MetaData) -> Table:
    name = "canvas_baselines"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("canvas_id", BigInteger, nullable=False),
        Column("number", Integer, nullable=False),
        Column("kind", Text, nullable=False),
        Column("week", Text),
        Column("due_at", DateTime(timezone=True)),
        Column("time_zone", Text),
        Column("status", Text, nullable=False, server_default="proposed"),
        Column("label", JSONB, nullable=False),
        Column("created_by", BigInteger),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("approved_at", DateTime(timezone=True)),
        Column("expired_at", DateTime(timezone=True)),
        Column("snapshot", JSONB),
        _key(name, "canvas_id", "canvas_canvases", ondelete="CASCADE"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="canvas_baselines_pkey"),
        UniqueConstraint("canvas_id", "number", name="canvas_baselines_number_key"),
        UniqueConstraint("canvas_id", "week", name="canvas_baselines_week_key"),
        Index("canvas_baselines_status_idx", "canvas_id", "status"),
    )


def _baseline_items(metadata: MetaData) -> Table:
    name = "canvas_baseline_items"
    return Table(
        name,
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("baseline_id", BigInteger, nullable=False),
        Column("claim_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("from_status", Text, nullable=False),
        Column("at_status", Text, nullable=False),
        Column("to_status", Text),
        Column("claim_rev", Integer, nullable=False),
        Column("proposal_id", BigInteger),
        Column("deadline", Date),
        Column("evidence_ids", ARRAY(BigInteger), nullable=False, server_default="{}"),
        Column("proposers", ARRAY(BigInteger), nullable=False, server_default="{}"),
        Column("label", JSONB, nullable=False),
        Column("decision", Text),
        Column("decided_by", BigInteger),
        Column("decided_at", DateTime(timezone=True)),
        Column("new_deadline", Date),
        Column("reason", Text),
        Column("rev", Integer),
        _key(name, "baseline_id", "canvas_baselines", ondelete="CASCADE"),
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "proposal_id", "canvas_proposals", ondelete="SET NULL"),
        _key(name, "decided_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="canvas_baseline_items_pkey"),
        UniqueConstraint("baseline_id", "claim_id", name="canvas_baseline_items_claim_key"),
        Index("canvas_baseline_items_claim_idx", "claim_id"),
    )


def canvas_tables(metadata: MetaData) -> CanvasTables:
    """The tables of the core plugin canvas, added to ``metadata`` (which holds projects, users, runs and
    chat_messages already; signal_keys and signals, which canvas_judgings names, are added after them)."""
    return CanvasTables(
        canvases=_canvases(metadata),
        claims=_claims(metadata),
        revs=_revs(metadata),
        evidence=_evidence(metadata),
        proposals=_proposals(metadata),
        judgings=_judgings(metadata),
        baselines=_baselines(metadata),
        baseline_items=_baseline_items(metadata),
    )
