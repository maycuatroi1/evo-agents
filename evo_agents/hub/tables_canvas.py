"""The tables of the core plugin canvas (0067), which ``evo_agents.hub.tables`` adds to its metadata as
``tables.canvas``: queries name them there (``tables.canvas.claims``). The plugin's code is
``evo_agents.core_plugins.canvas``; the hub owns the tables because its one chain of migrations creates them
(``docs/plugins.md``, Core plugins).

- canvas_canvases: the Lean Canvas of one product of a project: its slug (unique in the project), the product's name,
  a description, the label every row of it carries (the project's by default), who made it, and when an admin
  archived it.
- canvas_claims: each claim of a canvas as it is now: its number (1 and up within the project, shown ``CL-<n>`` and
  never given again), its cell (one of the nine of a Lean Canvas), its place in the cell, its text, status, confidence
  and test card, its rev (1 and up, one more on every change) and when it was archived.
- canvas_claim_revs: every rev of every claim, never changed: the whole content of the claim at that rev, what kind of
  change made it (``undo_of`` names the rev an undo took back), and its provenance: a person, an agent or the hub, the
  member the write was made for, the run with its runtime and model, the chat message that asked for it, and why.
- canvas_evidence: the evidence of a claim, only ever added: its kind, which way it points, a summary, a link, the day
  it was taken, whether it is synthetic (always, for a simulation), its provenance, the writer who confirmed what an
  agent added, and when and why it was withdrawn. Evidence is no part of a rev: adding some leaves the claim's rev.
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
from sqlalchemy.dialects.postgresql import JSONB

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


@dataclass(frozen=True)
class CanvasTables:
    """The tables of the core plugin canvas, by what each holds."""

    canvases: Table
    claims: Table
    revs: Table
    evidence: Table


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
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "chat_message_id", "chat_messages", ondelete="SET NULL"),
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
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "chat_message_id", "chat_messages", ondelete="SET NULL"),
        _key(name, "confirmed_by", "users", ondelete="RESTRICT"),
        _key(name, "withdrawn_by", "users", ondelete="RESTRICT"),
        PrimaryKeyConstraint("id", name="canvas_evidence_pkey"),
        Index("canvas_evidence_claim_idx", "claim_id", "id"),
    )


def canvas_tables(metadata: MetaData) -> CanvasTables:
    """The tables of the core plugin canvas, added to ``metadata`` (which holds projects, users, runs and
    chat_messages already)."""
    return CanvasTables(
        canvases=_canvases(metadata),
        claims=_claims(metadata),
        revs=_revs(metadata),
        evidence=_evidence(metadata),
    )
