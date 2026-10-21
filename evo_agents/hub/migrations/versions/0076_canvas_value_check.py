"""The value check of the core plugin canvas: what the evidence the hub writes rests on, the evidence a proposal of the
hub was made on, and the items a judging run of @strategist judges.

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.tables_canvas`` as of this revision, never imports, so a later change
to them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

canvas_evidence gains ``basis``, an object saying what a piece the hub wrote rests on (the figures of a check of a
test card, or the item a judging run judged), and ``rests_on_run``, true when it rests on signals the agent of a run
pushed: only the hub's evidence carries either. canvas_proposals gains ``evidence_id``, the evidence a value check
proposed a change of status on. canvas_judgings keeps each item put before a judging run of @strategist for one
claim: a piece of feedback (a signal of the key the claim's test card names) or a neutral interview, experiment or
market figure of the claim; the text the run read, its label, whether it is synthetic or rests on a run's signal, when
it was measured, the run, and once judged its verdict, why, when, and the evidence of kind feedback written from it.

Going back drops the table and the columns.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

VERDICTS = ("supports", "contradicts", "irrelevant")
JUDGED_SOURCES = ("signal", "evidence")
JUDGED_EVIDENCE_KINDS = ("interview", "experiment", "market")
MAX_JUDGED_TEXT_CHARS = 4_096
MAX_REASON_CHARS = 1_000

col = sa.column
NAME = "canvas_judgings"
INDEXES = ("canvas_judgings_run_idx", "canvas_judgings_claim_idx")
EVIDENCE_CHECK = "canvas_evidence_basis_check"
PROPOSAL_KEY = "canvas_proposals_evidence_id_fkey"


def _key(column: str, target: str, *, ondelete: str):
    return sa.ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{NAME}_{column}_fkey")


def _length(name: str, low: int, high: int):
    return sa.func.char_length(col(name)).between(low, high)


def _judging_check():
    judged = col("verdict").is_not(None)
    return sa.and_(
        col("source").in_(JUDGED_SOURCES),
        # a piece of feedback is a signal of a key; evidence is a neutral interview, experiment or market figure
        sa.or_(
            sa.and_(col("source") == "signal", col("kind") == "feedback", col("key_id").is_not(None)),
            sa.and_(col("source") == "evidence", col("kind").in_(JUDGED_EVIDENCE_KINDS), col("signal_id").is_(None)),
        ),
        sa.or_(col("source") == "evidence", col("evidence_id").is_(None)),
        _length("text", 1, MAX_JUDGED_TEXT_CHARS),
        sa.func.jsonb_typeof(col("label")) == "object",
        sa.or_(col("verdict").is_(None), col("verdict").in_(VERDICTS)),
        # a verdict comes with why and when, and only a verdict writes evidence
        judged == col("reason").is_not(None),
        judged == col("judged_at").is_not(None),
        sa.or_(col("reason").is_(None), _length("reason", 1, MAX_REASON_CHARS)),
        sa.or_(judged, col("verdict_evidence_id").is_(None)),
    )


def upgrade() -> None:
    op.add_column("canvas_evidence", sa.Column("basis", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("canvas_evidence", sa.Column("rests_on_run", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_check_constraint(
        EVIDENCE_CHECK,
        "canvas_evidence",
        sa.and_(
            sa.or_(col("basis").is_(None), sa.func.jsonb_typeof(col("basis")) == "object"),
            # what the hub wrote alone rests on something it says, or on a run's signals
            sa.or_(col("actor") == "hub", sa.and_(col("basis").is_(None), sa.not_(col("rests_on_run")))),
        ),
    )
    op.add_column("canvas_proposals", sa.Column("evidence_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        PROPOSAL_KEY, "canvas_proposals", "canvas_evidence", ["evidence_id"], ["id"], ondelete="SET NULL"
    )
    op.create_table(
        NAME,
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("canvas_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key_id", sa.BigInteger(), nullable=True),
        sa.Column("signal_id", sa.BigInteger(), nullable=True),
        sa.Column("evidence_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("rests_on_run", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("judged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verdict_evidence_id", sa.BigInteger(), nullable=True),
        _key("canvas_id", "canvas_canvases", ondelete="CASCADE"),
        _key("claim_id", "canvas_claims", ondelete="CASCADE"),
        _key("run_id", "runs", ondelete="SET NULL"),
        _key("key_id", "signal_keys", ondelete="RESTRICT"),
        _key("signal_id", "signals", ondelete="SET NULL"),
        _key("evidence_id", "canvas_evidence", ondelete="SET NULL"),
        _key("verdict_evidence_id", "canvas_evidence", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="canvas_judgings_pkey"),
        sa.CheckConstraint(_judging_check(), name="canvas_judgings_check"),
    )
    op.create_index("canvas_judgings_claim_idx", NAME, ["claim_id", "measured_at"])
    op.create_index("canvas_judgings_run_idx", NAME, ["run_id"])


def downgrade() -> None:
    for index in INDEXES:
        op.drop_index(index, table_name=NAME)
    op.drop_table(NAME)
    op.drop_constraint(PROPOSAL_KEY, "canvas_proposals", type_="foreignkey")
    op.drop_column("canvas_proposals", "evidence_id")
    op.drop_constraint(EVIDENCE_CHECK, "canvas_evidence", type_="check")
    op.drop_column("canvas_evidence", "rests_on_run")
    op.drop_column("canvas_evidence", "basis")
