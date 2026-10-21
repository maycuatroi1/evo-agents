"""The tables of the core plugin canvas: Lean Canvases with their claims, every rev of each claim, and evidence.

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_canvas`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

The plugin is ``evo_agents.core_plugins.canvas``; every table carries its prefix. canvas_canvases holds the canvas of
each product of a project, by a slug unique in the project, with the label every row of it carries; canvas_claims each
claim as it is now, numbered within the project (``CL-<n>``), in one of the nine cells, with its status, confidence,
test card and rev; canvas_claim_revs every rev of a claim with its whole content and its provenance, an undo naming
the rev it took back; canvas_evidence the evidence of a claim, kept when withdrawn, a simulation always synthetic.

Going back drops the four tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

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
CHANGES = ("create", "edit", "archive", "unarchive", "undo", "import", "proposal", "baseline")
ACTORS = ("person", "agent", "hub")
EVIDENCE_KINDS = ("signal", "feedback", "interview", "experiment", "market", "doc", "simulation")
DIRECTIONS = ("supports", "contradicts", "neutral")
SLUG = r"^[a-z0-9][a-z0-9-]{0,99}$"
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 10_000
MAX_TEXT_CHARS = 2_000
MAX_REASON_CHARS = 1_000
MAX_SUMMARY_CHARS = 4_000
MAX_URL_CHARS = 2_000

col = sa.column
TABLES = ("canvas_evidence", "canvas_claim_revs", "canvas_claims", "canvas_canvases")  # in the order going back drops
INDEXES = (  # (index, table), dropped before the tables
    ("canvas_evidence_claim_idx", "canvas_evidence"),
    ("canvas_claims_canvas_idx", "canvas_claims"),
)


def _key(table: str, column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _length(name: str, low: int, high: int):
    return sa.func.char_length(col(name)).between(low, high)


def _stamp(name: str):
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _object(name: str):
    return sa.func.jsonb_typeof(col(name)) == "object"


def _provenance() -> list:
    """The columns of who wrote a rev or evidence: a person, an agent or the hub; the member it was for, the run with
    its runtime and model, and the chat message that asked for it."""
    return [
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("runtime", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("chat_message_id", sa.BigInteger(), nullable=True),
    ]


def _provenance_check():
    return sa.and_(col("actor").in_(ACTORS), sa.or_(col("actor") == "hub", col("user_id").is_not(None)))


def _canvases() -> None:
    name = "canvas_canvases"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_by", sa.BigInteger(), nullable=True),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        _key(name, "archived_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="canvas_canvases_pkey"),
        sa.UniqueConstraint("project_id", "slug", name="canvas_canvases_slug_key"),
        sa.CheckConstraint(
            sa.and_(
                col("slug").regexp_match(SLUG),
                _length("name", 1, MAX_NAME_CHARS),
                sa.func.char_length(col("description")) <= MAX_DESCRIPTION_CHARS,
                col("archived_at").is_(None) == col("archived_by").is_(None),
            ),
            name="canvas_canvases_check",
        ),
    )


def _claims() -> None:
    name = "canvas_claims"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("canvas_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("cell", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="assumption", nullable=False),
        sa.Column("confidence", sa.Text(), server_default="medium", nullable=False),
        sa.Column("test_card", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rev", sa.Integer(), server_default="1", nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        _stamp("created_at"),
        _stamp("updated_at"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        _key(name, "project_id", "projects", ondelete="RESTRICT"),
        _key(name, "canvas_id", "canvas_canvases", ondelete="CASCADE"),
        _key(name, "created_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="canvas_claims_pkey"),
        sa.UniqueConstraint("project_id", "number", name="canvas_claims_number_key"),
        sa.CheckConstraint(
            sa.and_(
                col("number") >= 1,
                col("cell").in_(CELLS),
                col("position") >= 0,
                _length("text", 1, MAX_TEXT_CHARS),
                col("status").in_(STATUSES),
                col("confidence").in_(CONFIDENCES),
                _object("test_card"),
                col("rev") >= 1,
            ),
            name="canvas_claims_check",
        ),
    )
    op.create_index("canvas_claims_canvas_idx", name, ["canvas_id", "cell", "position"])


def _revs() -> None:
    name = "canvas_claim_revs"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change", sa.Text(), nullable=False),
        sa.Column("undo_of", sa.Integer(), nullable=True),
        *_provenance(),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        _stamp("created_at"),
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "chat_message_id", "chat_messages", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="canvas_claim_revs_pkey"),
        sa.UniqueConstraint("claim_id", "rev", name="canvas_claim_revs_rev_key"),
        sa.CheckConstraint(
            sa.and_(
                col("rev") >= 1,
                _object("content"),
                col("change").in_(CHANGES),
                (col("change") == "undo") == col("undo_of").is_not(None),
                col("undo_of") < col("rev"),
                _provenance_check(),
                sa.func.char_length(col("reason")) <= MAX_REASON_CHARS,
            ),
            name="canvas_claim_revs_check",
        ),
    )


def _evidence() -> None:
    name = "canvas_evidence"
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("observed_on", sa.Date(), nullable=True),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_provenance(),
        _stamp("created_at"),
        sa.Column("confirmed_by", sa.BigInteger(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_by", sa.BigInteger(), nullable=True),
        sa.Column("withdrawn_reason", sa.Text(), nullable=True),
        _key(name, "claim_id", "canvas_claims", ondelete="CASCADE"),
        _key(name, "user_id", "users", ondelete="RESTRICT"),
        _key(name, "run_id", "runs", ondelete="SET NULL"),
        _key(name, "chat_message_id", "chat_messages", ondelete="SET NULL"),
        _key(name, "confirmed_by", "users", ondelete="RESTRICT"),
        _key(name, "withdrawn_by", "users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="canvas_evidence_pkey"),
        sa.CheckConstraint(
            sa.and_(
                col("kind").in_(EVIDENCE_KINDS),
                col("direction").in_(DIRECTIONS),
                _length("summary", 1, MAX_SUMMARY_CHARS),
                _length("url", 1, MAX_URL_CHARS),
                sa.or_(col("kind") != "simulation", col("synthetic")),
                _provenance_check(),
                col("confirmed_by").is_(None) == col("confirmed_at").is_(None),
                col("withdrawn_at").is_(None) == col("withdrawn_reason").is_(None),
                _length("withdrawn_reason", 1, MAX_REASON_CHARS),
            ),
            name="canvas_evidence_check",
        ),
    )
    op.create_index("canvas_evidence_claim_idx", name, ["claim_id", "id"])


def upgrade() -> None:
    _canvases()
    _claims()
    _revs()
    _evidence()


def downgrade() -> None:
    for index, table in INDEXES:
        op.drop_index(index, table_name=table)
    for name in TABLES:
        op.drop_table(name)
